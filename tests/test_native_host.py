from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core.application import ApplicationServices, OperationError
from backend.core.daemon.client import DaemonClient, DaemonCommandError
from backend.core.llm_config import LLMConfigManager
from backend.core.loop.llm import LLMProvider, StreamInterruptedError
from backend.core.loop.outcome import OutcomeStatus, TaskOutcome
from backend.core.native_host import NativeHost, PROTOCOL_VERSION


def test_daemon_start_rejects_process_exit_before_started(monkeypatch):
    class ExitedProcess:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("startup exploded\n")
            self.returncode = 7

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    process = ExitedProcess()
    spawn = {}

    def fake_popen(args, **kwargs):
        spawn["args"] = args
        spawn["kwargs"] = kwargs
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("backend.core.daemon.client.attach_kill_job", lambda _process: None)
    monkeypatch.setattr(DaemonClient, "_fail_if_existing", lambda _self: None)

    client = DaemonClient("early-exit")
    with pytest.raises(RuntimeError, match=r"exited before readiness.*code=7") as exc:
        client.start(timeout=1)
    assert "startup exploded" in str(exc.value)
    assert spawn["args"][1] == str(client._project_root / "__main__.py")
    assert spawn["kwargs"]["cwd"] == str(client._project_root)


def test_daemon_disconnect_reports_exit_code_and_stderr_tail(monkeypatch):
    client = DaemonClient("diagnostic-test")
    client._process = SimpleNamespace(poll=lambda: 23)
    client._stderr_lines = ["first line\n", "fatal storage detail\n"]

    def disconnect(_command):
        for event in client._cmd_events.values():
            event.set()

    monkeypatch.setattr(client, "_write_cmd", disconnect)

    with pytest.raises(RuntimeError) as exc:
        client._send_command_once({"cmd": "task"}, timeout=0.1)

    message = str(exc.value)
    assert "code=23" in message
    assert "fatal storage detail" in message


def test_daemon_command_error_preserves_catalog_and_recovery_details(monkeypatch):
    client = DaemonClient("structured-error-test")

    def fail(_command):
        request_id = next(iter(client._cmd_events))
        client._cmd_results[request_id] = {
            "event": "command_result",
            "error": "STORAGE_CAS_REFERENCE_MISSING",
            "message": "missing durable object",
            "error_info": {
                "catalog_id": "GITGO-E7406",
                "details": {"ref": "sha256:missing"},
            },
        }
        client._cmd_events[request_id].set()

    monkeypatch.setattr(client, "_write_cmd", fail)

    with pytest.raises(DaemonCommandError) as exc:
        client._send_command_once({"cmd": "task"}, timeout=0.1)

    assert exc.value.code == "STORAGE_CAS_REFERENCE_MISSING"
    assert exc.value.error_info["catalog_id"] == "GITGO-E7406"
    assert exc.value.details["ref"] == "sha256:missing"


def test_native_host_reports_daemon_start_failure_with_stable_code():
    class BrokenDaemon:
        def __init__(self, _project):
            pass

        def is_running(self):
            return False

        def add_event_listener(self, _listener):
            pass

        def diagnostic_tail(self):
            return "backend detail"

        def start(self, timeout=30):
            raise RuntimeError("startup exploded")

    host = NativeHost(stdout=io.StringIO(), daemon_factory=BrokenDaemon)
    with pytest.raises(OperationError) as exc:
        host._get_daemon("demo", start=True)
    assert exc.value.code == "DAEMON_START_FAILED"
    assert "startup exploded" in str(exc.value)
    assert exc.value.details["diagnostic_tail"] == "backend detail"


def test_daemon_cold_start_does_not_block_status_projection():
    entered = threading.Event()
    release = threading.Event()

    class SlowDaemon:
        def __init__(self, _project):
            self.running = False

        def is_running(self):
            return self.running

        def add_event_listener(self, _listener):
            pass

        def start(self, timeout=30):
            entered.set()
            assert release.wait(timeout=2)
            self.running = True

    host = NativeHost(stdout=io.StringIO(), daemon_factory=SlowDaemon)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(host._get_daemon("demo", start=True)),
    )
    worker.start()
    assert entered.wait(timeout=1)

    started = time.perf_counter()
    assert host._get_daemon("demo", start=False) is None
    assert time.perf_counter() - started < 0.2

    release.set()
    worker.join(timeout=2)
    assert len(result) == 1
    assert result[0].is_running()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object contract")
def test_windows_kill_on_close_job_terminates_attached_child():
    """Model a terminal hard-close: the owner disappears without RPC cleanup."""
    from backend.core.process_control import attach_kill_job, close_job, creation_flags

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=creation_flags(),
    )
    job = attach_kill_job(child)
    if not job:
        child.kill()
        child.wait(timeout=5)
        pytest.skip("This Windows host did not allow nested Job Object assignment")
    close_job(job)
    child.wait(timeout=5)
    assert child.returncode is not None


