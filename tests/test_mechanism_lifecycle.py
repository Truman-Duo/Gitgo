"""Activation races, exact continuation and existing authority boundaries."""
from copy import deepcopy
from types import SimpleNamespace
import threading
import time

import pytest

from backend.core.loop import mechanisms
from backend.core.loop.agent_tool import AgentTool, ToolEffect, ApprovalMode
from backend.core.loop.event_bus import EventBus
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tool_batch_runtime import ToolBatchRuntime
from backend.core.loop.tools import ToolRegistry


class Store:
    def __init__(self):
        self.saved = []
        self.error = None

    def deleted_process_ids(self):
        return set()

    def save_process_checkpoint(self, process):
        if self.error:
            raise self.error
        self.saved.append({"mechanism_instances": deepcopy(process.session.mechanism_instances)})


def runtime(root):
    manager = AgentProcessManager()
    manager.session_store = Store()
    process = manager.fork(None, "worker", ToolRegistry(["unit"]), 8, RingLevel.RING_3,
                           workspace_path=str(root), task_id="work", actor_kind="worker")
    return manager, process


def tool(calls, *, effect=ToolEffect.PROCESS, execute=None):
    return AgentTool(name="unit", description="test work unit",
                     parameters={"type": "object", "properties": {"value": {"type": "string"}}},
                     execution_contract=data_broker("test.fixture"), effect=effect,
                     read_only=effect == ToolEffect.READ,
                     execute=execute or (lambda args: calls.append(args.get("value")) or {"ok": True}))


def context(process, root):
    return ExecutionContext(process=process, session=process.session,
                            workspace_path=str(root), event_bus=EventBus(),
                            cancellation=process.cancellation_event)


def execute(process, candidate, root, value="one", execution_id="batch", index=0):
    return ToolPipeline().execute({"name": "unit", "args": {"value": value}}, candidate,
                                  context(process, root), execution_id, index)


def test_revision_commit_is_cas_and_failed_prepare_leaves_effective_mode(tmp_path_factory):
    manager, process = runtime(tmp_path_factory)
    store = manager.session_store
    assert mechanisms.snapshot(process)["mode"] == "auto"
    revision = mechanisms.activate(process, "manual", 0, store)
    assert revision["revision"] == 1
    assert store.saved[-1]["mechanism_instances"][mechanisms.ADVANCEMENT]["parameters"] == {"mode": "manual"}
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        mechanisms.activate(process, "auto", 0, store)
    store.error = OSError("disk full")
    with pytest.raises(OSError):
        mechanisms.activate(process, "auto", 1, store)
    assert mechanisms.snapshot(process) == revision
    assert len([e for e in process.session.host_ledger if e["event"] == "mechanism_revision_activated"]) == 1


def test_children_bind_one_instance_and_unrelated_tasks_keep_their_mode(tmp_path_factory):
    manager, process = runtime(tmp_path_factory)
    child = manager.fork(process.process_id, "child", ToolRegistry(["unit"]), 4, RingLevel.RING_3,
                         workspace_path=str(tmp_path_factory), task_id="child")
    _, other = runtime(tmp_path_factory)
    mechanisms.activate(process, "manual", 0, manager.session_store)
    assert mechanisms.snapshot(child)["mode"] == "manual"
    assert mechanisms.snapshot(child)["scope_id"] == process.session.session_id
    assert "mechanisms" not in child.runtime_preferences
    assert mechanisms.snapshot(other)["mode"] == "auto"


