"""Adversarial state audit with repaired read-coherence regressions.
Original pre-fix failures remain in the private audit evidence.

Morphz reference properties (not its schema): context_db.rs tests for stale
writers, disjoint writes, exact idempotency, coherent reads and reopen; permission
checks must reevaluate current denial. Child probes import this Alpha3 checkout.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import threading

import pytest

from backend.core.storage import StorageRuntime
from backend.core.storage.lifecycle import RevisionConflict
from backend.core.storage.runtime_events import read_facts
from backend.core.workbench.sheets import SheetStore

REPO = Path(__file__).resolve().parents[1]


def snapshot(marker, revision=0):
    return {
        "process_id": "audit-root", "session_id": "audit-session", "task_id": "audit-task",
        "expected_revision": revision, "status": "running", "role": "worker",
        "messages": [{"role": "user", "content": marker, "message_type": "conversation"}],
        "session_metadata": {"context_memo": {"marker": marker}},
        "runtime_state": {"marker": marker},
    }


def child(code):
    # Portable Python has an installation _pth: PYTHONPATH alone does not select
    # this worktree. Assert source identity inside every child before the probe.
    prelude = (
        f"import sys;sys.path.insert(0,{str(REPO)!r});"
        "from pathlib import Path;import backend.core.storage.runtime as source;"
        f"assert Path(source.__file__).resolve().is_relative_to(Path({str(REPO)!r})),source.__file__;\n"
    )
    return subprocess.run([sys.executable, "-B", "-c", prelude + code],
                          cwd=REPO, capture_output=True, text=True, timeout=45)


def test_checkpoint_race_has_one_winner_and_no_loser_fact(tmp_path_factory):
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as left, StorageRuntime(root, state_home=root / "state") as right:
        left.save_agent_checkpoint(snapshot("initial"))
        barrier = threading.Barrier(2)
        def write(storage, marker):
            barrier.wait(timeout=10)
            try:
                return storage.save_agent_checkpoint(snapshot(marker, 1)).revision, marker
            except RevisionConflict:
                return "conflict", marker
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(write, left, "left"), pool.submit(write, right, "right")]
            results = [future.result(timeout=20) for future in futures]
        assert sum(revision == "conflict" for revision, _ in results) == 1
        winner = next(marker for revision, marker in results if revision == 2)
        state = right.load_agent_process_state("audit-root")
        assert state["runtime_state"]["marker"] == state["session"]["messages"][0]["content"] == winner
        assert [fact["payload"]["revision"] for fact in read_facts(left, "audit")] == [1, 2]


def test_independent_sheet_heads_do_not_create_false_conflicts(tmp_path_factory):
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as left, StorageRuntime(root, state_home=root / "state") as right:
        a = SheetStore(left, session_id="session", process_id="process")
        b = SheetStore(right, session_id="session", process_id="process")
        first = a.create("first", [{"id": "cell", "text": "first-old"}])
        second = a.create("second", [{"id": "cell", "text": "second-old"}])
        barrier = threading.Barrier(2)
        def revise(store, document, text):
            barrier.wait(timeout=10)
            return store.revise(document["sheet_id"], document["revision"], [{"id": "cell", "text": text}])
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(revise, a, first, "first-new"), pool.submit(revise, b, second, "second-new")]
            committed = [future.result(timeout=20) for future in futures]
        assert a.open(first["sheet_id"])["revision"] == committed[0]["revision"]
        assert b.open(second["sheet_id"])["revision"] == committed[1]["revision"]
        assert a.open(first["sheet_id"], first["revision"])["cells"][0]["source"] == "first-old"


def test_command_claim_races_across_connections_have_one_winner(tmp_path_factory):
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as left, StorageRuntime(root, state_home=root / "state") as right:
        barrier = threading.Barrier(2)
        def claim(store):
            barrier.wait(timeout=10)
            return store.claim_command("same-key", "a" * 64)
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(claim, left), pool.submit(claim, right)]
            results = [future.result(timeout=20) for future in futures]
        assert sum(result["admitted"] for result in results) == 1
        with pytest.raises(ValueError, match="COMMAND_IDENTITY_CONFLICT"):
            right.claim_command("same-key", "b" * 64)


@pytest.mark.parametrize("phase", ["after_claim", "after_effect", "after_receipt"])
def test_hard_exit_command_recovery_does_not_repeat_effect(tmp_path_factory, monkeypatch, phase):
    from backend.core.daemon import dispatch
    from backend.core.daemon.command_admission import fingerprint
    root = tmp_path_factory
    effect = root / "physical-effect.txt"
    command = {"cmd": "dispatch_tool", "command_id": "audit-command", "tool": "audit-marker"}
    code = f'''
import os
from backend.core.storage import StorageRuntime
from backend.core.daemon.command_admission import fingerprint
runtime=StorageRuntime({str(root)!r},state_home={str(root/'state')!r})
command={command!r}
runtime.claim_command(command['command_id'],fingerprint(command))
if {phase!r}=='after_claim': os._exit(81)
Path({str(effect)!r}).write_text('one',encoding='utf-8')
if {phase!r}=='after_effect': os._exit(81)
runtime.complete_command(command['command_id'],fingerprint(command),{{'result':{{'committed':True}}}})
os._exit(81)
'''
    result = child(code)
    assert result.returncode == 81, result.stderr
    events, calls = [], []
    monkeypatch.setattr(dispatch, "_emit_global", events.append)
    def handler(cmd, session, project, context, emit):
        calls.append(cmd["command_id"])
        emit({"event": "command_result", "cmd": "dispatch_tool", "result": {"committed": True}})
    monkeypatch.setitem(dispatch.COMMAND_HANDLERS, "dispatch_tool", handler)
    with StorageRuntime(root, state_home=root / "state") as restored:
        dispatch._handle_command(command, None, None, {"storage": restored})
        assert calls == []
        assert effect.exists() == (phase != "after_claim")
        if phase == "after_receipt":
            assert events[-1]["result"]["committed"]
        else:
            assert events[-1]["error"] == "COMMAND_RECOVERY_REQUIRED"
        dispatch._handle_command({**command, "command_id": "fresh-control"}, None, None, {"storage": restored})
        assert calls == ["fresh-control"]  # negative control: real new commands still execute


def test_process_killed_inside_checkpoint_rolls_back_state_and_outbox(tmp_path_factory):
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as storage:
        storage.save_agent_checkpoint(snapshot("before"))
    code = f'''
import os
from backend.core.storage import StorageRuntime
storage=StorageRuntime({str(root)!r},state_home={str(root/'state')!r})
storage._state.create_function('audit_die',0,lambda:os._exit(82))
storage._state.execute("CREATE TEMP TRIGGER audit_kill BEFORE INSERT ON runtime_outbox BEGIN SELECT audit_die(); END")
storage.save_agent_checkpoint({snapshot('must-not-survive',1)!r})
raise AssertionError('failure injection did not fire')
'''
    result = child(code)
    assert result.returncode == 82, result.stderr
    with StorageRuntime(root, state_home=root / "state") as restored:
        state = restored.load_agent_process_state("audit-root")
        assert state["process"]["state_revision"] == 1
        assert state["runtime_state"]["marker"] == state["session"]["messages"][0]["content"] == "before"
        assert len(read_facts(restored, "audit")) == 1
        assert not restored._state.in_transaction


def test_session_read_does_not_mix_old_metadata_with_new_messages(tmp_path_factory):
    """Force a real WAL commit between two SELECTs; no fabricated return data."""
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as writer, StorageRuntime(root, state_home=root / "state") as reader:
        writer.save_agent_checkpoint(snapshot("before"))
        control = reader.load_agent_session_state("audit-root")
        assert control["context_memo"]["marker"] == control["messages"][0]["content"] == "before"
        injected = []
        def interleave(sql):
            if not injected and "SELECT content_ref FROM messages" in " ".join(sql.split()):
                injected.append(True)
                writer.save_agent_checkpoint(snapshot("after", 1))
        reader._state.set_trace_callback(interleave)
        try:
            observed = reader.load_agent_session_state("audit-root")
        finally:
            reader._state.set_trace_callback(None)
        assert injected == [True]
        assert writer.process_revision("audit-root") == 2
        assert observed["context_memo"]["marker"] == observed["messages"][0]["content"], observed


def test_process_read_does_not_mix_old_session_with_new_process(tmp_path_factory, monkeypatch):
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as writer, StorageRuntime(root, state_home=root / "state") as reader:
        writer.save_agent_checkpoint(snapshot("before"))
        original = reader.load_agent_session_state
        def interleave(process_id):
            saved = original(process_id)
            writer.save_agent_checkpoint(snapshot("after", 1))
            return saved
        monkeypatch.setattr(reader, "load_agent_session_state", interleave)
        observed = reader.load_agent_process_state("audit-root")
        assert writer.process_revision("audit-root") == 2
        assert observed["process"]["state_revision"] == 1
        assert observed["runtime_state"]["marker"] == observed["session"]["messages"][0]["content"] == "before", observed
        assert not reader._state.in_transaction


def test_current_tool_denial_wins_over_previously_issued_exact_grant(tmp_path_factory):
    from backend.core.loop.agent_tool import AgentTool, ApprovalMode, ToolEffect
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.event_bus import EventBus
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
    from backend.core.loop.tool_pipeline import ToolPipeline
    root = tmp_path_factory
    process = AgentProcessManager().fork(parent_id=None, role="worker", tool_registry=ToolRegistry(["audit_tool"]),
        max_steps=8, ring_level=RingLevel.RING_3, workspace_path=str(root), task_id="audit-task", actor_kind="worker", task_kind="action")
    calls = []
    from backend.core.loop.execution_contract import HOST_COMPUTE
    tool = AgentTool(name="audit_tool", execution_contract=HOST_COMPUTE, description="test capability", parameters={"type":"object","properties":{}},
        execute=lambda args: calls.append(args) or {"ok":True}, effect=ToolEffect.READ,
        approval=ApprovalMode.ASK, approval_per_invocation=True)
    create_permission_request(process, {"purpose":"exercise audit fixture", "tool_name":"audit_tool", "arguments":{}},
        {"audit_tool":tool}, str(root))
    grant = grant_from_decision(process, process.pending_decision, "allow_once")
    assert grant and grant["remaining_uses"] == 1
    process.pending_decision = None
    tool.approval = ApprovalMode.DENY
    context = ExecutionContext(process=process, session=process.session, workspace_path=str(root),
        event_bus=EventBus(), cancellation=process.cancellation_event)
    result = ToolPipeline().execute({"name":"audit_tool","args":{}},tool,context,"audit",0)
    assert result.is_error and result.diagnostics["code"] == "TOOL_DENIED"
    assert calls == [] and grant["remaining_uses"] == 1


def test_snapshot_read_leaves_outer_transaction_owned_by_caller(tmp_path_factory):
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as storage:
        storage.save_agent_checkpoint(snapshot("before"))
        with storage._state_lock:
            storage._state.execute("BEGIN IMMEDIATE")
            storage._state.execute("UPDATE session_processes SET role='uncommitted' WHERE process_id='audit-root'")
            observed = storage.load_agent_process_state("audit-root")
            assert observed["process"]["role"] == "uncommitted"
            assert storage._state.in_transaction
            storage._state.execute("ROLLBACK")
        assert storage.load_agent_process_state("audit-root")["process"]["role"] == "worker"
        assert not storage._state.in_transaction


def test_snapshot_read_failure_releases_only_its_own_transaction(tmp_path_factory):
    from backend.core.storage.support import state_read_snapshot
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as writer, StorageRuntime(root, state_home=root / "state") as reader:
        writer.save_agent_checkpoint(snapshot("before"))
        with pytest.raises(sqlite3.OperationalError):
            with state_read_snapshot(reader._state, reader._state_lock):
                assert reader.load_agent_session_state("audit-root")["messages"][0]["content"] == "before"
                reader._state.execute("SELECT non_existing_column FROM session_processes")
        assert not reader._state.in_transaction
        writer.save_agent_checkpoint(snapshot("after", 1))
        assert reader.load_agent_process_state("audit-root")["session"]["messages"][0]["content"] == "after"
        assert not reader._state.in_transaction


def test_latest_root_selection_and_loading_share_one_snapshot(tmp_path_factory, monkeypatch):
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as writer, StorageRuntime(root, state_home=root / "state") as reader:
        writer.save_agent_checkpoint(snapshot("before"))
        original = reader.load_agent_process_state
        injected = []
        def interleave(process_id):
            if not injected:
                injected.append(True)
                writer.save_agent_checkpoint(snapshot("after", 1))
            return original(process_id)
        monkeypatch.setattr(reader, "load_agent_process_state", interleave)
        observed = reader.load_latest_root_process_state()
        assert writer.process_revision("audit-root") == 2
        assert observed["process"]["state_revision"] == 1
        assert observed["runtime_state"]["marker"] == observed["session"]["messages"][0]["content"] == "before"
        assert not reader._state.in_transaction
        assert reader.load_latest_root_process_state()["runtime_state"]["marker"] == "after"


def test_appended_event_does_not_enter_an_older_session_snapshot(tmp_path_factory):
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as writer, StorageRuntime(root, state_home=root / "state") as reader:
        writer.save_agent_checkpoint(snapshot("before"))
        injected = []
        def interleave(sql):
            if not injected and "SELECT sequence, event_type, payload_ref FROM session_events" in " ".join(sql.split()):
                injected.append(True)
                writer.append_session_event("audit-root", "message_append", {"message": {"role": "user", "content": "later"}})
        reader._state.set_trace_callback(interleave)
        try:
            observed = reader.load_agent_session_state("audit-root")
        finally:
            reader._state.set_trace_callback(None)
        assert injected == [True]
        assert [m["content"] for m in observed["messages"]] == ["before"]
        assert [m["content"] for m in reader.load_agent_session_state("audit-root")["messages"]] == ["before", "later"]
        assert not reader._state.in_transaction