def test_daemon_terminal_correlation_precedes_slow_observer():
    client = DaemonClient("correlation-order")
    task_id = "terminal-task"
    terminal = threading.Event()
    observer_entered = threading.Event()
    observer_release = threading.Event()
    client._agent_events[task_id] = terminal
    client._running = True
    client._process = SimpleNamespace(stdout=io.StringIO(
        json.dumps({
            "event": "agent_complete",
            "task_id": task_id,
            "outcome": {"status": "failed"},
        }) + "\n"
    ))

    def slow_observer(_event):
        observer_entered.set()
        observer_release.wait(2)

    client.add_event_listener(slow_observer)
    reader = threading.Thread(target=client._read_stdout, daemon=True)
    reader.start()
    assert observer_entered.wait(1)
    assert terminal.is_set()
    assert client._agent_data[task_id]["event"] == "agent_complete"
    observer_release.set()
    reader.join(2)
    assert not reader.is_alive()


def test_send_task_reconciles_durable_terminal_state_when_event_is_missed(monkeypatch):
    client = DaemonClient("terminal-reconciliation")
    client.TASK_RESULT_POLL_SECONDS = 0.001
    task_id = "durable-task"
    process_id = "durable-process"
    outcome = TaskOutcome(
        task_id=task_id,
        process_id=process_id,
        status=OutcomeStatus.COMPLETED,
        process_status="completed",
        response="finished",
        llm_used=True,
    ).to_dict()
    commands = []

    def fake_send(command, timeout=30.0):
        commands.append(dict(command))
        if command["cmd"] == "task":
            return {
                "status": "pending",
                "task_id": task_id,
                "process_id": process_id,
                "session_id": "session-1",
            }
        assert command == {
            "cmd": "task_result",
            "task_id": task_id,
            "process_id": process_id,
        }
        return {
            "task_id": task_id,
            "process_id": process_id,
            "session_id": "session-1",
            "status": "completed",
            "terminal": True,
            "outcome": outcome,
        }

    monkeypatch.setattr(client, "send_command", fake_send)
    observed = []
    client.add_event_listener(observed.append)

    result = client.send_task({"cmd": "task", "task_id": task_id}, timeout=1)

    assert result["outcome"] == outcome
    assert result["recovered_from"] == "task_result"
    assert [item["cmd"] for item in commands] == ["task", "task_result"]
    assert observed == [result]
    assert task_id not in client._agent_events
    assert task_id not in client._agent_data


def test_immediate_daemon_event_flushes_pending_protocol_events(monkeypatch):
    from backend.core.daemon import emit as daemon_emit

    output = io.StringIO()
    monkeypatch.setattr(daemon_emit.sys, "stdout", output)
    monkeypatch.setattr(daemon_emit, "_emit_buffer", [])
    monkeypatch.setattr(daemon_emit, "_last_flush_time", time.time() * 1000)

    daemon_emit._emit_v2({"event": "scan_progress"})
    assert output.getvalue() == ""

    daemon_emit._emit_v2({"event": "daemon_started"}, priority="immediate")
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [event["event"] for event in events] == ["scan_progress", "daemon_started"]


def test_json_line_protocol_is_utf8_even_under_legacy_windows_locale(monkeypatch):
    from backend.core.daemon import emit as daemon_emit

    daemon_bytes = io.BytesIO()
    daemon_stdout = io.TextIOWrapper(daemon_bytes, encoding="gbk")
    monkeypatch.setattr(daemon_emit.sys, "stdout", daemon_stdout)
    monkeypatch.setattr(daemon_emit, "_emit_buffer", [])
    daemon_emit._emit_v2(
        {"event": "agent_terminal", "message": "passed ✅"},
        priority="immediate",
    )
    decoded = daemon_bytes.getvalue().decode("utf-8")
    assert json.loads(decoded)["message"] == "passed ✅"

    host_bytes = io.BytesIO()
    host_stdout = io.TextIOWrapper(host_bytes, encoding="gbk")
    host = NativeHost(stdout=host_stdout)
    host._emit({"type": "event", "payload": {"message": "完成 ✅"}})
    envelope = json.loads(host_bytes.getvalue().decode("utf-8"))
    assert envelope["payload"]["message"] == "完成 ✅"
    host.close()


def test_workspace_watcher_matches_exclusions_against_relative_paths(tmp_path_factory):
    from backend.core.daemon.watcher import WorkspaceWatcher

    tmp_path = tmp_path_factory
    watcher = WorkspaceWatcher(
        tmp_path,
        [".gitgo/", ".codex/", "__pycache__/", "*.pyc", "CLAUDE.md"],
        lambda *_args: None,
    )
    assert watcher._is_excluded(str(tmp_path / ".gitgo" / "sessions" / "x.json"))
    assert watcher._is_excluded(str(tmp_path / ".codex" / "config.toml"))
    assert watcher._is_excluded(
        "//?/C:/unmatched/prefix/.gitgo/context_objects/.active.json.tmp"
    )
    assert watcher._is_excluded(str(tmp_path / "pkg" / "__pycache__" / "x.bin"))
    assert watcher._is_excluded(str(tmp_path / "pkg" / "cache.pyc"))
    assert watcher._is_excluded(str(tmp_path / "docs" / "CLAUDE.md"))
    assert not watcher._is_excluded(str(tmp_path / "backend" / "core.py"))