def test_manual_read_continues_and_exact_mutation_requires_fresh_single_use_ticket(tmp_path_factory):
    manager, process = runtime(tmp_path_factory)
    mechanisms.activate(process, "manual", 0, manager.session_store)
    calls = []
    assert not execute(process, tool(calls, effect=ToolEffect.READ), tmp_path_factory).is_error
    assert calls == ["one"]
    candidate = tool(calls)
    ctx = context(process, tmp_path_factory)
    result = ToolPipeline().execute({"name": "unit", "args": {"value": "one"}}, candidate, ctx, "batch")
    assert result.suspended, result
    unit = ctx.artifacts["advancement_pause"]
    process.pending_tool_batch = {"tool_calls": [{"name": "unit", "args": {"value": "one"}}]}
    pending = mechanisms.manual_decision(process, unit)
    mechanisms.approve_unit(process, pending, "run_once")
    assert execute(process, candidate, tmp_path_factory, "changed").suspended
    assert not execute(process, candidate, tmp_path_factory).is_error
    assert calls == ["one", "one"]
    assert execute(process, candidate, tmp_path_factory).suspended
    mechanisms.activate(process, "auto", 1, manager.session_store)
    mechanisms.activate(process, "manual", 2, manager.session_store)
    with pytest.raises(ValueError, match="REVISION_STALE"):
        mechanisms.approve_unit(process, pending, "run_once")


def test_activation_does_not_cancel_started_effect_and_fences_next_call(tmp_path_factory):
    manager, process = runtime(tmp_path_factory)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def run(args):
        entered.set()
        assert release.wait(4)
        calls.append(args["value"])
        return {"ok": True}
    candidate = tool(calls, execute=run)
    results = []
    thread = threading.Thread(target=lambda: results.append(execute(process, candidate, tmp_path_factory)))
    thread.start()
    try:
        assert entered.wait(4)
        mechanisms.activate(process, "manual", 0, manager.session_store)
        assert not process.cancellation_event.is_set()
    finally:
        release.set(); thread.join(5)
    assert not thread.is_alive()
    assert calls == ["one"]
    assert not results[0].is_error
    assert results[0].receipt["mechanism_revision"]["revision"] == 0
    assert execute(process, candidate, tmp_path_factory, "two", index=1).suspended


def test_activation_waits_for_commit_before_new_admission(tmp_path_factory):
    manager, process = runtime(tmp_path_factory)
    prepared, release = threading.Event(), threading.Event()
    class SlowStore(Store):
        def save_process_checkpoint(self, process):
            prepared.set(); assert release.wait(4)
            super().save_process_checkpoint(process)
    results, calls = [], []
    switch = threading.Thread(target=lambda: mechanisms.activate(process, "manual", 0, SlowStore()))
    switch.start(); assert prepared.wait(4)
    worker = threading.Thread(target=lambda: results.append(execute(process, tool(calls), tmp_path_factory)))
    worker.start()
    try:
        time.sleep(.05)
        assert calls == [] and results == []
    finally:
        release.set(); switch.join(5); worker.join(5)
    assert results[0].suspended


def test_permission_card_and_grants_are_not_answered_or_created_by_auto(tmp_path_factory):
    from backend.core.daemon.mechanism_control import resume_waiters
    manager, process = runtime(tmp_path_factory)
    mechanisms.activate(process, "manual", 0, manager.session_store)
    process.pending_decision = {"kind": "permission", "decision_id": "real-approval"}
    process.status = ProcessStatus.AWAITING_USER
    mechanisms.activate(process, "auto", 1, manager.session_store)
    resume_waiters(SimpleNamespace(), SimpleNamespace(), {"apm": manager})
    assert process.pending_decision["decision_id"] == "real-approval"
    assert process.approval_grants == []


