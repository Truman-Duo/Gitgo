from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
import pytest
from backend.core.storage.runtime import StorageRuntime
from backend.core.daemon.command_admission import fingerprint
from backend.core.daemon import dispatch
from backend.core.messaging import project_fact_to_trace
from backend.core.architecture_monitor import source_metrics, findings
from backend.core.loop.protocol_tools.registry import Factory, install_factories


def test_command_receipt_survives_restart_and_prevents_duplicate_effect(tmp_path_factory, monkeypatch):
    root = tmp_path_factory
    workspace = root / "work"
    workspace.mkdir()
    events, effects = [], []
    monkeypatch.setattr(dispatch, "_emit_global", events.append)
    def handler(cmd, session, project, context, emit):
        effects.append(cmd["command_id"])
        emit({"event": "command_result", "cmd": "dispatch_tool", "result": {"committed": True}})
    monkeypatch.setitem(dispatch.COMMAND_HANDLERS, "dispatch_tool", handler)
    cmd = {"cmd": "dispatch_tool", "command_id": "one", "tool": "write", "request_id": "first"}
    storage = StorageRuntime(workspace, state_home=root / "state")
    dispatch._handle_command(cmd, None, None, {"storage": storage})
    storage.close()
    storage = StorageRuntime(workspace, state_home=root / "state")
    try:
        dispatch._handle_command({**cmd, "request_id": "retry"}, None, None, {"storage": storage})
        assert effects == ["one"]
        assert events[-1]["request_id"] == "retry" and events[-1]["result"]["committed"]
        unknown = {**cmd, "command_id": "unknown"}
        storage.claim_command("unknown", fingerprint(unknown))
        dispatch._handle_command(unknown, None, None, {"storage": storage})
        assert events[-1]["error"] == "COMMAND_RECOVERY_REQUIRED"
        assert effects == ["one"]
        with pytest.raises(ValueError, match="IDENTITY_CONFLICT"):
            storage.claim_command("one", "0" * 64)
        storage.maintain_cas()
        assert storage.claim_command("one", fingerprint(cmd))["result"]["result"]["committed"]
    finally:
        storage.close()


def test_concurrent_command_admission_has_single_winner(tmp_path_factory):
    root = tmp_path_factory
    workspace = root / "work"
    workspace.mkdir()
    with StorageRuntime(workspace, state_home=root / "state") as storage:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: storage.claim_command("race", "a" * 64), range(8)))
        assert sum(result["admitted"] for result in results) == 1


def test_process_deletion_purges_command_body_but_retains_replay_tombstone(tmp_path_factory):
    root = tmp_path_factory
    workspace = root / "work"
    workspace.mkdir()
    with StorageRuntime(workspace, state_home=root / "state") as storage:
        storage.save_agent_checkpoint({"process_id": "proc", "session_id": "sess",
            "status": "running", "messages": [], "runtime_state": {}})
        storage.claim_command("private", "b" * 64, process_id="proc")
        storage.complete_command("private", "b" * 64, {"result": {"body": "private conversation"}})
        storage.delete_process_session("proc")
        receipt = storage.claim_command("private", "b" * 64)
        assert not receipt["admitted"] and receipt["deleted"]
        assert receipt["result_ref"] is None and "result" not in receipt


def test_fact_observer_replay_deduplicates_without_payload_copy(tmp_path_factory):
    root = tmp_path_factory
    workspace = root / "work"
    workspace.mkdir()
    with StorageRuntime(workspace, state_home=root / "state") as storage:
        fact = {"stream_id": "process:test", "stream_sequence": 1, "occurred_at": "2026-10-10T00:00:00+00:00",
                "process_id": "test", "task_id": "task", "kind": "checkpoint_committed", "producer": "StateRepository",
                "event_id": "fact", "correlation_id": "task", "causation_id": "command", "payload": {"secret": "not copied"}}
        project_fact_to_trace(storage, fact)
        project_fact_to_trace(storage, fact)
        result = storage.read_trace_events("runtime-facts:process:test")
        assert len(result["events"]) == 1
        assert "secret" not in json.dumps(result)


def test_factory_dependencies_resolve_without_list_order_and_refuse_missing():
    ctx = SimpleNamespace(a=None, b=None)
    consumer = SimpleNamespace(__name__="consumer", install=lambda c: setattr(c, "b", c.a + 1))
    producer = SimpleNamespace(__name__="producer", install=lambda c: setattr(c, "a", 1))
    install_factories(ctx, [Factory(consumer, ("a",), ("b",)), Factory(producer, (), ("a",))])
    assert ctx.b == 2
    with pytest.raises(RuntimeError, match="DEPENDENCY_UNRESOLVED"):
        install_factories(SimpleNamespace(a=None), [Factory(consumer, ("a",))])


def test_architecture_static_boundary_and_complexity_growth(tmp_path_factory):
    root = tmp_path_factory
    path = root / "tools.py"
    path.write_text("from backend.core.native_host import NativeHost\ndef work(x):\n" + "    if x: x -= 1\n" * 35)
    metrics, issues = source_metrics(root)
    rules = [{"source": "tools", "forbidden": ["backend.core.native_host"]}]
    alerts = findings(metrics, {}, rules)
    assert not issues
    assert {a["rule"] for a in alerts} == {"branch_complexity", "import_boundary"}
    assert all(a["rule"] != "branch_complexity" for a in findings(metrics, metrics, rules))
    path.write_text(path.read_text() + "    if x: x -= 1\n" * 10)
    assert any(a["rule"] == "branch_complexity" for a in findings(source_metrics(root)[0], metrics, rules))