def test_workspace_watcher_records_atomic_move_destination(tmp_path_factory):
    """Content-aware checks must receive the final path from atomic writes."""
    from types import SimpleNamespace

    from backend.core.daemon.watcher import WorkspaceWatcher

    tmp_path = tmp_path_factory
    observed = []
    watcher = WorkspaceWatcher(
        workspace_path=tmp_path,
        exclude_patterns=[".gitgo/"],
        on_dirty=lambda changed: observed.append(changed),
        debounce_sec=60,
    )
    source = tmp_path / ".harvest-source.txt.abc.tmp"
    destination = tmp_path / "harvest-source.txt"
    watcher.on_any_event(SimpleNamespace(
        is_directory=False,
        src_path=str(source),
        dest_path=str(destination),
    ))
    try:
        watcher._fire()
    finally:
        watcher.stop()

    assert observed == [[
        ".harvest-source.txt.abc.tmp",
        "harvest-source.txt",
    ]]


def test_unknown_application_operation_is_stable_error():
    services = ApplicationServices()
    with pytest.raises(OperationError) as exc:
        services.invoke("missing.operation")
    assert exc.value.code == "UNKNOWN_OPERATION"


def test_provider_secret_is_not_round_tripped_and_blank_can_retain(tmp_path_factory):
    config_path = tmp_path_factory / "llm_config.json"
    with patch.object(LLMConfigManager, "_config_path", return_value=config_path):
        services = ApplicationServices()
        fake_key = "sk-" + "secret-" + "123456789"
        created = services.provider_save(
            name="test", base_url="https://example.invalid/v1",
            api_key=fake_key, model_id="model",
            context_window=64000, max_output_tokens=2048,
        )
        provider_id = created["provider"]["id"]
        assert "api_key" not in created["provider"]
        assert created["provider"]["api_key_present"] is True

        status = services.provider_status()
        assert "api_key" not in status["providers"][0]
        assert "secret_ref" not in status["providers"][0]
        assert status["providers"][0]["api_key_display"].startswith("sk-s")
        assert status["providers"][0]["context_window"] == 64000
        assert status["providers"][0]["max_output_tokens"] == 2048

        services.provider_save(
            provider_id=provider_id, name="renamed",
            base_url="https://example.invalid/v1", api_key="",
            model_id="model", retain_api_key=True,
            context_window=64000, max_output_tokens=2048,
        )
        active = LLMConfigManager.get_active()
        assert active.api_key == fake_key
        assert active.runtime_capabilities()["context_window"] == 64000
        metadata = config_path.read_text(encoding="utf-8")
        secret_file = config_path.with_name("provider_secrets.json")
        encrypted = secret_file.read_text(encoding="utf-8")
        assert fake_key not in metadata
        assert fake_key not in encrypted
        assert '"secret_ref"' in metadata


def test_plaintext_provider_config_is_migrated_to_encrypted_store(tmp_path_factory):
    config_path = tmp_path_factory / "llm_config.json"
    fake_key = "sk-" + "legacy-plaintext-must-disappear"
    config_path.write_text(json.dumps({
        "providers": [{
            "id": "legacy", "name": "legacy", "base_url": "https://example.invalid/v1",
            "api_key": fake_key, "model_id": "model",
        }],
        "active_provider": "legacy", "failover_enabled": False, "failover_order": [],
    }), encoding="utf-8")
    with patch.object(LLMConfigManager, "_config_path", return_value=config_path):
        provider = LLMConfigManager.get_active()
        assert provider is not None and provider.api_key == fake_key
        assert fake_key not in config_path.read_text(encoding="utf-8")
        assert fake_key not in config_path.with_name("provider_secrets.json").read_text(encoding="utf-8")