def test_manual_suspension_preserves_ordered_suffix_and_resumes_without_replaying_prefix(tmp_path_factory):
    manager, process = runtime(tmp_path_factory)
    calls, outputs, events = [], [], []
    candidate = tool(calls)
    ctx = context(process, tmp_path_factory)
    from backend.core.loop.engineering_workflow import EngineeringWorkflow
    ctx.artifacts["engineering_workflow"] = EngineeringWorkflow(process)
    guard = SimpleNamespace(check_storm_break=lambda *a: None, record_tool_result=lambda *a, **k: None)
    batch = ToolBatchRuntime(process, process.session, lambda: {"unit": candidate}, guard, ctx,
                             str(tmp_path_factory), time.time(), lambda event, **k: events.append(event))
    # Switch after the first committed invocation, while this batch remains live.
    def run(args):
        calls.append(args["value"])
        if args["value"] == "one":
            mechanisms.activate(process, "manual", 0, manager.session_store)
        return {"ok": True}
    candidate = tool(calls, execute=run)
    tool_calls = [{"id": str(i), "name": "unit", "args": {"value": v}}
                  for i, v in enumerate(["one", "two", "three"])]
    batch.commit_results = lambda current, results, **k: outputs.extend(current[r.call_index]["id"] for r in results)
    result = batch.execute_live_batch(tool_calls)
    assert result.result is not None, events
    assert calls == ["one"] and outputs == ["0"]
    assert [c["id"] for c in process.pending_tool_batch["tool_calls"]] == ["1", "2"]
    pending = dict(process.pending_decision)
    mechanisms.approve_unit(process, pending, "run_once")
    saved_batch = process.pending_tool_batch
    process.pending_tool_batch = None; process.pending_decision = None
    process.status = ProcessStatus.RUNNING
    assert batch.execute_resumed_batch(saved_batch) is not None
    assert calls == ["one", "two"] and outputs == ["0", "1"]
    mechanisms.activate(process, "auto", 1, manager.session_store)
    saved_batch = process.pending_tool_batch
    process.pending_tool_batch = None; process.pending_decision = None
    process.status = ProcessStatus.RUNNING
    assert batch.execute_resumed_batch(saved_batch) is None
    assert calls == ["one", "two", "three"] and outputs == ["0", "1", "2"]


def test_mechanism_revision_survives_authoritative_checkpoint_reload(tmp_path_factory):
    from backend.core.loop.recovery import restore_incomplete_processes
    manager, process = runtime(tmp_path_factory)
    store = SessionStore(str(tmp_path_factory))
    try:
        manager.session_store = store
        mechanisms.activate(process, "manual", 0, store)
        restored = AgentProcessManager()
        restore_incomplete_processes(store, restored, str(tmp_path_factory), include_process_ids=[process.process_id])
        assert mechanisms.snapshot(restored.get(process.process_id))["mode"] == "manual"
        assert mechanisms.snapshot(restored.get(process.process_id))["revision"] == 1
    finally:
        store.close()


def test_unknown_commit_blocks_new_admissions_without_claiming_rollback(tmp_path_factory):
    from backend.core.storage import StorageCommitUnknown
    manager, process = runtime(tmp_path_factory)
    mechanisms.activate(process, "manual", 0, manager.session_store)
    manager.session_store.error = StorageCommitUnknown("unknown commit")
    with pytest.raises(StorageCommitUnknown):
        mechanisms.activate(process, "auto", 1, manager.session_store)
    assert mechanisms.projection(process)["phase"] == "blocked"
    with pytest.raises(ValueError, match="ACTIVATION_BLOCKED"):
        mechanisms.snapshot(process)
    calls = []
    result = execute(process, tool(calls), tmp_path_factory)
    assert result.is_error and calls == []


def test_definition_dependency_change_is_visible_and_fails_closed(tmp_path_factory):
    manager, process = runtime(tmp_path_factory)
    mechanisms.activate(process, "manual", 0, manager.session_store)
    process.session.mechanism_instances[mechanisms.ADVANCEMENT]["dependencies"]["permission_broker"] = 999
    assert mechanisms.projection(process)["phase"] == "blocked"
    with pytest.raises(ValueError, match="DEFINITION_UNSUPPORTED"):
        mechanisms.activate(process, "auto", 1, manager.session_store)


