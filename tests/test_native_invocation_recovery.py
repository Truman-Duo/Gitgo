"""Persistent lifecycle evidence; protocol fault injection is not OS isolation."""
import json
import os
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.core.loop.execution_contract import NativeInvocation, NATIVE_FILE_READ
from backend.core.storage import StorageRuntime
from backend.core.storage.native_invocations import (
    NAMESPACE, NativeInvocationRecord, pending_native_invocations,
)


@pytest.fixture
def bound_store(tmp_path_factory):
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    with StorageRuntime(workspace, state_home=tmp_path_factory / "private-state") as storage:
        yield workspace, storage


def invocation(workspace):
    return NativeInvocation.compile("read_file", {"_workspace": str(workspace), "path": "secret-argument"},
                                    30, owner=("process", "task"), registration="registered-version")


def test_cleanup_guidance_preserves_already_uncertain_effects():
    from backend.core.sandbox import SandboxDenied
    denial = SandboxDenied("SANDBOX_CLEANUP_PENDING", "retained resources")
    denial.effect_state = "ambiguous"
    result = denial.result()
    assert result["effect_state"] == "ambiguous"
    assert result["error_info"]["next_actions"][0]["action"] == "inspect_native_ownership"


def test_launch_facts_use_project_authority_and_exclude_arguments(bound_store):
    workspace, storage = bound_store
    record = NativeInvocationRecord(storage, workspace, invocation(workspace), execution_id="call", session_id="session")
    try:
        record.prepare()
        before = pending_native_invocations(storage, include_active=True)
        assert before[0]["code"] == "NATIVE_LAUNCH_UNCONFIRMED"
        assert pending_native_invocations(storage) == []  # Only a live local owner is skipped.
        record.started(os.getpid())
        raw = storage.read_blob(record.ref).decode()
        assert "secret-argument" not in raw and str(workspace) not in raw
        assert record.record["execution_basis"] == invocation(workspace).digest
        assert record.record["host_instance"] and record.record["registration"] == "registered-version"
        record.finish({"started": True, "cleanup": {"complete": True, "mode": "test-only"}, "exit_code": 0})
        assert not record.failed and pending_native_invocations(storage, include_active=True) == []
        assert storage.get_state_ref(NAMESPACE, record.key) is None
        saved = storage.get_state_ref(NAMESPACE, "history/" + record.record["invocation_id"])
        assert json.loads(storage.read_blob(saved))["phase"] == "finished"
    finally:
        record.finish({"started": False, "launch_state": "not_started"})


