"""Observer attachment/capture/round limits; no external model or user profile."""
import base64
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import io
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.application import OperationError
from backend.core.native_host import NativeHost
from backend.core.observer.bridge import ObserverClient
from backend.core.observer.files import private_directory, read_private_json
from backend.core.observer.recording import Archive, Recorder, close_process_recorder
from backend.core.observer.trial import Trial


def eventually(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(.02)
    raise AssertionError("observer did not reach the expected state")


def test_private_archive_exact_unicode_redaction_dedup_and_chunks(tmp_path_factory):
    directory = tmp_path_factory / "capture"
    recorder = Recorder(directory)
    message = {"role": "user", "content": "文件🧭\n" * 20000}
    payload = {"detail": {"messages": [message], "tools": [{"name": "read_file"}],
                           "api_key": "local-secret"}, "token": "private", "event": "provider_request_started"}
    assert recorder.submit("trace_source", payload)
    assert recorder.submit("trace_source", {**payload, "detail": {**payload["detail"], "messages": [message, {"role": "assistant", "content": "好"}]}})
    recorder.close()
    archive = Archive(directory)
    rows = archive.events(recorder.source_id)["events"]
    assert len(rows) == 2
    body = archive.body(recorder.source_id, rows[0]["body_ref"])
    assert body["detail"]["messages"][0] == message
    assert body["detail"]["api_key"] == body["token"] == "[REDACTED]"
    chunks, offset = [], 0
    while True:
        chunk = archive.chunk(recorder.source_id, rows[0]["body_ref"], offset, 8191)
        chunks.append(base64.b64decode(chunk["data_base64"]))
        offset = chunk["next_offset"]
        if chunk["complete"]:
            break
    assert json.loads(b"".join(chunks)) == body
    with closing(sqlite3.connect(recorder.path)) as db:
        # One copy of the unchanged, large user message despite two complete snapshots.
        assert db.execute("SELECT COUNT(*) FROM objects WHERE bytes>100000").fetchone()[0] == 1
    assert archive.status()["coverage"] == "capturing"
    assert recorder.health()["dropped"] == 0


def test_overflow_and_disk_failure_are_visible_not_blocking(tmp_path_factory):
    recorder = Recorder(tmp_path_factory / "small", queue_bytes=4096)
    start = time.perf_counter()
    assert not recorder.submit("large", {"body": "x" * 10000000})
    assert time.perf_counter() - start < .1
    recorder.close()
    assert recorder.health()["dropped"] == 1 and recorder.health()["coverage"] == "degraded"
    failed = Recorder(tmp_path_factory / "full", disk_bytes=1)
    failed.submit("test", {"event": "test"})
    eventually(lambda: failed.health()["error"])
    assert "OBSERVER_DISK_QUOTA_EXCEEDED" in failed.health()["error"]
    assert not failed.submit("after_failure", {})
    failed.close()


def test_live_source_precedes_trace_throttling_and_includes_fragments(tmp_path_factory, monkeypatch):
    from backend.core.loop.trace import TraceJournal
    directory = private_directory(tmp_path_factory / "live")
    monkeypatch.setenv("GITGO_OBSERVER_CAPTURE_DIR", str(directory))
    storage = SimpleNamespace(trace_last_sequence=lambda _: 0, append_trace_record=lambda *a, **k: None)
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    journal = TraceJournal(workspace, "observed", storage=storage)
    long_text = "完整中文🧭" * 4000
    assert not journal.append({"event": "toolcall_delta", "delta": "partial"})["trace_persisted"]
    assert not journal.append({"event": "provider_request_started"}, detail={"messages": [{"role": "user", "content": long_text}], "tools": []})["trace_persisted"]
    close_process_recorder()
    archive = Archive(directory)
    source = archive.status()["sources"][0]["source_id"]
    rows = archive.events(source)["events"]
    assert rows[0]["summary"]["event"] == "toolcall_delta"
    body = archive.body(source, rows[1]["body_ref"])
    assert body["detail"]["messages"][0]["content"] == long_text


def test_attach_uses_original_owner_and_cannot_write_without_confirmation(tmp_path_factory, monkeypatch):
    directory = tmp_path_factory / "host"
    monkeypatch.setenv("GITGO_OBSERVER_DIR", str(directory))
    host = NativeHost(stdout=io.StringIO())
    marker = object()
    host._sessions["demo"] = "stable-session"
    called = []
    monkeypatch.setattr(host, "_runtime_status", lambda p: {"daemon_online": True, "owner": id(marker), "session_id": host._sessions[p]})
    monkeypatch.setattr(host, "_runtime_stop", lambda *a: called.append(a) or {"cancelled": True})
    try:
        reader = ObserverClient(directory / "attach.json")
        status = reader.call("runtime.status", {"project": "demo"})
        assert status["result"] == {"daemon_online": True, "owner": id(marker), "session_id": "stable-session"}
        assert not host.stdout.getvalue()  # RPC replies never pollute Dashboard stdio.
        with pytest.raises(OperationError, match="Explicit control"):
            reader.call("runtime.stop", {"project": "demo", "process_id": "p"}, confirm=True)
        writer = ObserverClient(directory / "attach.json", control=True)
        # Server independently enforces confirmation even if the adapter is bypassed.
        descriptor = read_private_json(directory / "control.json")
        denied = {**descriptor, "operation": "runtime.stop", "arguments": {"project": "demo", "process_id": "p"}}
        with pytest.raises(OperationError, match="confirmation"):
            host._observer.execute(denied)
        assert not called
        assert writer.call("runtime.stop", {"project": "demo", "process_id": "p"}, confirm=True)["ok"]
        assert len(called) == 1
        invalid = {**descriptor, "instance_id": "stale", "operation": "observer.status"}
        with pytest.raises(OperationError, match="Reconnect"):
            host._observer.execute(invalid)
        # Disconnecting observation never closes the original runtime.
        from mcp_tools import control
        monkeypatch.setenv("GITGO_CONTROL_DESCRIPTOR", str(directory / "attach.json"))
        assert control._call("runtime.status", {"project": "demo"})["daemon_online"]
        control.shutdown()
        assert reader.call("runtime.status", {"project": "demo"})["ok"]
    finally:
        host.close()
        close_process_recorder()
    assert not (directory / "attach.json").exists()
    assert (directory / "run.json").exists()
    with pytest.raises(OperationError, match="cannot be reached"):
        reader.call("observer.status")


def test_original_request_admission_memo_is_shared_by_control_transport(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GITGO_OBSERVER_DIR", str(tmp_path_factory / "memo"))
    host = NativeHost(stdout=io.StringIO())
    executions = []
    monkeypatch.setattr(host, "_runtime_chat", lambda rid, args: executions.append(rid) or {"session_id": "same"})
    try:
        client = ObserverClient(tmp_path_factory / "memo" / "attach.json", control=True)
        for _ in range(2):
            result = client.call("runtime.chat", {"project": "demo", "message": "hello"}, request_id="same", confirm=True)
            assert result["result"]["session_id"] == "same"
        assert executions == ["same"]
        denied = client.call("runtime.chat", {"project": "demo", "message": "changed"}, request_id="same", confirm=True)
        assert not denied["ok"] and denied["error"]["code"] == "REQUEST_IDENTITY_CONFLICT"
    finally:
        host.close()


@pytest.mark.parametrize("action", ["resume", "discard"])
def test_recovery_uses_original_request_memo(action, monkeypatch):
    host = NativeHost(stdout=io.StringIO())
    executed = []
    monkeypatch.setattr(host, "_runtime_recovery", lambda rid, args, **kw: executed.append(kw["action"]) or {"status": "returned"})
    try:
        for _ in range(2):
            replies = []
            host._handle_request({"request_id": "same", "operation": "runtime.recovery." + action,
                                  "arguments": {"project": "demo", "process_id": "p"}}, response_callback=replies.append)
            assert replies[0]["ok"]
        assert executed == [action]
    finally:
        host.close()


def test_deferred_start_and_numeric_frontend_contract(tmp_path_factory, monkeypatch):
    directory = tmp_path_factory / "deferred"
    monkeypatch.setenv("GITGO_OBSERVER_DIR", str(directory))
    monkeypatch.setenv("GITGO_OBSERVER_DEFER_START", "1")
    host = NativeHost(stdout=io.StringIO())
    try:
        assert not directory.exists()
        assert host._dispatch({"operation": "observer.start"})["enabled"]
        assert host._dispatch({"operation": "observer.start"})["enabled"]
        assert host._dispatch({"operation": "observer.frontend", "arguments": {"frames": {"duration": {"count": 1, "buckets": [0, 1]}}}})["captured"]
        with pytest.raises(OperationError, match="numeric"):
            host._dispatch({"operation": "observer.frontend", "arguments": {"inputs": {"text": "secret typed field"}}})
        assert host._observer.status()["frontend_batches"] == 1
    finally:
        host.close()


def trial_fixture(root):
    workspace = root / "workspace"
    workspace.mkdir()
    plan = root / "plan.json"
    plan.write_text(json.dumps({"project": "trial", "workspace": str(workspace),
        "state_file": str(root / "trial-budget" / "state.json"), "phases": [
        {"provider_id": "deepseek-fixture", "max_user_turns": 10}, {"provider_id": "gpt-fixture", "max_user_turns": 10}]}))
    directory = private_directory(root / "trial")
    switches = []
    host = SimpleNamespace(services=SimpleNamespace(project_list=lambda: [{"name": "trial", "workspace": str(workspace)}]),
                           _provider_switch=lambda p: switches.append(p), _runtime_status=lambda p: {"processes": {}})
    return Trial(plan, directory, host), host, switches


def test_ten_plus_ten_counts_real_admissions_not_steps_retries_or_decisions(tmp_path_factory):
    trial, host, switches = trial_fixture(tmp_path_factory)
    for i in range(20):
        rid = f"user-{i}"
        operation = ["runtime.chat", "runtime.decision", "runtime.btw", "runtime.feedback"][i % 4]
        trial.before(rid, {"project": "trial"}, "dashboard" if i != 4 else "control", operation)
        trial.admitted(rid, {"process_id": "same-process", "task_id": str(i), "session_id": "same-session"})
        trial.admitted(rid, {})  # Duplicate ack cannot consume another round.
        trial.finished(rid, {"result": {"provider_calls": 400, "steps": 600}})
    assert switches == ["deepseek-fixture"] * 10 + ["gpt-fixture"] * 10
    assert trial.status()["counts"] == [10, 10]
    assert len(trial.status()["accepted"]) == 20
    with pytest.raises(OperationError, match="budget is complete"):
        trial.before("twenty-first", {"project": "trial"}, "dashboard")
    # An explicitly authorized external user message consumes budget and preserves its true origin.
    assert trial.status()["accepted"]["user-4"]["origin"] == "control"
    assert set(v["operation"] for v in trial.status()["accepted"].values()) == {
        "runtime.chat", "runtime.decision", "runtime.btw", "runtime.feedback"}
    with pytest.raises(OperationError, match="budget is complete"):
        trial.before("intervention", {"project": "trial"}, "control")


def test_trial_rejects_new_session_project_drift_and_pending_boundary(tmp_path_factory):
    trial, host, switches = trial_fixture(tmp_path_factory)
    with pytest.raises(OperationError, match="existing conversation"):
        trial.before("new", {"project": "trial", "session_mode": "fresh"}, "dashboard")
    # Pre-admission validation failures and transport acceptance do not consume rounds.
    trial.before("rejected", {"project": "trial"}, "dashboard")
    trial.finished("rejected", {"error": {"code": "INPUT_INTEGRITY_MISMATCH"}})
    assert trial.status()["counts"] == [0, 0]
    trial.state["counts"][0] = 9
    trial.before("tenth", {"project": "trial"}, "dashboard")
    trial.admitted("tenth", {"process_id": "p"})
    with pytest.raises(OperationError, match="current request to return"):
        trial.before("next-phase", {"project": "trial"}, "dashboard")
    assert switches == ["deepseek-fixture", "deepseek-fixture"]
    trial.finished("tenth", {"result": {"status": "awaiting_user"}})
    trial.before("answer-next-model", {"project": "trial"}, "dashboard", "runtime.decision")
    assert switches[-1] == "gpt-fixture"
    trial.admitted("answer-next-model", {"process_id": "p"})
    trial.finished("answer-next-model", {"result": {"status": "completed"}})
    host.services.project_list = lambda: [{"name": "trial", "workspace": str(tmp_path_factory / "original")}]
    with pytest.raises(OperationError, match="isolated workspace"):
        trial.before("drift", {"project": "trial"}, "dashboard")


def test_concurrent_btw_and_main_inputs_share_budget_without_blocking_each_other(tmp_path_factory):
    trial, _, _ = trial_fixture(tmp_path_factory)
    trial.before("main", {"project": "trial"}, "dashboard")
    trial.admitted("main", {"process_id": "p"})
    trial.before("btw", {"project": "trial"}, "dashboard", "runtime.btw")
    trial.admitted("btw", {"sidecar_id": "s"})
    trial.finished("btw", {"result": {"status": "completed"}})
    assert trial.status()["counts"] == [2, 0] and set(trial.status()["pending"]) == {"main"}
    trial.finished("main", {"result": {"status": "completed"}})
    assert not trial.status()["pending"]


def test_real_host_counts_decisions_live_feedback_and_btw_admission_failure(tmp_path_factory, monkeypatch):
    trial, _, _ = trial_fixture(tmp_path_factory)
    monkeypatch.setenv("GITGO_OBSERVER_DIR", str(tmp_path_factory / "capture-mixed"))
    host = NativeHost(stdout=io.StringIO())
    host._observer.trial = trial
    def chat(rid, args, **kwargs):
        host._emit({"type": "event", "request_id": rid, "payload": {
            "event": "runtime_ack", "stage": "admitted", "task_id": "fixture-t", "process_id": "fixture-p"}})
        return {"status": "completed"}
    def btw(command, **kwargs):
        host._on_daemon_event("trial", {"event": "command_result", "result": {
            "accepted": True, "sidecar_id": command["sidecar_id"]}})
        raise OperationError("FIXTURE_TRANSPORT_FAILURE", "Failure after real sidecar admission")
    client = SimpleNamespace(send_command=lambda command: ({"processes": {"fixture-p": {"status": "running"}}}
        if command["cmd"] == "loop_status" else {"status": "accepted", "task_id": "fixture-t", "process_id": "fixture-p"}), send_btw=btw)
    monkeypatch.setattr(host, "_runtime_chat", chat)
    monkeypatch.setattr(host, "_get_daemon", lambda *a, **kw: client)
    try:
        for i, operation in enumerate(["runtime.chat", "runtime.decision", "runtime.feedback", "runtime.btw"]):
            replies = []
            request = {"request_id": f"mixed-{i}", "operation": operation,
                "arguments": {"project": "trial", "process_id": "fixture-p", "message": "real input", "question": "BTW input"}}
            host._handle_request(request, response_callback=replies.append)
            assert replies[0]["ok"] == (operation != "runtime.btw")
            # Re-delivery uses the original admission/result and never consumes another round.
            host._handle_request(request, response_callback=lambda _: None)
        assert trial.status()["counts"] == [4, 0]
        assert trial.status()["accepted"]["mixed-3"]["error_code"] == "FIXTURE_TRANSPORT_FAILURE"
    finally:
        host.close()


def test_trial_counter_disk_failure_does_not_replay_already_admitted_work(tmp_path_factory, monkeypatch):
    trial, host, _ = trial_fixture(tmp_path_factory)
    trial.before("live", {"project": "trial"}, "dashboard")
    def fail(*_):
        raise OSError("disk full")
    monkeypatch.setattr("backend.core.observer.trial.atomic_json", fail)
    trial.admitted("live", {"task_id": "t"})
    trial.finished("live", {"result": {"status": "completed"}})
    assert trial.status()["counts"] == [1, 0] and "WRITE_FAILED" in trial.status()["error"]
    with pytest.raises(OperationError, match="could not be saved"):
        trial.before("later", {"project": "trial"}, "dashboard")


def test_trial_restart_keeps_budget_and_rejects_uncertain_admission(tmp_path_factory):
    trial, host, _ = trial_fixture(tmp_path_factory)
    trial.before("first", {"project": "trial"}, "dashboard")
    trial.admitted("first", {"session_id": "same"})
    trial.finished("first", {"result": {"status": "completed"}})
    next_directory = private_directory(tmp_path_factory / "capture-after-restart")
    restarted = Trial(tmp_path_factory / "plan.json", next_directory, host)
    assert restarted.status()["counts"] == [1, 0]
    restarted.before("interrupted", {"project": "trial"}, "dashboard")
    with pytest.raises(ValueError, match="ADMISSION_UNCERTAIN"):
        Trial(tmp_path_factory / "plan.json", next_directory, host)


def test_observer_disabled_has_no_socket_worker_or_capture_side_effect(monkeypatch):
    monkeypatch.delenv("GITGO_OBSERVER_DIR", raising=False)
    before = {t.ident for t in threading.enumerate()}
    host = NativeHost(stdout=io.StringIO())
    try:
        assert host._observer is None
        assert not host._dispatch({"operation": "observer.frontend"})["captured"]
        assert not any(t.ident not in before and t.name.startswith("observer-") for t in threading.enumerate())
    finally:
        host.close()


def test_capture_shutdown_failure_never_skips_original_daemon_cleanup(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GITGO_OBSERVER_DIR", str(tmp_path_factory / "shutdown"))
    host = NativeHost(stdout=io.StringIO())
    stopped = []
    host._daemons["fixture"] = SimpleNamespace(stop=lambda: stopped.append(True))
    original_close = host._observer.close
    def failed_close(**_):
        raise OSError("fixture metadata access denied")
    monkeypatch.setattr(host._observer, "close", failed_close)
    try:
        host.close()
        assert stopped == [True] and not host._daemons
    finally:
        original_close()


def test_concurrent_observer_start_attaches_exactly_once(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GITGO_OBSERVER_DIR", str(tmp_path_factory / "one-observer"))
    monkeypatch.setenv("GITGO_OBSERVER_DEFER_START", "1")
    host = NativeHost(stdout=io.StringIO())
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: host._dispatch({"operation": "observer.start"}), range(4)))
        assert all(result["enabled"] for result in results)
        assert len(list((tmp_path_factory / "one-observer").glob("host-*.sqlite3"))) == 1
    finally:
        host.close()