def test_mode_can_be_set_before_first_message_without_a_provider(tmp_path_factory):
    from backend.core.daemon.mechanism_control import command
    manager = AgentProcessManager()
    store = SessionStore(str(tmp_path_factory))
    manager.session_store = store
    emitted = []
    try:
        command({"mode": "manual", "expected_revision": 0}, SimpleNamespace(workspace_path=tmp_path_factory),
                SimpleNamespace(name="isolated"), {"apm": manager, "session_store": store}, emitted.append)
        assert "error" not in emitted[-1], emitted
        result = emitted[-1]["result"]
        assert result["mode"] == "manual" and result["revision"] == 1
        process = manager.get(result["process_id"])
        assert process.session.messages == []
        assert process.status == ProcessStatus.COMPLETED
        assert store.load_process_state(process.process_id)["session"]["mechanism_instances"]
        assert store.load_process_state(process.process_id)["runtime_state"]["lifecycle_finalized"] is True
        from backend.core.loop.conversation_continuation import resolve_conversation_process
        restored = resolve_conversation_process(AgentProcessManager(), store, str(tmp_path_factory), process.process_id)
        assert restored.status == ProcessStatus.COMPLETED
        assert mechanisms.snapshot(restored)["mode"] == "manual"
    finally:
        store.close()


def test_auto_releases_only_manual_continuation_using_current_epoch(tmp_path_factory, monkeypatch):
    from backend.core.daemon.mechanism_control import resume_waiters
    manager, process = runtime(tmp_path_factory)
    mechanisms.activate(process, "manual", 0, manager.session_store)
    ctx = context(process, tmp_path_factory)
    candidate = tool([])
    ToolPipeline().execute({"name": "unit", "args": {"value": "one"}}, candidate, ctx, "batch")
    unit = ctx.artifacts["advancement_pause"]
    process.pending_decision = mechanisms.manual_decision(process, unit)
    process.pending_tool_batch = {"tool_calls": [{"name": "unit", "args": {"value": "one"}}]}
    process.status = ProcessStatus.AWAITING_USER
    resumes = []
    monkeypatch.setattr("backend.core.daemon.dispatch._cmd_task", lambda cmd, *a: resumes.append(cmd))
    mechanisms.activate(process, "auto", 1, manager.session_store)
    resume_waiters(SimpleNamespace(), SimpleNamespace(), {"apm": manager, "session_store": manager.session_store})
    assert len(resumes) == 1 and resumes[0]["action"] == "continue_mechanism"
    assert resumes[0]["instruction"] == ""
    assert process.pending_decision is None and process.approval_grants == []
    assert process.pending_tool_batch["decision_action"] == "automatic"


def test_auto_waits_for_explicit_mechanism_resume_without_losing_original_batch(tmp_path_factory, monkeypatch):
    from backend.core.daemon.mechanism_control import resume_waiters, command
    from backend.core.loop.task_contract import publish_contract, validate_contract_proposal
    from backend.core.loop.task_maintenance import source_receipts, state_for, invoke
    manager, process = runtime(tmp_path_factory)
    process.session.append_user('Keep the original task while changing its lifecycle')
    publish_contract(process, validate_contract_proposal({'goal':'Original work', 'execution_mode':'self_execute',
        'deliverables':[], 'acceptance_criteria':['Do the original work'],
        'mechanism':{'intent':'Maintain original task', 'source_refs':list(source_receipts(process)), 'elements':[]}},
        str(tmp_path_factory)), store=manager.session_store)
    mechanisms.activate(process, 'manual', 0, manager.session_store)
    ctx = context(process, tmp_path_factory)
    # A fresh projection is acknowledged just as formal admission does.
    from backend.core.loop.tool_surface_runtime import ToolSurfaceRuntime
    candidate = tool([])
    mounted = ToolSurfaceRuntime(process,None,None,str(tmp_path_factory),None)
    mounted.tools = mounted.known_tools = {'unit':candidate}
    mounted.synchronize()
    paused = ToolPipeline().execute({'name':'unit','args':{'value':'one'}}, candidate,ctx,'batch')
    assert paused.suspended, paused.diagnostics
    process.pending_decision = mechanisms.manual_decision(process,ctx.artifacts['advancement_pause'])
    original = deepcopy(process.pending_decision)
    process.pending_tool_batch={'tool_calls':[{'name':'unit','args':{'value':'one'}}]}
    process.status=ProcessStatus.AWAITING_USER
    invoke(process,{'operation':'suspend','expected_revision':state_for(process)['revision']},user_control=True)
    mechanisms.activate(process,'auto',1,manager.session_store)
    resumed=[]
    monkeypatch.setattr('backend.core.daemon.dispatch._cmd_task',lambda cmd,*a:resumed.append(cmd))
    daemon={'apm':manager,'session_store':manager.session_store}
    session=SimpleNamespace(workspace_path=tmp_path_factory)
    resume_waiters(session,SimpleNamespace(),daemon)
    assert resumed==[] and process.pending_decision==original
    events=[]
    command({'task_operation':'resume','process_id':process.process_id,'task_id':process.active_task_id,
        'expected_revision':state_for(process)['revision']},session,SimpleNamespace(),daemon,events.append)
    assert len(resumed)==1 and resumed[0]['action']=='continue_mechanism', events
    assert process.pending_tool_batch['tool_calls']==[{'name':'unit','args':{'value':'one'}}]
    assert process.approval_grants==[] and not process.cancellation_event.is_set()