@pytest.mark.parametrize("phase", ["launching", "started", "finished"])
def test_host_kill_leaves_read_only_debt_older_than_checkpoint(bound_store, phase):
    """Real Host-process termination around a protocol-injected launch window.

    This exercises real SQLite/CAS and process death, not AppContainer isolation.
    """
    workspace, storage = bound_store
    script = f'''
import os,sys,time
sys.path.insert(0,{str(Path.cwd())!r})
from pathlib import Path
from backend.core.storage import StorageRuntime
from backend.core.loop.execution_contract import NativeInvocation
from backend.core.storage.native_invocations import NativeInvocationRecord
with StorageRuntime({str(workspace)!r},state_home={str(storage.paths.state_home)!r}) as store:
    call=NativeInvocation.compile('read_file',{{'_workspace':{str(workspace)!r},'path':'note.txt'}},30,owner=('process','task'),registration='fixture')
    record=NativeInvocationRecord(store,Path({str(workspace)!r}),call,execution_id='killed-host')
    record.prepare()
    if {phase!r} != 'launching':record.started(os.getpid())
    if {phase!r} == 'finished':record.finish({{'started':True,'cleanup':{{'complete':False,'mode':'injected'}}}})
    print('ready',flush=True)
    time.sleep(60)
'''
    child = subprocess.Popen([sys.executable, "-B", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # communicate timeout captures a startup failure without an unbounded readline.
        deadline = time.monotonic() + 15
        while not pending_native_invocations(storage) and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert pending_native_invocations(storage), child.communicate(timeout=2) if child.poll() is not None else "no persisted intent"
        if phase != "launching":
            while time.monotonic() < deadline:
                rows = storage.list_state_refs(NAMESPACE, prefix="pending/")
                current = json.loads(storage.read_blob(rows[0]["value_ref"]))
                if current["phase"] == phase:
                    break
                time.sleep(.02)
            assert current["phase"] == phase
        child.kill()
        child.communicate(timeout=10)
        from backend.core.loop.recovery import inspect_post_checkpoint_invocations
        findings = inspect_post_checkpoint_invocations(workspace, "process",
            datetime.now(timezone.utc).isoformat(), storage=storage, paths=storage.paths)
        expected = "NATIVE_LAUNCH_UNCONFIRMED" if phase == "launching" else "NATIVE_CLEANUP_UNCONFIRMED"
        assert any(item["code"] == expected for item in findings)
        storage.close()
        with StorageRuntime(workspace, state_home=storage.paths.state_home) as reopened:
            for _ in range(2):
                assert pending_native_invocations(reopened)[0]["code"] == expected
                with pytest.raises(Exception, match="Unsettled native"):
                    NativeInvocationRecord(reopened, workspace, invocation(workspace)).prepare()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        child.stdout.close()
        child.stderr.close()


@pytest.mark.parametrize("evidence_failure", [False, True])
def test_read_only_pipeline_records_launch_before_os_and_preserves_real_result(tmp_path_factory, monkeypatch, runner_transport_only, evidence_failure):
    from backend.core.loop.agent_tool import AgentTool
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core import sandbox
    from backend.core.storage import get_storage
    (tmp_path_factory / "note.txt").write_text("retained read output")
    storage = get_storage(tmp_path_factory)
    process = AgentProcessManager().fork(parent_id=None, role="worker", max_steps=3,
        workspace_path=str(tmp_path_factory), ring_level=RingLevel.RING_0, tool_registry=ToolRegistry(["read_file"]))
    ctx = ExecutionContext(process=process, session=process.session, workspace_path=str(tmp_path_factory), storage=storage)
    events = []
    ctx.event_bus.subscribe("ToolResultReady", lambda event: events.append(event.data))
    if evidence_failure:
        monkeypatch.setattr(storage, "commit_state_revision", lambda *a, **k: (_ for _ in ()).throw(OSError("injected final failure")))
    tool = AgentTool("read_file", "read", {}, lambda _: pytest.fail("no inline read"),
                     read_only=True, isolated=True, execution_contract=NATIVE_FILE_READ)
    launch = sandbox.sandbox_popen
    def observed_launch(*args, **kwargs):
        assert pending_native_invocations(storage, include_active=True)[0]["state"] == "launching"
        return launch(*args, **kwargs)
    monkeypatch.setattr(sandbox, "sandbox_popen", observed_launch)
    actual = ToolPipeline().execute({"name": tool.name, "args": {"path": "note.txt"}}, tool, ctx, "readonly")
    assert not actual.is_error and "retained read output" in str(actual.data), actual.error
    assert len(actual.receipt["native_invocations"]) == 1
    if evidence_failure:
        assert actual.receipt["evidence_state"] == "failed"
        assert events[0]["diagnostics"]["evidence_state"] == "failed"
        assert process.recovery["resume_forbidden"]
        assert pending_native_invocations(storage, include_active=True)
    else:
        assert pending_native_invocations(storage, include_active=True) == []


def test_final_evidence_failure_keeps_read_result_but_blocks_new_native(tmp_path_factory, monkeypatch, runner_transport_only):
    from backend.core.loop.process_tool_runner import ProcessToolRunner
    from backend.core.storage import get_storage
    storage = get_storage(tmp_path_factory)
    (tmp_path_factory / "note.txt").write_text("retained read output")
    monkeypatch.setattr(storage, "commit_state_revision", lambda *a, **k: (_ for _ in ()).throw(OSError("injected full disk")))
    result = ProcessToolRunner().run("read_file", {"_workspace": str(tmp_path_factory), "path": "note.txt"})
    assert result.success and "retained read output" in str(result.data)
    assert result.cleanup["complete"] is True and result.evidence_state == "failed"
    assert pending_native_invocations(storage)[0]["code"] == "NATIVE_CLEANUP_UNCONFIRMED"
    second = ProcessToolRunner().run("read_file", {"_workspace": str(tmp_path_factory), "path": "note.txt"})
    assert not second.started and second.data["error"] == "SANDBOX_CLEANUP_PENDING"


def test_uncommitted_intent_never_enters_os(tmp_path_factory, monkeypatch, runner_transport_only):
    from backend.core.loop.process_tool_runner import ProcessToolRunner
    from backend.core.storage import get_storage
    from backend.core import sandbox
    storage = get_storage(tmp_path_factory)
    monkeypatch.setattr(storage, "compare_state_ref", lambda *a, **k: (_ for _ in ()).throw(OSError("injected intent failure")))
    monkeypatch.setattr(sandbox, "sandbox_popen", lambda *a, **k: pytest.fail("OS must not start"))
    result = ProcessToolRunner().run("read_file", {"_workspace": str(tmp_path_factory), "path": "note.txt"})
    assert not result.started and result.data["error"] == "INVOCATION_JOURNAL_UNAVAILABLE"


def test_finished_fact_survives_pending_retirement_failure(bound_store, monkeypatch):
    workspace, storage = bound_store
    record = NativeInvocationRecord(storage, workspace, invocation(workspace))
    record.prepare()
    monkeypatch.setattr(storage, "release_state_ref", lambda *a, **k: (_ for _ in ()).throw(OSError("injected index removal failure")))
    record.finish({"started": True, "cleanup": {"complete": True, "mode": "test-only"}})
    assert not record.failed and record.retirement_pending
    # Terminal facts, not the mere presence of an index entry, establish retirement.
    assert pending_native_invocations(storage) == []


def test_missing_or_corrupt_native_evidence_fails_closed(bound_store):
    _, storage = bound_store
    ref = storage.put_blob(b'{"schema_version":999}', media_type="application/json")
    storage.put_state_ref(NAMESPACE, "pending/invalid", ref)
    assert pending_native_invocations(storage)[0]["code"] == "INVOCATION_EVIDENCE_UNAVAILABLE"


def test_checkpoint_cannot_clear_prior_read_only_native_debt(tmp_path_factory):
    from tests.test_cross_daemon_recovery import _running_supervisor
    from backend.core.loop.manager import AgentProcessManager, SessionStore
    from backend.core.loop.recovery import restore_incomplete_processes
    from backend.core.loop.models import ProcessStatus
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    _, process = _running_supervisor(workspace)
    state_home = tmp_path_factory / "state"
    with closing(SessionStore(str(workspace), state_home=state_home)) as store:
        call = NativeInvocation.compile("read_file", {"_workspace": str(workspace)}, 30,
            owner=(process.process_id, process.active_task_id), registration="fixture")
        record = NativeInvocationRecord(store._storage, workspace, call, execution_id="readonly-debt")
        record.prepare()
        record.finish({"started": True, "cleanup": {"complete": False, "mode": "injected"}})
        store.save_process_checkpoint(process)  # Checkpoint newer than the unresolved native fact.
    with closing(SessionStore(str(workspace), state_home=state_home)) as reopened:
        manager = AgentProcessManager()
        restore_incomplete_processes(reopened, manager, workspace)
        restored = manager.get(process.process_id)
        assert restored.status is ProcessStatus.RECOVERY_REVIEW_REQUIRED
        assert restored.recovery["resume_forbidden"]
        assert restored.approval_grants == []
        assert any(reason.get("execution_id") == "readonly-debt" for reason in restored.recovery["reasons"])


def test_old_external_journal_cleanup_is_not_filtered_as_observation(bound_store):
    from backend.core.storage.invocation_journal import invocation_journal_root
    from backend.core.loop.recovery import inspect_post_checkpoint_invocations
    workspace, storage = bound_store
    root = invocation_journal_root(workspace, paths=storage.paths)
    root.mkdir(parents=True)
    (root / "old-read.json").write_text(json.dumps(dict(process_id="process", execution_id="old-read",
        effect="read", state="finished", effect_state="not_committed", updated_at=1,
        runtime_cleanup={"complete": False, "mode": "injected"})))
    findings = inspect_post_checkpoint_invocations(workspace, "process", datetime.now(timezone.utc).isoformat(),
        storage=storage, paths=storage.paths)
    assert [item["code"] for item in findings] == ["NATIVE_CLEANUP_UNCONFIRMED"]


def test_new_approval_cannot_bypass_debt_and_preflight_does_not_spend_it(bound_store):
    from backend.core.loop.agent_tool import AgentTool, ApprovalMode
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.execution_contract import NATIVE_PROCESS, HOST_COMPUTE
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
    from backend.core.loop.tool_pipeline import ToolPipeline
    workspace, storage = bound_store
    record = NativeInvocationRecord(storage, workspace, invocation(workspace))
    record.prepare()
    record.finish({"started": True, "cleanup": {"complete": False, "mode": "injected"}})
    tool = AgentTool('native_fixture', 'fixture', {}, lambda _: pytest.fail('must not execute'),
        execution_contract=NATIVE_PROCESS, approval=ApprovalMode.ASK, approval_per_invocation=True)
    process = AgentProcess('new-process', 'worker', RingLevel.RING_3, active_task_id='new-task',
        session=AgentSession(), worktree_path=str(workspace), tool_registry=ToolRegistry([tool.name]))
    create_permission_request(process, {'tool_name': tool.name, 'arguments': {},
        'resource': 'capability://native_fixture', 'purpose': 'Approve a new test operation'}, {tool.name: tool}, str(workspace))
    grant = grant_from_decision(process, process.pending_decision, 'allow_once')
    assert grant
    process.pending_decision = None
    ctx = ExecutionContext(process, process.session, str(workspace), storage=storage)
    result = ToolPipeline().execute({'name': tool.name, 'args': {}}, tool, ctx, 'new-call')
    assert result.is_error and 'SANDBOX_CLEANUP_PENDING' in result.error
    assert grant['remaining_uses'] == 1 and process.recovery['resume_forbidden']
    # Recovery inspection and Host computation do not inherit a native-process ban.
    inspect = AgentTool('inspection_fixture', 'inspect', {}, lambda _: {'observed': True},
                        execution_contract=HOST_COMPUTE)
    process.tool_registry = ToolRegistry([inspect.name])
    observed = ToolPipeline().execute({'name': inspect.name, 'args': {}}, inspect, ctx, 'inspect')
    assert not observed.is_error and observed.data == {'observed': True}