def test_stream_never_replays_after_first_chunk():
    provider = LLMProvider("https://example.invalid", "key", "model")
    calls = 0

    def interrupted(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        yield {"choices": [{"delta": {"content": "partial"}}]}
        raise StreamInterruptedError()

    provider._stream_once = interrupted  # type: ignore[method-assign]
    stream = provider.stream_chat([{"role": "user", "content": "hi"}])
    assert next(stream)["choices"][0]["delta"]["content"] == "partial"
    with pytest.raises(StreamInterruptedError):
        next(stream)
    assert calls == 1


def test_stream_retries_once_before_first_chunk():
    provider = LLMProvider("https://example.invalid", "key", "model")
    calls = 0

    def transient(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StreamInterruptedError()
        yield {"ok": True}

    provider._stream_once = transient  # type: ignore[method-assign]
    assert list(provider.stream_chat([{"role": "user", "content": "hi"}])) == [{"ok": True}]
    assert calls == 2


class FakeDaemon:
    def __init__(self, project: str):
        self.project = project
        self.running = False
        self.listeners = []

    def add_event_listener(self, listener):
        self.listeners.append(listener)

    def start(self, timeout=30.0):
        self.start_timeout = timeout
        self.running = True

    def stop(self):
        self.running = False

    def is_running(self):
        return self.running

    def send_command(self, command):
        if command.get("cmd") == "loop_status":
            return {"daemon_online": True, "processes": {},
                    "recent_tool_executed": [], "main_conversation": [],
                    "agent_conversations": {}}
        return {"requested": True}

    def send_task(self, command, timeout=300, on_ack=None):
        ack = {"task_id": command["task_id"], "process_id": "process-1",
               "session_id": "session-1"}
        if on_ack:
            on_ack(ack)
        for listener in self.listeners:
            listener({"event": "text_delta", "task_id": command["task_id"],
                      "delta": "hello"})
        outcome = {"status": "completed", "task_id": command["task_id"],
                   "process_id": "process-1", "process_status": "completed",
                   "response": "hello", "steps_used": 1, "llm_used": True,
                   "error": None}
        return {"event": "agent_complete", "session_id": "session-1",
                "outcome": outcome}

    def send_btw(self, command, timeout=135):
        return self.send_command(command)


def test_native_host_close_cancels_roots_and_btw_before_stopping_daemon():
    class RecordingDaemon(FakeDaemon):
        def __init__(self, project):
            super().__init__(project)
            self.commands = []

        def send_command(self, command, timeout=30.0):
            self.commands.append(dict(command))
            return {"cancelled": True, "status": "cancelled"}

    host = NativeHost(stdout=io.StringIO(), daemon_factory=RecordingDaemon)
    daemon = host._get_daemon("demo", start=True)
    host._active_processes["root-request"] = ("demo", "root-process")
    host._active_btw["btw-request"] = ("demo", "sidecar-1")

    host.close()

    assert daemon.commands[:2] == [
        {"cmd": "task", "action": "kill", "process_id": "root-process",
         "reason": "native_host_closed", "wait_timeout": 2.0},
        {"cmd": "task", "action": "btw_cancel", "sidecar_id": "sidecar-1"},
    ]
    assert daemon.running is False


def test_native_host_correlates_streams_and_owns_one_daemon():
    output = io.StringIO()
    host = NativeHost(stdout=output, daemon_factory=FakeDaemon)
    result = host._runtime_chat("request-1", {"project": "demo", "message": "hi"})
    assert result["status"] == "completed"
    assert result["outcome"]["duration_ms"] > 0
    assert result["outcome"]["metadata"]["timing"]["request_started_at"].endswith("+00:00")
    assert len(host._daemons) == 1
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert all(event["protocol_version"] == PROTOCOL_VERSION for event in events)
    assert any(event.get("request_id") == "request-1" and
               event.get("payload", {}).get("event") == "text_delta"
               for event in events)
    acknowledgements = [
        event["payload"] for event in events
        if event.get("request_id") == "request-1"
        and event.get("payload", {}).get("event") == "runtime_ack"
    ]
    assert [item["stage"] for item in acknowledgements] == ["accepted", "admitted"]
    assert acknowledgements[0]["process_id"] == ""
    assert acknowledgements[0]["task_id"] == acknowledgements[1]["task_id"]
    assert acknowledgements[1]["process_id"] == "process-1"
    host.close()


def test_native_host_rejects_corrupt_prompt_before_provider_or_daemon():
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    with pytest.raises(OperationError) as raised:
        host._runtime_chat("bad-input", {"project": "demo", "message": "bad\udcaf"})
    assert raised.value.code == "INPUT_ENCODING_CORRUPTED"
    assert host._daemons == {}
    host.close()


def test_native_host_rejects_prompt_digest_mismatch_before_provider():
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    with pytest.raises(OperationError) as raised:
        host._runtime_chat("bad-digest", {
            "project": "demo", "message": "中文",
            "message_utf8_sha256": "0" * 64,
        })
    assert raised.value.code == "INPUT_INTEGRITY_MISMATCH"
    assert host._daemons == {}
    host.close()


def test_native_host_rejects_stale_dashboard_project_identity(monkeypatch):
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host.services.project_list = lambda: [{"name": "demo", "workspace": "C:/demo"}]
    monkeypatch.setattr(
        "backend.core.storage.resolve_existing_storage_paths",
        lambda _workspace: SimpleNamespace(project_id="actual-project-id"),
    )
    with pytest.raises(OperationError) as raised:
        host._runtime_chat("wrong-project", {
            "project": "demo", "message": "hello",
            "expected_project_id": "stale-project-id",
        })
    assert raised.value.code == "PROJECT_IDENTITY_MISMATCH"
    assert host._daemons == {}
    host.close()


def test_native_host_binds_host_search_configuration_to_task_preferences():
    class RecordingDaemon(FakeDaemon):
        def send_task(self, command, timeout=300, on_ack=None):
            self.last_task = dict(command)
            return super().send_task(command, timeout=timeout, on_ack=on_ack)

    host = NativeHost(stdout=io.StringIO(), daemon_factory=RecordingDaemon)
    host.services.config_get = lambda: {
        "auto_compact": True,
        "agent_routing": "owner",
        "web_search_mode": "auto",
        "web_search_endpoint": "https://search.example.test/search",
        "web_search_engine": "duckduckgo",
    }
    host._runtime_chat("search-config", {"project": "demo", "message": "hi"})

    preferences = host._daemons["demo"].last_task["runtime_preferences"]
    request_started_at = preferences.pop("request_started_at_utc")
    assert request_started_at.endswith("+00:00")
    assert preferences.pop("frontend_origin") == host.frontend_origin
    assert preferences == {
        "auto_compact": True,
        "agent_routing": "owner",
        "web_search_mode": "auto",
        "web_search_endpoint": "https://search.example.test/search",
        "web_search_engine": "duckduckgo",
    }
    host.close()


def test_provider_switch_retires_idle_daemons_but_keeps_session_identity(monkeypatch):
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    daemon = host._get_daemon("demo", start=True)
    host._sessions["demo"] = "session-1"
    host._conversation_processes["demo"] = "process-1"
    monkeypatch.setattr(
        host.services, "provider_switch",
        lambda provider_id: {"status": "switched", "active_provider": provider_id},
    )

    result = host._provider_switch("mimo")

    assert result["runtime_reload"] is True
    assert result["retired_projects"] == ["demo"]
    assert daemon.running is False
    assert host._daemons == {}
    assert host._sessions["demo"] == "session-1"
    assert host._conversation_processes["demo"] == "process-1"
    host.close()


def test_provider_switch_refuses_inflight_task(monkeypatch):
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host._root_admissions.add("demo")
    called = False

    def switch(_provider_id):
        nonlocal called
        called = True

    monkeypatch.setattr(host.services, "provider_switch", switch)
    with pytest.raises(OperationError) as raised:
        host._provider_switch("mimo")
    assert raised.value.code == "PROVIDER_SWITCH_BUSY"
    assert called is False
    host._root_admissions.clear()
    host.close()


def test_decision_requires_full_task_process_and_decision_identity():
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    with pytest.raises(OperationError) as exc:
        host._runtime_chat("decision", {
            "project": "demo", "process_id": "process-1",
            "decision_id": "decision-1", "message": "Allow once",
        }, action="decision")
    assert exc.value.code == "INVALID_ARGUMENTS"
    assert "task_id" in str(exc.value)
    host.close()


def test_fresh_session_admission_does_not_reuse_durable_conversation_state():
    class RecordingDaemon(FakeDaemon):
        def send_task(self, command, timeout=300, on_ack=None):
            self.last_task = dict(command)
            return super().send_task(command, timeout=timeout, on_ack=on_ack)

    host = NativeHost(stdout=io.StringIO(), daemon_factory=RecordingDaemon)
    host._sessions["demo"] = "stale-session"
    host._conversation_processes["demo"] = "stale-root"

    result = host._runtime_chat(
        "fresh-eval", {"project": "demo", "message": "isolated", "session_mode": "fresh"},
    )

    command = host._daemons["demo"].last_task
    assert "session_id" not in command
    assert "conversation_process_id" not in command
    assert result["session_id"] == "session-1"
    assert host._sessions["demo"] == "session-1"
    assert host._conversation_processes["demo"] == "process-1"
    host.close()


def test_project_summary_reads_durable_terminal_state_without_starting_daemon(monkeypatch):
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host.services.project_list = lambda: [{"name": "cold", "workspace": "C:/cold"}]
    monkeypatch.setattr(
        "backend.core.storage.read_project_list_status",
        lambda _workspace: {
            "process_id": "root-old", "status": "completed",
            "updated_at": "2026-09-07T00:00:00Z",
        },
    )

    result = host._runtime_summaries()["projects"]["cold"]

    assert {key: result[key] for key in (
        "daemon_online", "state_available", "summary_error",
        "active_process_count", "waiting_process_count",
        "finished_process_count", "durable_status",
    )} == {
        "daemon_online": False, "state_available": True, "summary_error": "",
        "active_process_count": 0, "waiting_process_count": 0,
        "finished_process_count": 1, "durable_status": "completed",
    }
    assert result["stale"] is False
    assert result["observed_at"] > 0
    assert host._daemons == {}
    host.close()


def test_project_summary_surfaces_storage_failure_instead_of_claiming_new(monkeypatch):
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host.services.project_list = lambda: [{"name": "broken", "workspace": "C:/broken"}]

    def fail_storage(_workspace):
        raise OSError("database unavailable")

    monkeypatch.setattr("backend.core.storage.read_project_list_status", fail_storage)
    result = host._runtime_summaries()["projects"]["broken"]

    assert result["state_available"] is False
    assert result["summary_error"] == "database unavailable"
    assert result["active_process_count"] == 0
    assert result["waiting_process_count"] == 0
    assert result["finished_process_count"] == 0
    host.close()


def test_project_summary_falls_back_to_durable_state_when_live_daemon_is_busy(monkeypatch):
    class BusyDaemon:
        def is_running(self):
            return True

        def send_command(self, _command, timeout=5):
            raise TimeoutError("daemon busy")

        def stop(self):
            pass

    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host.services.project_list = lambda: [{"name": "busy", "workspace": "C:/busy"}]
    host._daemons["busy"] = BusyDaemon()
    monkeypatch.setattr(
        "backend.core.storage.read_project_list_status",
        lambda _workspace: {
            "process_id": "root-last", "status": "completed",
            "updated_at": "2026-09-24T00:00:00Z",
        },
    )

    result = host._runtime_summaries()["projects"]["busy"]

    assert result["daemon_online"] is False
    assert result["state_available"] is True
    assert result["finished_process_count"] == 1
    assert result["durable_status"] == "completed"
    assert result["stale"] is True
    assert result["summary_error"] == "daemon busy"
    assert result["last_known_at"] == "2026-09-24T00:00:00Z"
    host.close()


def test_project_overview_is_one_bounded_read_when_many_daemons_are_slow():
    class SlowDaemon:
        def is_running(self):
            return True

        def send_command(self, _command, timeout=5):
            time.sleep(min(float(timeout), 0.8))
            raise TimeoutError("slow daemon")

        def stop(self):
            pass

    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    projects = [
        {"name": f"p{index}", "workspace": f"C:/p{index}"}
        for index in range(20)
    ]
    host.services.project_list = lambda: list(projects)
    host._daemons.update({item["name"]: SlowDaemon() for item in projects})
    started = time.perf_counter()
    result = host._project_overview()
    elapsed = time.perf_counter() - started

    assert len(result["projects"]) == 20
    assert elapsed < 1.5
    assert all("runtime_summary" in item for item in result["projects"])
    assert all(item["runtime_summary"]["state_available"] is False
               for item in result["projects"])
    host.close()


def test_project_summary_reuses_timed_out_inflight_read():
    class SlowThenHealthyDaemon:
        calls = 0

        def is_running(self):
            return True

        def send_command(self, _command, timeout=5):
            self.calls += 1
            time.sleep(1.5)
            return {"processes": {}}

        def stop(self):
            pass

    daemon = SlowThenHealthyDaemon()
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host.services.project_list = lambda: [{"name": "slow", "workspace": "C:/slow"}]
    host._daemons["slow"] = daemon

    first = host._runtime_summaries()
    assert first["timed_out_projects"] == 1
    second = host._runtime_summaries()
    assert second["projects"]["slow"]["state_available"] is True
    assert daemon.calls == 1
    host.close()


def test_project_summary_returns_warm_projection_without_waiting_for_daemon():
    class SlowChangingDaemon:
        calls = 0

        def is_running(self):
            return True

        def send_command(self, _command, timeout=5):
            self.calls += 1
            time.sleep(0.5)
            return {"processes": {"root": {"status": "running"}}}

        def stop(self):
            pass

    daemon = SlowChangingDaemon()
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host.services.project_list = lambda: [{"name": "warm", "workspace": "C:/warm"}]
    host._daemons["warm"] = daemon
    host._summary_cache["warm"] = {
        "daemon_online": True, "state_available": True,
        "summary_error": "", "durable_status": "",
        "active_process_count": 0, "waiting_process_count": 0,
        "finished_process_count": 1, "observed_at": 1.0,
        "last_known_at": "", "stale": False,
    }

    started = time.perf_counter()
    result = host._runtime_summaries()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2
    assert result["warm_projects"] == 1
    assert result["projects"]["warm"]["finished_process_count"] == 1
    assert daemon.calls == 1
    host.close()


def test_cancel_during_admission_is_remembered_until_process_identity_exists():
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host._admission_requests["request-pending"] = ("demo", "task-pending")
    result = host._cancel("request-pending")
    assert result == {
        "cancelled": True,
        "pending_admission": True,
        "project": "demo",
        "task_id": "task-pending",
    }
    assert "request-pending" in host._cancelled_requests
    host.close()


def test_runtime_status_projects_all_durable_b_processes_while_daemon_is_offline():
    storage = SimpleNamespace(
        read_latest_conversations=lambda: {
            "main_conversation": [{"role": "assistant", "content": "latest"}],
            "agent_conversations": {"latest-b": [{"content": "latest B"}]},
            "conversation_process_id": "root-latest",
            "session_id": "session-latest",
        },
        read_project_b_conversations=lambda: {
            "old-b": [{"content": "old B"}],
            "latest-b": [{"content": "older projection"}],
        },
        read_project_b_processes=lambda: {
            "old-b": {"process_id": "old-b", "status": "completed"},
            "latest-b": {"process_id": "latest-b", "status": "waiting"},
        },
    )
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host.services = SimpleNamespace(
        project_list=lambda: [{"name": "demo", "workspace": "/workspace/demo"}],
        provider_status=lambda: {"providers": [], "active_provider": ""},
    )
    host._storage_projections["demo"] = storage
    result = host._runtime_status("demo")

    assert result["daemon_online"] is False
    assert set(result["processes"]) == {"old-b", "latest-b"}
    assert set(result["agent_conversations"]) == {"old-b", "latest-b"}
    assert result["agent_conversations"]["latest-b"] == [{"content": "latest B"}]
    host.close()


def test_runtime_status_degrades_instead_of_hiding_ui_on_storage_corruption():
    class DamagedStorage:
        def damaged(self):
            raise RuntimeError("database disk image is malformed")

        read_latest_conversations = damaged
        read_project_b_conversations = damaged
        read_project_b_processes = damaged
        read_latest_root_process = damaged

    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host.services = SimpleNamespace(
        project_list=lambda: [{"name": "demo", "workspace": "/workspace/demo"}],
        provider_status=lambda: {"providers": [], "active_provider": ""},
    )
    host._storage_projections["demo"] = DamagedStorage()
    result = host._runtime_status("demo")

    assert result["daemon_online"] is False
    assert result["processes"] == {}
    assert result["storage"]["level"] == "blocked"
    assert result["storage"]["reasons"] == ["storage_projection_failed"]
    assert len(result["storage"]["projection_errors"]) == 4
    host.close()


def test_runtime_status_returns_while_full_storage_projection_opens(monkeypatch):
    release = threading.Event()
    storage = SimpleNamespace()

    def slow_open(_workspace):
        release.wait(2)
        return storage

    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host.services = SimpleNamespace(
        project_list=lambda: [{"name": "demo", "workspace": "/workspace/demo"}],
        provider_status=lambda: {"providers": [], "active_provider": ""},
    )
    monkeypatch.setattr("backend.core.storage.get_storage", slow_open)
    started = time.perf_counter()
    result = host._runtime_status("demo")

    assert time.perf_counter() - started < 0.2
    assert result["storage"]["level"] == "initializing"
    assert result["processes"] == {}
    release.set()
    for _ in range(50):
        if host._storage_projections.get("demo") is storage:
            break
        time.sleep(0.01)
    assert host._storage_projections.get("demo") is storage
    host.close()


def test_native_host_feedback_targets_explicit_process_mailbox():
    class FeedbackDaemon(FakeDaemon):
        def send_command(self, command):
            if command.get("cmd") == "loop_status":
                return {"processes": {"child-1": {"status": "running"}}}
            self.last_command = command
            return {"status": "accepted", "instruction": {"sequence": 3}}

    host = NativeHost(stdout=io.StringIO(), daemon_factory=FeedbackDaemon)
    daemon = host._get_daemon("demo", start=True)
    result = host._runtime_feedback("demo", "child-1", "Re-check the boundary")
    assert result["status"] == "accepted"
    assert daemon.last_command == {
        "cmd": "task",
        "action": "instruct",
        "process_id": "child-1",
        "instruction": "Re-check the boundary",
        "actor_kind": "user",
    }
    host.close()


def test_native_host_feedback_continues_a_terminal_worker_session(monkeypatch):
    class FeedbackDaemon(FakeDaemon):
        def send_command(self, command):
            if command.get("cmd") == "loop_status":
                return {"processes": {"child-1": {
                    "status": "completed",
                    "parent_id": "root-1",
                    "role": "worker",
                    "actor_kind": "worker",
                    "capability_profile_id": "development.workspace",
                    "task_kind": "action",
                    "max_steps": 9,
                }}}
            return super().send_command(command)

        def send_task(self, command, timeout=300, on_ack=None):
            self.continuation = dict(command)
            return super().send_task(command, timeout=timeout, on_ack=on_ack)

    host = NativeHost(stdout=io.StringIO(), daemon_factory=FeedbackDaemon)
    monkeypatch.setattr(host.services, "config_get", lambda: {
        "auto_compact": True, "agent_routing": "owner", "web_search_endpoint": "",
    })
    daemon = host._get_daemon("demo", start=True)
    result = host._runtime_feedback(
        "demo", "child-1", "Revise the same deliverable", request_id="feedback-1",
    )

    assert result["continued_from_process_id"] == "child-1"
    assert daemon.continuation["process_id"] == "child-1"
    assert daemon.continuation["parent_id"] == "root-1"
    assert daemon.continuation["fresh_task_budget"] is True
    assert daemon.continuation["runtime_preferences"]["user_direct_continuation"] is True
    host.close()


def test_native_host_manual_compaction_targets_explicit_process():
    class CompactDaemon(FakeDaemon):
        def send_command(self, command, timeout=30.0):
            self.last_command = command
            return {"status": "queued", "applies_at": "next_safe_turn_boundary"}

    host = NativeHost(stdout=io.StringIO(), daemon_factory=CompactDaemon)
    daemon = host._get_daemon("demo", start=True)
    result = host._runtime_compact("demo", "root-1")
    assert result["status"] == "queued"
    assert daemon.last_command == {
        "cmd": "task", "action": "compact", "process_id": "root-1",
    }
    host.close()


def test_native_host_manual_compaction_starts_offline_project_for_durable_restore():
    class CompactDaemon(FakeDaemon):
        def send_command(self, command, timeout=30.0):
            self.last_command = command
            self.timeout = timeout
            return {"status": "completed", "changed": True, "context_epoch": 2}

    host = NativeHost(stdout=io.StringIO(), daemon_factory=CompactDaemon)
    assert "demo" not in host._daemons
    result = host._runtime_compact("demo")
    daemon = host._daemons["demo"]
    assert result["status"] == "completed"
    assert daemon.last_command == {
        "cmd": "task", "action": "compact", "process_id": "",
    }
    assert daemon.timeout == 135
    host.close()


def test_native_host_forwards_explicit_compaction_approval_identity():
    class CompactDaemon(FakeDaemon):
        def send_command(self, command, timeout=30.0):
            self.last_command = command
            return {"status": "completed", "changed": True}

    host = NativeHost(stdout=io.StringIO(), daemon_factory=CompactDaemon)
    try:
        host._runtime_compact("demo", "b-1", decision_id="decision-1", choice="force_compact")
        assert host._daemons["demo"].last_command == {
            "cmd": "task", "action": "compact", "process_id": "b-1",
            "decision_id": "decision-1", "choice": "force_compact",
        }
    finally:
        host.close()


def test_native_host_btw_uses_isolated_daemon_action():
    class BtwDaemon(FakeDaemon):
        def send_command(self, command, timeout=30.0):
            self.last_command = command
            return {"answer": "small answer", "sidecar_id": "side-1", "isolated": True}

    host = NativeHost(stdout=io.StringIO(), daemon_factory=BtwDaemon)
    daemon = host._get_daemon("demo", start=True)
    result = host._runtime_btw(
        "demo", {"question": "small question", "process_id": "root-1"},
    )
    assert result["isolated"] is True
    assert daemon.last_command["action"] == "btw"
    assert daemon.last_command["question"] == "small question"
    assert daemon.last_command["process_id"] == "root-1"
    host.close()


def test_native_host_recovery_requires_structured_candidate_and_preserves_task_id():
    class RecoveryDaemon(FakeDaemon):
        def send_command(self, command):
            if command.get("cmd") == "loop_status":
                return {
                    "daemon_online": True,
                    "processes": {},
                    "recovery_candidates": [{
                        "process_id": "process-recovery",
                        "task_id": "task-original",
                        "status": "resume_available",
                    }],
                }
            self.last_command = command
            return {"status": "discarded"}

        def send_task(self, command, timeout=300, on_ack=None):
            self.last_task = command
            if on_ack:
                on_ack({
                    "task_id": command["task_id"],
                    "process_id": command["process_id"],
                    "session_id": "session-recovery",
                })
            return {
                "event": "agent_complete",
                "session_id": "session-recovery",
                "outcome": {
                    "status": "completed",
                    "task_id": command["task_id"],
                    "process_id": command["process_id"],
                    "process_status": "completed",
                    "response": "recovered",
                    "steps_used": 1,
                    "llm_used": True,
                    "error": None,
                },
            }

    host = NativeHost(stdout=io.StringIO(), daemon_factory=RecoveryDaemon)
    resumed = host._runtime_recovery(
        "request-recovery",
        {"project": "demo", "process_id": "process-recovery"},
        action="resume",
    )
    daemon = host._daemons["demo"]

    assert resumed["status"] == "completed"
    assert daemon.last_task["action"] == "resume"
    assert daemon.last_task["process_id"] == "process-recovery"
    assert daemon.last_task["task_id"] == "task-original"
    assert daemon.last_task["manually_verified"] is False

    discarded = host._runtime_recovery(
        "request-discard",
        {"project": "demo", "process_id": "process-recovery", "reason": "obsolete"},
        action="discard",
    )
    assert discarded["status"] == "discarded"
    assert daemon.last_command["action"] == "discard"
    assert daemon.last_command["reason"] == "obsolete"
    host.close()


def test_task_timeout_is_not_hidden_when_cancellation_ack_also_times_out():
    class TimeoutDaemon(FakeDaemon):
        def send_task(self, command, timeout=300, on_ack=None):
            if on_ack:
                on_ack({
                    "task_id": command["task_id"],
                    "process_id": "process-stuck",
                    "session_id": "session-stuck",
                })
            raise RuntimeError("Agent task timed out after 255s")

        def send_command(self, command):
            if command.get("action") == "kill":
                raise RuntimeError("Command 'task' timed out after 30s")
            return super().send_command(command)

    host = NativeHost(stdout=io.StringIO(), daemon_factory=TimeoutDaemon)
    with pytest.raises(OperationError) as exc:
        host._runtime_chat("request-timeout", {"project": "demo", "message": "hi"})
    error = exc.value.to_dict()
    assert error["code"] == "AGENT_TASK_TIMEOUT"
    assert error["details"]["cancellation"]["error_code"] == (
        "CANCELLATION_COMMAND_FAILED"
    )
    host.close()
def test_replace_existing_daemon_uses_windows_process_tree_termination(
    monkeypatch, tmp_path_factory,
):
    from backend.core.daemon import client as client_module

    pid_path = tmp_path_factory / "daemon.pid"
    pid_path.write_text("12345", encoding="utf-8")
    client = DaemonClient("replace-test", existing_policy="replace")
    monkeypatch.setattr(client, "_pid_path", lambda: pid_path)
    monkeypatch.setattr(client_module.sys, "platform", "win32")
    monkeypatch.setattr(client_module.os, "kill", lambda pid, signal_number: None)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")

    monkeypatch.setattr(client_module.subprocess, "run", run)
    client._kill_existing()

    assert calls[0][0] == ["taskkill", "/PID", "12345", "/T", "/F"]
    assert not pid_path.exists()