def test_unknown_continuation_commit_stops_instead_of_replaying(tmp_path_factory, monkeypatch):
    from backend.core.daemon.mechanism_control import resume_waiters
    from backend.core.storage import StorageCommitUnknown
    manager, process = runtime(tmp_path_factory)
    mechanisms.activate(process, "manual", 0, manager.session_store)
    ctx = context(process, tmp_path_factory)
    ToolPipeline().execute({"name": "unit", "args": {"value": "one"}}, tool([]), ctx, "batch")
    process.pending_decision = mechanisms.manual_decision(process, ctx.artifacts["advancement_pause"])
    process.pending_tool_batch = {"tool_calls": [{"name": "unit", "args": {"value": "one"}}]}
    process.status = ProcessStatus.AWAITING_USER
    mechanisms.activate(process, "auto", 1, manager.session_store)
    manager.session_store.error = StorageCommitUnknown("unknown continuation commit")
    resumed = []
    monkeypatch.setattr("backend.core.daemon.dispatch._cmd_task", lambda *a: resumed.append(a))
    resume_waiters(SimpleNamespace(), SimpleNamespace(), {"apm": manager, "session_store": manager.session_store})
    assert resumed == []
    assert process.status == ProcessStatus.RECOVERY_REVIEW_REQUIRED
    assert process.recovery["resume_forbidden"] is True


def test_restored_child_without_live_lifecycle_is_not_resumed_by_auto(tmp_path_factory):
    from backend.core.daemon.mechanism_control import resume_waiters
    manager, root = runtime(tmp_path_factory)
    child = manager.fork(root.process_id, "child", ToolRegistry(["unit"]), 4, RingLevel.RING_3,
                         workspace_path=str(tmp_path_factory), task_id="child")
    mechanisms.activate(root, "manual", 0, manager.session_store)
    ctx = context(child, tmp_path_factory)
    ToolPipeline().execute({"name": "unit", "args": {"value": "one"}}, tool([]), ctx, "batch")
    child.pending_decision = mechanisms.manual_decision(child, ctx.artifacts["advancement_pause"])
    child.pending_tool_batch = {"tool_calls": [{"name": "unit", "args": {"value": "one"}}]}
    child.status = ProcessStatus.AWAITING_USER
    pending = child.pending_decision
    mechanisms.activate(root, "auto", 1, manager.session_store)
    resume_waiters(SimpleNamespace(), SimpleNamespace(), {"apm": manager, "session_store": manager.session_store})
    assert child.status == ProcessStatus.AWAITING_USER and child.pending_decision is pending
    assert child.runtime_preferences["mechanism_resume_blocked"] == "CHILD_CONTINUATION_UNAVAILABLE"
    assert child.approval_grants == [] and manager._threads == {}


def test_continuing_session_keeps_one_instance_across_root_successors_and_restart(tmp_path_factory):
    from backend.core.loop.recovery import restore_incomplete_processes
    manager, predecessor = runtime(tmp_path_factory)
    store = SessionStore(str(tmp_path_factory))
    manager.session_store = store
    try:
        mechanisms.activate(predecessor, "manual", 0, store)
        child = manager.fork(predecessor.process_id, "child", ToolRegistry(["unit"]), 4, RingLevel.RING_3,
                             workspace_path=str(tmp_path_factory), task_id="child")
        store.save_process_checkpoint(child)
        predecessor.status = ProcessStatus.COMPLETED
        store.save_process_checkpoint(predecessor)
        successor = manager.fork(None, "worker", ToolRegistry(["unit"]), 8, RingLevel.RING_3,
                                 workspace_path=str(tmp_path_factory), task_id="next-work", session=predecessor.session)
        store.save_process_checkpoint(successor)
        assert mechanisms.snapshot(successor)["mode"] == "manual"
        mechanisms.activate(child, "auto", 1, store)
        assert mechanisms.snapshot(successor)["revision"] == 2
        assert mechanisms.snapshot(predecessor)["mode"] == "auto"
        assert store.load_latest_root_process_state()["process"]["process_id"] == successor.process_id
        restored = AgentProcessManager()
        restore_incomplete_processes(store, restored, str(tmp_path_factory), include_process_ids=[child.process_id, successor.process_id])
        a, b = restored.get(predecessor.process_id), restored.get(successor.process_id)
        assert a.session is b.session
        assert mechanisms.snapshot(restored.get(child.process_id))["mode"] == "auto"
        assert mechanisms.snapshot(b)["revision"] == 2
    finally:
        store.close()


@pytest.mark.parametrize('registered_mechanism', [False, True])
def test_switch_while_provider_streams_keeps_response_and_gates_generated_work(tmp_path_factory, registered_mechanism):
    from dataclasses import replace
    from backend.core.loop.executor import agent_step
    from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
    from backend.core.loop.outcome import TaskOutcome, OutcomeStatus
    from backend.core.tools.catalog import build_workspace_tools
    manager = AgentProcessManager(); manager.session_store = Store()
    process = manager.fork(None, "worker", ToolRegistry(["web_search"]), 6, RingLevel.RING_3,
                           workspace_path=str(tmp_path_factory), task_id="streaming-mode",
                           capability_profile_id="governance.observe", task_kind="answer")
    if registered_mechanism:
        from backend.core.loop.task_contract import publish_contract, validate_contract_proposal
        from backend.core.loop.task_maintenance import source_receipts
        process.session.append_user('Execute the original work under the current selected mode')
        publish_contract(process, validate_contract_proposal({'goal': 'Original work', 'execution_mode': 'answer',
            'deliverables': [], 'acceptance_criteria': [], 'mechanism': {'intent': 'Preserve original work',
                'source_refs': list(source_receipts(process)), 'elements': []}}, str(tmp_path_factory)),
            store=manager.session_store)
    started, release = threading.Event(), threading.Event()
    calls, events, results = [], [], []
    catalog = build_workspace_tools(tmp_path_factory)
    catalog["web_search"] = replace(catalog["web_search"], isolated=False,
        execution_contract=data_broker("test.fixture"), effect=ToolEffect.PROCESS, read_only=False,
        execute=lambda args: calls.append(args["query"]) or {"answer": "fixture result"})
    class Provider:
        protocol = SimpleNamespace(value="openai_responses")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 8192
        invocations = 0
        def stream_events(self, *args, **kwargs):
            self.invocations += 1
            if self.invocations == 1:
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="kept response")
                started.set(); assert release.wait(5)
                yield ProviderEvent(ProviderEventType.TOOL_CALL_STARTED, tool_call_id="exact-unit", tool_name="web_search", output_index=0)
                yield ProviderEvent(ProviderEventType.TOOL_CALL_DONE, tool_call_id="exact-unit", tool_name="web_search", output_index=0, arguments='{"query":"fixture"}')
            else:
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="done\nTASK_COMPLETE")
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)
    provider = Provider()
    dispatcher = SimpleNamespace(_executors=catalog)
    thread = threading.Thread(target=lambda: results.append(agent_step(process, provider, instruction="run the fixture",
        dispatcher=dispatcher, workspace_path=str(tmp_path_factory), on_stream_event=lambda event: events.append(event))))
    thread.start()
    try:
        assert started.wait(5)
        mechanisms.activate(process, "manual", 0, manager.session_store)
        assert not process.cancellation_event.is_set()
    finally:
        release.set(); thread.join(8)
    assert not thread.is_alive() and provider.invocations == 1
    assert TaskOutcome.from_dict(results[0]).status == OutcomeStatus.AWAITING_USER
    assert process.pending_decision["kind"] == "manual_execution"
    assert calls == []
    assert any("kept response" in str(event) for event in events)
    mechanisms.activate(process, "auto", 1, manager.session_store)
    process.pending_decision = None; process.pending_tool_batch["decision_action"] = "automatic"
    process.status = ProcessStatus.RUNNING; process.task_budget.resume_from_user()
    resumed = agent_step(process, provider, instruction="", dispatcher=dispatcher, workspace_path=str(tmp_path_factory))
    assert calls == ["fixture"]
    assert provider.invocations == 2, resumed
    outputs = [m for m in process.session.messages if m.get("tool_call_id") == "exact-unit" and m.get("message_type") == "tool_result"]
    assert len(outputs) == 1


def test_manual_pause_does_not_spend_real_permission_grant(tmp_path_factory):
    from dataclasses import replace
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
    manager, process = runtime(tmp_path_factory)
    calls = []
    candidate = replace(tool(calls), approval=ApprovalMode.ASK, approval_per_invocation=True)
    create_permission_request(process, {"tool_name": "unit", "arguments": {"value": "one"},
        "purpose": "Run the exact isolated fixture"}, {"unit": candidate}, str(tmp_path_factory))
    grant = grant_from_decision(process, process.pending_decision, "allow_once")
    process.pending_decision = None
    mechanisms.activate(process, "manual", 0, manager.session_store)
    ctx = context(process, tmp_path_factory)
    paused = ToolPipeline().execute({"name": "unit", "args": {"value": "one"}}, candidate, ctx, "batch")
    assert paused.suspended and calls == [] and grant["remaining_uses"] == 1
    process.pending_tool_batch = {"tool_calls": [{"name": "unit", "args": {"value": "one"}}]}
    mechanisms.approve_unit(process, mechanisms.manual_decision(process, ctx.artifacts["advancement_pause"]), "run_once")
    result = execute(process, candidate, tmp_path_factory)
    assert not result.is_error and calls == ["one"] and grant["remaining_uses"] == 0


def test_native_mode_command_uses_canonical_cursor_and_explicit_child_target():
    from backend.core.native_host import NativeHost
    host = NativeHost.__new__(NativeHost)
    host._conversation_processes = {"isolated": "root"}; host._sessions = {}
    commands = []
    def send(cmd, timeout):
        commands.append(cmd)
        return {"mode": "manual", "revision": 1, "process_id": cmd["process_id"], "session_id": "session"}
    host._get_daemon = lambda project, start: SimpleNamespace(send_command=send)
    result = host._dispatch({"operation": "runtime.advancement", "arguments": {
        "project": "isolated", "mode": "manual", "expected_revision": 0}})
    assert commands[-1] == {"cmd": "mechanism", "process_id": "root", "mode": "manual", "expected_revision": 0}
    assert result["mode"] == "manual" and host._sessions["isolated"] == "session"
    host._dispatch({"operation": "runtime.advancement", "arguments": {"project": "isolated", "process_id": "child"}})
    assert commands[-1]["process_id"] == "child" and host._conversation_processes["isolated"] == "root"
