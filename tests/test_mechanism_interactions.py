"""Component/ToolPipeline fixtures; these are not real user/API acceptance."""
from contextlib import closing
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import RingLevel, ProcessStatus
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.loop_guard import LoopGuard
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.tool_batch_runtime import ToolBatchRuntime
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.task_maintenance import source_receipts
from backend.core.mechanism_runtime.application import Application


@pytest.fixture
def runtime(tmp_path_factory):
    workspace = tmp_path_factory / "work"
    workspace.mkdir()
    with closing(SessionStore(workspace, state_home=tmp_path_factory / "state", migrate_legacy=False)) as store:
        manager = AgentProcessManager()
        manager.storage, manager.session_store = store._storage, store
        process = manager.fork(None, "worker", ToolRegistry(["unit", "harness_mechanism"]), 30, RingLevel.RING_3,
            workspace_path=str(workspace), task_id="interaction-fixture", actor_kind="worker", capability_profile_id="development.workspace")
        process.session.append_user("Unit fixture: establish an optional per-call checkpoint with hints, skip and exit.")
        app = Application(process)
        process._harness_application = app
        calls = []
        tool = AgentTool(name="unit", description="Fixture effect", parameters={"type": "object", "properties": {"value": {"type": "string"}}},
            effect=ToolEffect.PROCESS, execution_contract=data_broker("test.fixture"), execute=lambda args: calls.append(args["value"]) or {"ok": True})
        ctx = ExecutionContext(process, process.session, str(workspace), storage=store._storage)
        ctx.artifacts["engineering_workflow"] = EngineeringWorkflow(process)
        batch = ToolBatchRuntime(process, process.session, lambda: {"unit": tool}, LoopGuard(), ctx, str(workspace), time.time(), lambda *args, **kwargs: None)
        yield app, process, calls, tool, ctx, batch, store


def checkpoint_program(process):
    return {"schema_version": 2, "id": "checkpoints", "intent": "fixture optional checkpoint before the unit call; compare its actual outcome", "source_refs": list(source_receipts(process)),
        "elements": [{"id": "before", "use": "runtime.event", "parameters": {"boundaries": ["before_tool"]}},
            {"id": "question", "use": "interaction.checkpoint", "parameters": {"question": "Predict this operation's result", "reason": "The fixture user requested checkpoints.", "hint": "Read the exact operation arguments."}},
            {"id": "after", "use": "runtime.event", "parameters": {"boundaries": ["after_tool"]}},
            {"id": "compare", "use": "interaction.compare"}],
        "bindings": [{"kind": "trigger", "from": {"node": "before", "port": "go"}, "to": {"node": "question", "port": "go"}},
            {"kind": "data", "from": {"node": "before", "port": "event"}, "to": {"node": "question", "port": "event"}},
            {"kind": "data", "from": {"node": "after", "port": "event"}, "to": {"node": "compare", "port": "event"}}]}


def enable(app, process, raw):
    preview = app.invoke({"operation": "preview", "program": raw})
    app.invoke({"operation": "publish", "program": raw, "expected_head": preview["expected_head"], "preview_digest": preview["preview_digest"]})
    return app.invoke({"operation": "instantiate", "definition_id": raw["id"], "version_policy": "pinned", "source_refs": list(source_receipts(process))})["instance"]


def test_cold_btw_delivery_uses_retained_instance_without_resuming_model(runtime):
    from backend.core.daemon.discussion_delivery import notify_discussion
    from backend.core.mechanism_runtime.application import bound_application
    app, process, _, _, _, _, store = runtime
    enable(app, process, inbox_program(process))
    from backend.core.loop.decision_support import create_user_decision
    question = create_user_decision(process, {'question': 'What should change?',
        'why_user_must_decide': 'Fixture-owned choice', 'kind': 'clarification',
        'options': [], 'allow_free_form': True})
    process.status = ProcessStatus.AWAITING_USER
    reference = fixture_discussion(app, 'A candidate retained before restart.', 'cold-discussion')
    store.save_process_checkpoint(process)
    cold = AgentProcessManager()
    cold.storage, cold.session_store = store._storage, store
    notices = []
    result = {'discussion_ref': reference, 'source_process_ids': [process.process_id]}
    notify_discussion(result, cold, store, process.workspace_root, object(), notices.append)
    restored = cold.get(process.process_id)
    assert restored is not None and restored.status == ProcessStatus.AWAITING_USER
    assert restored.active_task_id == process.active_task_id
    assert restored.session.session_id == process.session.session_id
    assert restored.pending_decision == process.pending_decision
    assert restored.pending_decision['decision_id'] == question['decision_id']
    assert not restored.approval_grants and restored.capability_lease is None
    recovered_app = bound_application(restored)
    first = recovered_app.repo.list_instances(recovered_app.task)
    notify_discussion(result, cold, store, process.workspace_root, object(), notices.append)
    assert recovered_app.repo.list_instances(recovered_app.task) == first
    assert not notices, notices
    instance = recovered_app.repo.list_instances(recovered_app.task)[0]
    assert len(instance['inbox']) == 1
    candidate = next(iter(instance['inbox'].values()))
    assert candidate['source_refs'] == [reference]
    assert candidate['status'] == 'candidate'


def test_btw_delivery_reports_interrupted_target_without_implicitly_resuming(runtime):
    from backend.core.daemon.discussion_delivery import notify_discussion
    app, process, _, _, _, _, store = runtime
    enable(app, process, inbox_program(process))
    process.status = ProcessStatus.RESUME_AVAILABLE
    reference = fixture_discussion(app, 'A candidate for an interrupted task.', 'interrupted-discussion')
    notices = []
    notify_discussion({'discussion_ref': reference, 'source_process_ids': [process.process_id]},
                      process._manager, store, process.workspace_root, object(), notices.append)
    assert process.status == ProcessStatus.RESUME_AVAILABLE
    assert not app.repo.list_instances(app.task)[0].get('inbox')
    assert len(notices) == 1 and notices[0]['discussion_committed'] is True
    assert notices[0]['error'].startswith('CONTINUATION_RECOVERY_REQUIRED:')


def fixture_answer(process, *, selected=None, text="I predict it will succeed"):
    """Explicit synthetic answer for a component test, never an API score."""
    request = deepcopy(process.pending_decision)
    process.session.host_ledger.append({"event": "user_decision_received", "decision_id": request["decision_id"],
        "task_id": process.active_task_id, "kind": request["kind"], "answer": text, "selected_label": selected or "",
        "source": "unit_fixture"})
    process.pending_decision = None
    process.status = ProcessStatus.RUNNING
    saved = process.pending_tool_batch
    process.pending_tool_batch = None
    return saved


def inbox_program(process, *, attach=False):
    raw = {"schema_version": 2, "id": "discussions", "intent": "fixture discussion lifecycle", "source_refs": list(source_receipts(process)),
        "elements": [{"id": "event", "use": "runtime.event", "parameters": {"boundaries": ["discussion_candidate"]}},
            {"id": "collect", "use": "inbox.collect"}],
        "bindings": [{"kind": "trigger", "from": {"node": "event", "port": "go"}, "to": {"node": "collect", "port": "go"}},
            {"kind": "data", "from": {"node": "event", "port": "event"}, "to": {"node": "collect", "port": "event"}}]}
    if attach:
        raw["elements"][1] = {"id": "collect", "use": "question.attach", "parameters": {"decision_id": process.pending_decision["decision_id"]}}
    return raw


def fixture_discussion(app, text, identifier="fixture-sidecar"):
    from backend.core.loop.discussion_adoption import save_discussion
    return save_discussion(app.repo.storage, identifier, "Fixture only: what should change?",
        {"answer": text, "status": "completed"}, [{"process_id": app.process.process_id, "task_id": app.task, "contract_revision": 0}])["discussion_ref"]


def test_same_immutable_discussion_coalesces_while_question_snapshot_changes(runtime):
    from backend.core.loop.decision_support import create_user_decision
    app, process, *_ = runtime
    enable(app, process, inbox_program(process))
    reference = fixture_discussion(app, 'A genuine new candidate.')
    entered, release = threading.Event(), threading.Event()
    original = app.services['inbox.add']
    effects = []
    def held(ctx, payload):
        effects.append(ctx.event_id)
        entered.set()
        assert release.wait(5)
        return original(ctx, payload)
    app.services['inbox.add'] = held
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(app.receive_discussion, reference)
        assert entered.wait(5)
        try:
            create_user_decision(process, {'question': 'A new current question',
                'why_user_must_decide': 'Fixture business ambiguity', 'kind': 'clarification',
                'options': [], 'allow_free_form': True})
            assert app.receive_discussion(reference)
        finally:
            release.set()
        assert first.result()
    assert effects == ['discussion:' + reference]
    assert len(app.repo.instance(app.task, 'discussions')[0]['inbox']) == 1
    assert process.pending_decision['question'] == 'A new current question'


def test_running_before_tool_observation_never_admits_business_effect(runtime):
    app, process, calls, *_ = runtime
    enable(app, process, checkpoint_program(process))
    app.dispatch = lambda *a: [{'instance_id': 'checkpoints', 'phase': 'running'}]
    with pytest.raises(ValueError, match='MECHANISM_EVENT_PENDING'):
        app.before_tool('actual-unit', {'tool_name': 'unit', 'arguments': {'value': 'effect'}})
    assert calls == [] and not process.approval_grants


def test_material_citations_do_not_confuse_historical_basis_with_pending_destination(runtime):
    from types import SimpleNamespace
    from backend.core.loop.decision_support import create_user_decision
    app, process, *_ = runtime
    def ask(text):
        return create_user_decision(process, {'question': text, 'why_user_must_decide': 'Fixture',
            'kind': 'clarification', 'options': [], 'allow_free_form': True})['decision_id']
    old = ask('Existing compatibility requirement?')
    fixture_answer(process, text='Keep the original signature.')
    current = ask('Adopt the proposed change?')
    enable(app, process, inbox_program(process))
    reference = fixture_discussion(app, 'Material cites the earlier answer as evidence for the pending choice.')
    context = SimpleNamespace(boundary='discussion_candidate', task_id=app.task)
    event = {'discussion_ref': reference, 'sidecar_id': 'fixture-sidecar'}
    result = app.inbox.attach_material(context, {'event': event, 'evidence': {'evidence_refs': [old, current, reference]}})
    assert result['attached'] and result['material']['decision_id'] == current
    assert process.pending_decision['decision_id'] == current and not process.approval_grants
    # Merely having one pending card is insufficient without a reference.
    refused = app.inbox.attach_material(context, {'event': event, 'evidence': {'evidence_refs': [old]}})
    assert refused['attached'] is False
    historical = app.inbox.attach_material(context, {'event': event, 'decision_id': old})
    assert historical['attached'] and historical['material']['decision_id'] == old


def test_judgment_evidence_cannot_be_wired_as_a_discussion_event(runtime):
    app, process, *_ = runtime
    app.services['agent.judge'] = lambda *a: {'matches': True, 'reason': 'Synthetic unit judgment', 'evidence_refs': []}
    app.coordinator.capabilities = tuple(app.services)
    raw = inbox_program(process)
    raw['elements'].append({'id': 'judge', 'use': 'agent.condition', 'parameters': {'criterion': 'Unit criterion'}})
    raw['bindings'] = [
        {'kind': 'trigger', 'from': {'node': 'event', 'port': 'go'}, 'to': {'node': 'judge', 'port': 'go'}},
        {'kind': 'data', 'from': {'node': 'event', 'port': 'event'}, 'to': {'node': 'judge', 'port': 'event'}},
        {'kind': 'condition', 'from': {'node': 'judge', 'port': 'matches'}, 'to': {'node': 'collect', 'port': 'go'}, 'when': True},
        {'kind': 'data', 'from': {'node': 'judge', 'port': 'evidence'}, 'to': {'node': 'collect', 'port': 'event'}}]
    with pytest.raises(ValueError, match='BINDING_TYPE_MISMATCH'):
        app.invoke({'operation': 'preview', 'program': raw})
    raw['bindings'][-1]['from'] = {'node': 'event', 'port': 'event'}
    enable(app, process, raw)
    app.receive_discussion(fixture_discussion(app, 'A real candidate behind the correctly typed condition route.'))
    assert len(app.repo.instance(app.task, 'discussions')[0]['inbox']) == 1
    assert app.registry.resolve('agent.condition', 1)['outputs'][1]['type'] == 'record'
    assert app.registry.resolve('agent.condition')['outputs'][1]['type'] == 'evidence'


def test_legacy_untyped_bad_discussion_payload_is_rejected_before_storage_effect(runtime, monkeypatch):
    from types import SimpleNamespace
    app, process, *_ = runtime
    observed = []
    monkeypatch.setattr(app.repo.storage, 'read_blob', lambda *a: observed.append(a))
    ctx = SimpleNamespace(task_id=app.task, boundary='discussion_candidate')
    with pytest.raises(ValueError, match='MECHANISM_DISCUSSION_EVENT_REQUIRED'):
        app.inbox.add(ctx, {'event': {'matches': True, 'evidence_refs': []}})
    assert not observed


def test_restored_waiting_task_rebinds_model_service_before_discussion(runtime):
    from types import SimpleNamespace
    app, process, *_ = runtime
    provider = SimpleNamespace(chat=lambda *args, **kwargs: {'content':
        '{"matches":true,"reason":"synthetic unit judgment","evidence_refs":[]}'})
    app.bind_provider(provider)
    raw = inbox_program(process)
    raw['elements'].append({'id':'judge','use':'agent.condition','parameters':{'criterion':'Unit-only candidate judgment'}})
    raw['bindings'] = [
        {'kind':'trigger','from':{'node':'event','port':'go'},'to':{'node':'judge','port':'go'}},
        {'kind':'data','from':{'node':'event','port':'event'},'to':{'node':'judge','port':'event'}},
        {'kind':'condition','from':{'node':'judge','port':'matches'},'to':{'node':'collect','port':'go'},'when':True},
        {'kind':'data','from':{'node':'event','port':'event'},'to':{'node':'collect','port':'event'}}]
    enable(app, process, raw)
    restored = Application(process)
    assert 'agent.judge' not in restored.services
    assert 'agent.judge' not in restored.coordinator.capabilities
    reference = fixture_discussion(app, 'Genuine immutable fixture input after task reload.')
    restored.bind_provider(provider)
    assert 'agent.judge' in restored.coordinator.capabilities
    assert restored.receive_discussion(reference)
    state = restored.repo.instance(restored.task,'discussions')[0]
    assert state['phase'] == 'ready' and len(state['inbox']) == 1
    assert not process.pending_decision and not process.approval_grants
    restored.bind_provider(None)
    assert 'agent.judge' not in restored.services and 'agent.judge' not in restored.coordinator.capabilities


def test_mechanism_judgment_emits_actual_usage_without_losing_billing(runtime):
    from types import SimpleNamespace
    app,process,*_=runtime
    events=[]
    usage={'input_tokens':100,'cache_read_tokens':80,'output_tokens':5,'raw':{'provider':'unit-only'}}
    provider=SimpleNamespace(chat=lambda *a,**k:{'content':'{"matches":true,"reason":"unit evidence","evidence_refs":[]}',
        'usage':usage},max_output_tokens=4096)
    app.bind_provider(provider,events.append)
    ctx=SimpleNamespace(deadline=time.monotonic()+30,instance_id='unit-instance',epoch=1,check=lambda:None)
    result=app.judge(ctx,{'criterion':'Observe','event':{},'prior_state':{}})
    event=next(e for e in events if e.get('event')=='mechanism_provider_usage')
    assert result['matches'] and result['authority_granted'] is False
    assert event['purpose']=='mechanism_judgment' and event['usage']['cache_read_tokens']==80
    assert 'raw' not in event['usage']
    assert process.session.provider_usage[-1]['purpose']=='mechanism_judgment'
    assert process.session.provider_usage[-1]['output_tokens']==5


def test_preview_token_binds_actual_cas_head_when_nullable_argument_is_omitted(runtime):
    app, process, *_ = runtime
    raw = checkpoint_program(process)
    preview = app.invoke({'operation': 'preview', 'program': raw})
    with pytest.raises(ValueError, match='PREVIEW_STALE'):
        app.invoke({'operation': 'publish', 'program': raw, 'preview_digest': preview['preview_digest'], 'expected_head': 'null'})
    saved = app.invoke({'operation': 'publish', 'program': raw, 'preview_digest': preview['preview_digest']})
    assert saved['definition']['revision'] == 1
    with pytest.raises(ValueError, match='PREVIEW_STALE'):
        app.invoke({'operation': 'publish', 'program': raw, 'preview_digest': preview['preview_digest']})
    assert app.repo.definition(raw['id'])[0]['revision'] == 1


@pytest.mark.parametrize('cancelled, expected', [(True, 'cancelled'), (False, 'failed')])
def test_component_cancellation_uses_actual_task_lifecycle_not_exception_text(runtime, monkeypatch, cancelled, expected):
    from backend.core.loop import executor
    app, process, *_ = runtime
    if cancelled:
        process.cancel_requested = True
        process.cancellation_event.set()
    def interrupted(*args, **kwargs):
        raise RuntimeError('MECHANISM_CANCELLED')
    monkeypatch.setattr(executor, '_agent_step', interrupted)
    result = executor.agent_step(process, None)
    assert result['status'] == expected
    assert process.status.value == expected


def test_omitted_nullable_head_never_removes_the_final_storage_cas(runtime, monkeypatch):
    app, process, *_ = runtime
    raw = checkpoint_program(process)
    preview = app.invoke({'operation': 'preview', 'program': raw})
    original = app.repo.publish
    raced = []
    def concurrent_writer(compiled, sources, *, expected_head):
        assert expected_head is None
        raced.append(original(compiled, sources, expected_head=expected_head))
        return original(compiled, sources, expected_head=expected_head)
    monkeypatch.setattr(app.repo, 'publish', concurrent_writer)
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        app.invoke({'operation': 'publish', 'program': raw, 'preview_digest': preview['preview_digest']})
    assert len(raced) == 1 and app.repo.definition(raw['id'])[0]['revision'] == 1


def test_grill_and_main_context_share_actual_composition_and_control_versions(runtime):
    from backend.core.loop.alignment_evidence import alignment_input_digest
    from backend.core.loop.alignment_runtime import AlignmentRuntime
    app, process, *_ = runtime
    assessor = AlignmentRuntime(process)
    assessor.payload('Unit fixture: use the existing registered checkpoint lifecycle.')
    empty = alignment_input_digest(process)
    enable(app, process, checkpoint_program(process))
    active = alignment_input_digest(process)
    assert active != empty
    payload = assessor.payload()
    described = payload['harness_semantics']['instances'][0]
    assert described['phase'] == 'ready' and described['program']['bindings']
    assert any(node['use'] == 'interaction.checkpoint' for node in described['program']['elements'])
    app.dispatch('before_tool', 'unit:actual-unit', {'unit_id': 'actual-unit', 'tool_name': 'unit', 'arguments': {'value': 'one'}})
    assert alignment_input_digest(process) == active, 'Observing a checkpoint is not a new semantic obligation'
    observed = assessor.payload()['harness_semantics']['instances'][0]
    assert observed['execution']['checkpoints'][0]['decision_id'] == process.pending_decision['decision_id']
    fixture_answer(process, selected='Disable this extension')
    app.interactions.admit('actual-unit')
    assert alignment_input_digest(process) != active
    inactive = assessor.payload()['harness_semantics']['instances'][0]
    assert inactive['phase'] == 'suspended' and inactive['user_control']['action'] == 'suspend'
    assert inactive['control_effect']['scope'] == 'this_instance_activation_only'
    assert inactive['control_effect']['selected_label'] == 'Disable this extension'
    assert inactive['control_effect']['business_scope_cancelled'] is False
    assert inactive['control_effect']['authority_granted'] is False
    assert inactive['program_digest'] in app.envelope() and 'program' not in inactive


def test_discussion_candidates_revise_merge_adopt_survive_detach_without_permission(runtime):
    from backend.core.loop.task_maintenance import state_for, guard
    app, process, calls, tool, ctx, batch, store = runtime
    current = enable(app, process, inbox_program(process))
    reference = fixture_discussion(app, "Add a real compatibility check.")
    app.drain_discussions()
    app.receive_discussion(reference)
    item = next(iter(app.repo.instance(app.task, "discussions")[0]["inbox"].values()))
    assert item["status"] == "candidate" and not state_for(process).get("changes")
    args = {"instance_id": "discussions", "source_refs": list(source_receipts(process)), "candidate_id": item["id"], "candidate_revision": 1}
    revised = app.invoke({**args, "operation": "inbox_revise", "text": "Add compatibility and negative checks."})["candidate"]
    assert revised["revision"] == 2 and not guard(process, "unit")
    app.receive_discussion(fixture_discussion(app, "Verify a restart.", "second"))
    items = app.repo.instance(app.task, "discussions")[0]["inbox"]
    merged = app.invoke({"operation": "inbox_merge", "instance_id": "discussions", "source_refs": args["source_refs"],
        "candidates": [{"id": v["id"], "revision": v["revision"]} for v in items.values()],
        "text": "Verify compatibility, negative cases and restart."})["candidate"]
    assert merged["status"] == "candidate" and len(merged["source_refs"]) == 2
    process.session.append_user("Adopt these compatibility and restart requirements in the existing task.")
    accepted = {"operation": "inbox_adopt", "instance_id": "discussions", "candidate_id": merged["id"], "candidate_revision": 1,
        "source_refs": list(source_receipts(process))[-1:], "quote": "Adopt these compatibility and restart requirements in the existing task.", "contract_revision": 0}
    result = app.invoke(accepted)
    assert result["candidate"]["status"] == "adopted"
    assert guard(process, "unit")[0] == "TASK_MAINTENANCE_REQUIRED"
    assert app.invoke(accepted)["change"]["id"] == result["change"]["id"]
    value = app.repo.instance(app.task, "discussions")[0]
    app.invoke({"operation": "detach", "instance_id": "discussions", "expected_revision": value["revision"], "source_refs": accepted["source_refs"]})
    assert len(state_for(process)["changes"]) == 1 and guard(process, "unit") and not process.approval_grants


def test_real_question_material_is_durable_deduplicated_not_an_answer(runtime):
    from backend.core.loop.decision_support import create_user_decision
    from backend.core.loop.decision_timeline import decision_timeline
    from backend.core.loop.question_broker import attach_material
    app, process, calls, tool, ctx, batch, store = runtime
    create_user_decision(process, {"question": "Which interface must remain compatible?", "why_user_must_decide": "Need a target.",
        "options": [], "allow_free_form": True, "kind": "clarification"})
    question = process.pending_decision["decision_id"]
    enable(app, process, inbox_program(process, attach=True))
    reference = fixture_discussion(app, "The fixture's answer explains the existing public interface.")
    app.receive_discussion(reference)
    app.receive_discussion(reference)
    card = decision_timeline(process.session.host_ledger)[-1]
    assert card["status"] == "awaiting_user" and card["decision_answer"] is None
    assert len(card["decision"]["materials"]) == 1 and not process.approval_grants
    assert process.pending_decision["materials"][0]["reference"] == reference
    assert not store.load_session_state(process.process_id)["host_ledger"][-1].get("authority_granted")
    process.session.host_ledger.append({"event": "user_decision_cancelled", "decision_id": question, "task_id": app.task, "reason": "fixture"})
    with pytest.raises(ValueError, match="TARGET_STALE"):
        attach_material(process, question, reference, store=store)


def test_pending_adoption_reconciles_same_change_after_result_checkpoint_failure(runtime, monkeypatch):
    from backend.core.loop.task_maintenance import state_for
    app, process, calls, tool, ctx, batch, store = runtime
    enable(app, process, inbox_program(process))
    app.receive_discussion(fixture_discussion(app, "Preserve public interface."))
    item = next(iter(app.repo.instance(app.task, "discussions")[0]["inbox"].values()))
    process.session.append_user("Adopt the interface requirement.")
    original = app.repo.mutate
    def fail_result(task, name, callback, **kwargs):
        if callback.__name__ == "adopted":
            raise OSError("unit fixture: reply checkpoint failed")
        return original(task, name, callback, **kwargs)
    monkeypatch.setattr(app.repo, "mutate", fail_result)
    with pytest.raises(OSError):
        app.invoke({"operation": "inbox_adopt", "instance_id": "discussions", "candidate_id": item["id"], "candidate_revision": 1,
            "source_refs": list(source_receipts(process))[-1:], "quote": "Adopt the interface requirement.", "contract_revision": 0})
    assert len(state_for(process)["changes"]) == 1
    monkeypatch.setattr(app.repo, "mutate", original)
    app.inbox.reconcile()
    assert len(state_for(process)["changes"]) == 1
    assert app.repo.instance(app.task, "discussions")[0]["inbox"][item["id"]]["status"] == "adopted"


def test_explicit_owner_requirements_bind_child_without_copying_local_instances_or_tool_scope(runtime):
    app, process, calls, tool, ctx, batch, store = runtime
    manager = process._manager
    raw = {"schema_version": 2, "id": "scope-note", "intent": "fixture shared required guidance", "source_refs": list(source_receipts(process)),
        "elements": [{"id": "event", "use": "runtime.event", "parameters": {"boundaries": ["before_context"]}},
            {"id": "note", "use": "context.project", "parameters": {"content": "Do not alter the public interface."}}],
        "bindings": [{"kind": "trigger", "from": {"node": "event", "port": "go"}, "to": {"node": "note", "port": "go"}}]}
    enable(app, process, raw)
    child = manager.fork(process.process_id, "reviewer", ToolRegistry(["read_file"]), 10, RingLevel.RING_3,
        workspace_path=process.workspace_root, task_id="child-review", actor_kind="reviewer", capability_profile_id="review.independent")
    child_app = Application(child)
    assert child_app.requirements.synchronize() == []  # Parent-local is not inherited.
    args = {"operation": "require", "expected_required_revision": 0, "source_refs": list(source_receipts(process)),
        "requirements": [{"definition_id": "scope-note", "version_policy": "pinned"}]}
    app.invoke(args)
    staged = child_app.repo.list_instances(child_app.task, limit=100)
    assert len(staged) == 1 and staged[0]['phase'] == 'pending_activation'
    assert staged[0]['managed_by']['requirements_revision'] == 1
    names = child_app.requirements.synchronize()
    assert len(names) == 1 and child.tool_registry.has("read_file") and not child.tool_registry.has("unit")
    child_app.dispatch("before_context", "fixture-child-context", {"task_id": child_app.task})
    assert "Do not alter the public interface." in child_app.envelope()
    assert not child.approval_grants
    child.actor_kind, child.capability_profile_id = "worker", "development.workspace"
    child.session.append_user("Fixture child: try to remove the owner's required constraint.")
    with pytest.raises(ValueError, match="REQUIRED_INSTANCE_OWNER_ONLY"):
        child_app.invoke({"operation": "detach", "instance_id": names[0], "expected_revision": 2,
                          "source_refs": list(source_receipts(child))})
    unrelated = manager.fork(None, "worker", ToolRegistry(["read_file"]), 10, RingLevel.RING_3,
        workspace_path=process.workspace_root, task_id="unrelated", actor_kind="worker", capability_profile_id="text.only")
    assert Application(unrelated).requirements.synchronize() == []
    app.invoke({**args, "expected_required_revision": 1, "requirements": []})
    child_app.requirements.synchronize()
    assert child_app.repo.instance(child_app.task, names[0])[0]["phase"] == "detached"


def test_formal_decision_choice_resumes_runtime_checkpoint_without_new_requirement(runtime):
    import queue
    from types import SimpleNamespace
    from backend.core.daemon.dispatch import _cmd_task
    from backend.core.loop.decision_support import create_user_decision, confirmed_user_state
    from backend.core.loop.alignment_evidence import alignment_input_digest
    app, root, calls, tool, ctx, batch, store = runtime
    manager = root._manager
    child = manager.fork(root.process_id, "worker", ToolRegistry(["unit"]), 12, RingLevel.RING_3,
        workspace_path=root.workspace_root, task_id="checkpoint-controller-fixture", actor_kind="worker", capability_profile_id="development.workspace")
    child.session.append_user("Fixture: a prediction is advancement input, not a new business requirement.")
    before = alignment_input_digest(child)
    pending = create_user_decision(child, {"kind": "runtime_checkpoint", "state_topic": "fixture.runtime-checkpoint",
        "question": "Predict this result", "why_user_must_decide": "Fixture checkpoint", "allow_free_form": True,
        "options": [{"label": "Hint"}, {"label": "Skip"}, {"label": "Exit"}]})
    child.pending_tool_batch = {"execution_id": "fixture-execution", "tool_calls": [{"id": "fixture-call", "name": "unit", "args": {}}],
        "harness_checkpoint": {"decision_id": pending["decision_id"]}}
    child.status = ProcessStatus.AWAITING_USER
    resumed = []
    def resume(instruction):
        resumed.append(instruction)
        child.status = ProcessStatus.COMPLETED
        return {"status": "completed"}
    manager.start(child.process_id, lambda: {"status": "awaiting_user"}, resume_target=resume)
    manager._threads[child.process_id].join(3)
    previous = manager._threads[child.process_id]
    emitted = []
    _cmd_task({"action": "decision", "process_id": child.process_id, "decision_id": pending["decision_id"],
        "instruction": "Choose option 1: Hint"}, SimpleNamespace(workspace_path=root.workspace_root), SimpleNamespace(name="fixture"),
        {"apm": manager, "session_store": store, "evq": queue.Queue(), "dispatcher": object()}, emitted.append)
    deadline = time.monotonic() + 3
    while (manager._threads[child.process_id] is previous or manager._threads[child.process_id].ident is None) and time.monotonic() < deadline:
        time.sleep(.01)
    manager._threads[child.process_id].join(3)
    assert resumed == [""]
    assert alignment_input_digest(child) == before and confirmed_user_state(child.session.host_ledger, task_id=child.active_task_id) == []
    receipt = next(item for item in child.session.host_ledger if item.get("event") == "user_decision_received")
    assert receipt["selected_label"] == "Hint" and not receipt["approval_grant_id"]
    assert child.pending_tool_batch["execution_id"] == "fixture-execution" and not child.approval_grants


def test_checkpoint_reply_is_consumed_before_new_permission_checks(runtime, monkeypatch):
    app, process, calls, tool, ctx, batch, store = runtime
    instance = enable(app, process, checkpoint_program(process))
    call = {'id': 'exact-restored-call', 'name': 'unit', 'args': {'value': 'one'}}
    assert batch.execute_live_batch([call]).result
    saved = fixture_answer(process, text='Prediction, never permission')
    unit = saved['harness_checkpoint']['unit_id']
    original = batch.missing_permission
    def recheck(*args):
        assert app.repo.instance(app.task, instance['id'])[0]['holds'][unit]['phase'] == 'answered'
        assert not process.approval_grants
        return original(*args)
    monkeypatch.setattr(batch, 'missing_permission', recheck)
    assert batch.execute_resumed_batch(saved) is None
    assert calls == ['one']
    assert app.repo.instance(app.task, instance['id'])[0]['holds'][unit]['phase'] == 'compared'


def test_completed_text_cannot_hide_a_registered_wait_after_canonical_reply(runtime):
    from backend.core.loop.completion_protocol import HostCompletionEvaluator
    app, process, calls, tool, ctx, batch, store = runtime
    instance = enable(app, process, checkpoint_program(process))
    assert batch.execute_live_batch([{'id': 'held-call', 'name': 'unit', 'args': {'value': 'one'}}]).result
    fixture_answer(process, text='Prediction received but invocation not yet settled')
    assert process.pending_decision is None
    process.task_kind = 'answer'
    evaluated = HostCompletionEvaluator.evaluate(process, 'The task is complete')
    assert not evaluated.allowed and any('checkpoint remains unsettled' in r for r in evaluated.reasons)
    assert not calls and not process.approval_grants


def test_material_recovery_reader_returns_only_current_task_canonical_facts(runtime):
    from types import SimpleNamespace
    app, process, *_ = runtime
    reference = 'sha256:unit-material-fixture'
    # Explicit synthetic ledger fixtures, never a real API evaluation input.
    fact = {'event':'user_decision_material_attached','task_id':app.task,'decision_id':'unit-card',
        'reference':reference,'source':'immutable_discussion','authority_granted':False}
    process.session.host_ledger.extend([deepcopy(fact), {**fact,'task_id':'other-task','decision_id':'private-other-card'}])
    read = app.services['decision.read'](SimpleNamespace(task_id=app.task), {'material_reference':reference})
    assert read['material_facts'] == [fact]
    assert app.services['decision.read'](SimpleNamespace(task_id=app.task), {'material_reference':'sha256:absent'})['material_facts'] == []
    assert 'material_facts' not in app.services['decision.read'](SimpleNamespace(task_id=app.task), {})


def test_changed_pending_call_retires_old_prediction_without_executing(runtime):
    app, process, calls, tool, ctx, batch, store = runtime
    instance = enable(app, process, checkpoint_program(process))
    assert batch.execute_live_batch([{'id': 'original-call', 'name': 'unit', 'args': {'value': 'one'}}]).result
    saved = fixture_answer(process, text='Prediction for the original call only')
    old_unit = saved['harness_checkpoint']['unit_id']
    saved['tool_calls'][0]['args'] = {'value': 'changed'}
    batch.execute_resumed_batch(saved)
    assert not calls and not process.approval_grants
    assert app.repo.instance(app.task, instance['id'])[0]['holds'][old_unit]['phase'] == 'superseded'


def test_hint_waits_skip_releases_exact_call_and_exit_restores_normal_work(runtime):
    app, process, calls, tool, ctx, batch, store = runtime
    enable(app, process, checkpoint_program(process))
    call = {"id": "provider-call-one", "name": "unit", "args": {"value": "one"}}
    paused = batch.execute_live_batch([call])
    assert paused.result and not calls and process.pending_decision["kind"] == "runtime_checkpoint"
    original = process.pending_decision["decision_id"]
    saved = fixture_answer(process, selected="Give a hint")
    assert batch.execute_resumed_batch(saved) is not None
    assert not calls and process.pending_decision["decision_id"] != original
    assert "Read the exact operation arguments." in process.pending_decision["question"]
    saved = fixture_answer(process, selected="Skip this checkpoint")
    assert batch.execute_resumed_batch(saved) is None
    assert calls == ["one"] and not process.approval_grants, str(process.session.messages[-1])
    assert '"phase":"skipped"' in app.envelope()
    assert batch.execute_live_batch([{**call, "id": "provider-call-two", "args": {"value": "two"}}]).result
    assert calls == ["one"]
    saved = fixture_answer(process, selected="Disable this extension")
    assert batch.execute_resumed_batch(saved) is None
    assert calls == ["one", "two"]
    assert app.repo.instance(app.task, "checkpoints")[0]["phase"] == "suspended"
    assert '"phase":"suspended"' in app.envelope()
    assert batch.execute_live_batch([{**call, "id": "provider-call-three", "args": {"value": "three"}}]).result is None
    assert calls == ["one", "two", "three"] and not process.pending_decision


def test_prediction_and_actual_receipt_are_linked_and_question_survives_definition_update(runtime):
    app, process, calls, tool, ctx, batch, store = runtime
    current = enable(app, process, checkpoint_program(process))
    call = {"id": "kept-call", "name": "unit", "args": {"value": "real"}}
    assert batch.execute_live_batch([call]).result
    question = deepcopy(process.pending_decision)
    raw = checkpoint_program(process)
    raw["elements"][1]["parameters"]["question"] = "The new definition's question"
    preview = app.invoke({"operation": "preview", "program": raw})
    saved = app.invoke({"operation": "publish", "program": raw, "expected_head": preview["expected_head"], "preview_digest": preview["preview_digest"]})
    app.coordinator.request(app.task, "checkpoints", saved["reference"], expected_revision=current["revision"], sources=saved["definition"]["sources"])
    assert app.coordinator.settle(app.task, "checkpoints")["epoch"] == 2
    assert process.pending_decision == question
    pending = fixture_answer(process, text="I predict an ok result")
    assert batch.execute_resumed_batch(pending) is None
    assert calls == ["real"], str(process.session.messages[-1])
    unit = process.tool_receipts[-1]["harness_unit"]
    value = app.repo.instance(app.task, "checkpoints")[0]
    assert value["holds"][unit]["answer_id"] == question["decision_id"]
    assert value["holds"][unit]["epoch"] == 1 and value["holds"][unit]["phase"] == "compared"
    assert process.tool_receipts[-1]["harness_instances"][0]["epoch"] == 2
    assert "I predict an ok result" in app.envelope()
    assert "actual_result" in app.envelope() and "question_epoch" in app.envelope()


def test_exit_cannot_be_overridden_by_older_sources_after_reload_but_later_user_can_reactivate(runtime):
    app, process, calls, tool, ctx, batch, store = runtime
    initial_refs = list(source_receipts(process))
    enable(app, process, checkpoint_program(process))
    assert batch.execute_live_batch([{'id': 'first', 'name': 'unit', 'args': {'value': 'one'}}]).result
    pending = fixture_answer(process, selected='Disable this extension')
    assert batch.execute_resumed_batch(pending) is None and calls == ['one']
    fresh = Application(process)
    value = fresh.repo.instance(app.task, 'checkpoints')[0]
    assert value['phase'] == 'suspended' and value['user_control']['action'] == 'suspend'
    activation = {'operation': 'activate', 'instance_id': value['id'], 'expected_revision': value['revision'], 'source_refs': initial_refs}
    with pytest.raises(ValueError, match='NEWER_USER_CONTROL'):
        fresh.invoke(activation)
    assert fresh.repo.instance(app.task, value['id'])[0] == value
    from backend.core.loop.mechanism_lifecycle import next_authority_sequence
    process.session.host_ledger.append({'event': 'task_user_instruction_received', 'task_id': app.task,
        'source_ref': 'user:later-unit-fixture', 'content': 'Unit fixture: explicitly reactivate the same extension now.',
        'authority_sequence': next_authority_sequence(process.session)})
    resumed = fresh.invoke({**activation, 'source_refs': ['user:later-unit-fixture']})['instance']
    assert resumed['phase'] == 'ready' and resumed['epoch'] == value['epoch'] + 1
    assert not process.approval_grants and calls == ['one']


def test_exit_hold_release_and_lifecycle_staging_are_one_instance_transaction(runtime, monkeypatch):
    app, process, calls, tool, ctx, batch, store = runtime
    enable(app, process, checkpoint_program(process))
    assert batch.execute_live_batch([{'id': 'first', 'name': 'unit', 'args': {'value': 'one'}}]).result
    fixture_answer(process, selected='Disable this extension')
    value = app.repo.instance(app.task, 'checkpoints')[0]
    unit = next(iter(value['holds']))
    original = app.coordinator.request
    monkeypatch.setattr(app.coordinator, 'request', lambda *a, **k: (_ for _ in ()).throw(OSError('Unit fixture staging failure')))
    with pytest.raises(OSError, match='staging failure'):
        app.interactions.admit(unit)
    assert app.repo.instance(app.task, value['id'])[0]['holds'][unit]['phase'] == 'waiting'
    assert not calls
    monkeypatch.setattr(app.coordinator, 'request', original)
    app.interactions.admit(unit)
    current = app.repo.instance(app.task, value['id'])[0]
    assert current['holds'][unit]['phase'] == 'exited' and current['phase'] == 'suspended'
    before = deepcopy(current)
    app.interactions.admit(unit)
    assert app.repo.instance(app.task, value['id'])[0] == before and not calls


def test_retained_readonly_review_can_report_old_generation_while_owner_aligns_new_scope(runtime, monkeypatch):
    from types import SimpleNamespace
    from backend.core.mechanism_runtime.reviews import review_service, validate_boundary
    from backend.core.loop.task_maintenance import add_change, guard
    app, owner, calls, tool, ctx, batch, store = runtime
    manager = owner._manager
    children = []
    def spawn(**arguments):
        origin = arguments['retained_boundary']
        validate_boundary(owner, origin)
        child = manager.fork(owner.process_id, 'reviewer', ToolRegistry(['read_file', 'complete_review']), 8, RingLevel.RING_3,
            workspace_path=owner.workspace_root, task_id='unit-frozen-review', actor_kind='reviewer', capability_profile_id='review.independent')
        child.review_target_id = owner.process_id
        child.runtime_preferences['retained_review_boundary'] = deepcopy(origin)
        children.append(child)
        return child
    def wait(identifier, **kwargs):
        child = children[-1]
        assert child.process_id == identifier
        add_change(owner, 'Unit fixture: update the next completion policy', identifier='next-policy', source_ref='unit-fixture',
            expected_task_id=app.task, expected_contract_revision=0, store=store)
        assert guard(owner)[0] == 'TASK_MAINTENANCE_REQUIRED'
        assert guard(child) is None
        origin = child.runtime_preferences['retained_review_boundary']
        origin['epoch'] += 1
        assert guard(child)[0] == 'TASK_MAINTENANCE_REQUIRED'
        origin['epoch'] -= 1
        child.review_claim = {'verdict': 'approved', 'summary': 'Synthetic component review only', 'evidence': []}
        child.status = ProcessStatus.COMPLETED
        assert not child.tool_registry.has('write_file') and not child.approval_grants
    monkeypatch.setattr(manager, 'wait', wait)
    app.services['agent.review'] = review_service(SimpleNamespace(process=owner, manager=manager, spawn_child=spawn), app)
    app.coordinator.capabilities = tuple(app.services)
    raw = {'schema_version': 2, 'id': 'frozen-review', 'intent': 'Unit fixture retained completion report',
        'source_refs': list(source_receipts(owner)), 'elements': [
            {'id': 'event', 'use': 'runtime.event', 'parameters': {'boundaries': ['completion']}},
            {'id': 'review', 'use': 'agent.review', 'parameters': {'instructions': 'Unit fixture read-only review', 'max_steps': 8}}],
        'bindings': [{'kind': 'trigger', 'from': {'node': 'event', 'port': 'go'}, 'to': {'node': 'review', 'port': 'go'}},
            {'kind': 'data', 'from': {'node': 'event', 'port': 'event'}, 'to': {'node': 'review', 'port': 'event'}}]}
    enable(app, owner, raw)
    result = app.dispatch('completion', 'unit-completion', {'candidate': 'Unit fixture candidate', 'receipts': []})
    assert result[0]['phase'] == 'complete'
    from backend.core.loop.alignment_runtime import AlignmentRuntime
    from backend.core.mechanism_runtime.application import projection
    from backend.core.loop.runtime_outcomes import _make_result
    payload = AlignmentRuntime(owner).payload(candidate='Unit fixture candidate')
    observed = payload['harness_semantics']['instances'][0]['execution']['reviews']
    assert observed == projection(owner)[0]['completion']['reviews']
    assert observed[0]['verdict'] == 'approved' and observed[0]['phase'] == 'complete'
    assert observed[0]['candidate_digest'] == payload['candidate_digest']
    assert observed[0]['definition_ref'] == result[0]['definition_ref']
    assert observed[0]['event_id'] == result[0]['event_id']
    assert observed[0]['authority_granted'] is False
    other = AlignmentRuntime(owner).payload(candidate='A changed completion claim')
    assert other['candidate_digest'] != observed[0]['candidate_digest']
    # Reporting an old stage must neither apply pending scope nor certify it.
    assert not owner.read_context_snapshot()[0]['alignment_runtime'].get('delivery')
    outcome = _make_result(owner, owner.session, 'fixture only')
    assert outcome['metadata']['completion_stages'][0]['completion']['reviews'] == observed
    assert guard(owner)[0] == 'TASK_MAINTENANCE_REQUIRED'
    assert guard(children[0])[0] == 'TASK_MAINTENANCE_REQUIRED'  # The retained boundary has ended.


def test_actual_tool_invocation_retains_generation_until_result_not_just_before_hook(runtime):
    app, process, calls, tool, ctx, batch, store = runtime
    raw = {"schema_version": 2, "id": "notes", "intent": "fixture tool-generation retention", "source_refs": list(source_receipts(process)),
        "elements": [{"id": "event", "use": "runtime.event"}, {"id": "note", "use": "context.project", "parameters": {"content": "old"}}],
        "bindings": [{"kind": "trigger", "from": {"node": "event", "port": "go"}, "to": {"node": "note", "port": "go"}}]}
    current = enable(app, process, raw)
    entered, release = threading.Event(), threading.Event()
    def run(args):
        entered.set()
        assert release.wait(5)
        calls.append(args["value"])
        return {"ok": True}
    tool = AgentTool(name="unit", description="Fixture effect", parameters=tool.parameters,
        effect=ToolEffect.PROCESS, execution_contract=data_broker("test.fixture"), execute=run)
    call = {"id": "long-call", "name": "unit", "args": {"value": "one"}}
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(ToolPipeline().execute, call, tool, ctx, "actual-effect", 0)
        assert entered.wait(5), running.result().formatted
        raw["elements"][1]["parameters"]["content"] = "new"
        preview = app.invoke({"operation": "preview", "program": raw})
        definition = app.invoke({"operation": "publish", "program": raw, "expected_head": preview["expected_head"], "preview_digest": preview["preview_digest"]})
        app.coordinator.request(app.task, "notes", definition["reference"], expected_revision=current["revision"], sources=definition["definition"]["sources"])
        assert app.coordinator.settle(app.task, "notes")["phase"] == "pending_activation"
        assert not process.cancel_requested
        release.set()
        result = running.result()
    assert not result.is_error and calls == ["one"]
    assert result.receipt["harness_instances"][0]["epoch"] == 1
    assert app.repo.instance(app.task, "notes")[0]["epoch"] == 2


def test_completion_returns_are_durable_and_bound_then_use_real_question_flow(runtime):
    app, process, calls, tool, ctx, batch, store = runtime
    raw = {"schema_version": 2, "id": "bounded-return", "intent": "fixture bounded additional completion strategy", "source_refs": list(source_receipts(process)),
        "elements": [{"id": "event", "use": "runtime.event", "parameters": {"boundaries": ["completion"]}},
            {"id": "return", "use": "completion.return", "parameters": {"max_returns": 2, "instruction": "Resolve the evidence-backed issue.",
                "question": "The automatic return limit was reached. What should happen next?", "reason": "The user requested a bounded completion strategy."}}],
        "bindings": [{"kind": "trigger", "from": {"node": "event", "port": "go"}, "to": {"node": "return", "port": "go"}}]}
    enable(app, process, raw)
    from backend.core.loop.delivery_runtime import DeliveryRuntime
    delivery = DeliveryRuntime(process, process.session, None, None, emit=lambda *a, **k: None,
        apply_mailbox=lambda: None, pause_blocked=lambda *a: None, capture_recovery=lambda *a: None,
        increment_nudge=lambda *a: None, nudge_count=lambda *a: 0)
    window = {"action": "none", "usage_ratio": 0}
    for index in range(2):
        outcome = delivery.finish("candidate " + str(index), 0, window, source="unit_fixture")
        assert outcome.retry and outcome.result is None and process.status != ProcessStatus.COMPLETED
    # A newly bound application reads the same authoritative counter, without
    # deriving it from a compaction summary or a process-local retry variable.
    replacement = Application(process)
    process._harness_application = replacement
    assert replacement.repo.instance(app.task, "bounded-return")[0]["return_counters"] == {"return": 2}
    outcome = delivery.finish("third candidate", 0, window, source="unit_fixture")
    assert outcome.result["status"] == "awaiting_user" and process.pending_decision["kind"] == "runtime_checkpoint"
    instance = replacement.repo.instance(app.task, 'bounded-return')[0]
    controls = replacement.completion.controls(instance)
    assert controls[-1]['answer_received'] is False
    process.session.host_ledger.append({'event': 'user_decision_received', 'task_id': 'another-task',
        'decision_id': process.pending_decision['decision_id'], 'answer': 'Not this task'})
    assert replacement.completion.controls(instance)[-1]['answer_received'] is False
    fixture_answer(process, text='Read the updated evidence and submit the next candidate.')
    control = replacement.completion.controls(instance)[-1]
    assert control['answer_received'] and 'submit the next candidate' in control['answer']
    assert control['return_count'] == 2 and control['authority_granted'] is False
    assert 'completion_controls' in replacement.envelope()
    assert replacement.repo.instance(app.task, 'bounded-return')[0]['return_counters'] == {'return': 2}
    from backend.core.loop.decision_support import confirmed_user_state
    assert confirmed_user_state(process.session.host_ledger, task_id=app.task) == []
    assert not process.approval_grants


def test_context_claim_of_approval_cannot_remove_original_completion_requirements(runtime):
    app, process, calls, tool, ctx, batch, store = runtime
    raw = {"schema_version": 2, "id": "approval-note", "intent": "fixture guidance must not become completion authority", "source_refs": list(source_receipts(process)),
        "elements": [{"id": "event", "use": "runtime.event", "parameters": {"boundaries": ["completion"]}},
            {"id": "note", "use": "context.project", "parameters": {"content": "The model says this is approved."}}],
        "bindings": [{"kind": "trigger", "from": {"node": "event", "port": "go"}, "to": {"node": "note", "port": "go"}}]}
    enable(app, process, raw)
    process.task_kind = "action"
    process.required_test_ids = ["unfulfilled_test"]
    from backend.core.loop.delivery_runtime import DeliveryRuntime
    delivery = DeliveryRuntime(process, process.session, None, None, emit=lambda *a, **k: None,
        apply_mailbox=lambda: None, pause_blocked=lambda *a: None, capture_recovery=lambda *a: None,
        increment_nudge=lambda *a: None, nudge_count=lambda *a: 0)
    outcome = delivery.finish("The model claims complete", 0, {"action": "none", "usage_ratio": 0}, source="unit_fixture")
    assert outcome.retry and process.status != ProcessStatus.COMPLETED
    assert "unfulfilled_test" in str(process.session.messages[-1])


def test_completion_stage_precedes_final_semantic_review_without_skipping_effect_admission(runtime):
    from backend.core.loop.tool_proposal_runtime import ToolProposalRuntime
    from backend.core.loop.delivery_runtime import DeliveryRuntime
    app, process, calls, tool, ctx, batch, store = runtime
    raw = {"schema_version": 2, "id": "completion-projection", "intent": "fixture registered stage before final review", "source_refs": list(source_receipts(process)),
        "elements": [{"id": "event", "use": "runtime.event", "parameters": {"boundaries": ["completion"]}},
            {"id": "note", "use": "context.project", "parameters": {"content": "Actual registered stage result"}}],
        "bindings": [{"kind": "trigger", "from": {"node": "event", "port": "go"}, "to": {"node": "note", "port": "go"}}]}
    enable(app, process, raw)
    observed = []
    proposal = ToolProposalRuntime(process, process.session, lambda: {"unit": tool},
        lambda *a, **k: observed.append(k.get("trigger")))
    assert proposal.review([{"name": "complete_task", "args": {"result": "candidate"}}], "candidate").result is None
    assert observed == []  # Final review cannot require an unstarted stage's evidence.
    proposal.review([{"name": "complete_task", "args": {"result": "candidate"}}, {"name": "unit", "args": {"value": "x"}}], "candidate")
    assert observed == ["execution"]  # Domain effects still pass normal admission.
    def final_review(candidate):
        assert "Actual registered stage result" in app.envelope()
        return {"alignment_repair": "Unit fixture: actual result is insufficient"}
    delivery = DeliveryRuntime(process, process.session, None, None, emit=lambda *a, **k: None,
        apply_mailbox=lambda: None, pause_blocked=lambda *a: None, capture_recovery=lambda *a: None,
        increment_nudge=lambda *a: None, nudge_count=lambda *a: 0, final_review=final_review)
    outcome = delivery.finish("candidate", 0, {"action": "none", "usage_ratio": 0}, source="unit_fixture")
    assert outcome.retry and process.status != ProcessStatus.COMPLETED
    assert "actual result is insufficient" in str(process.session.messages[-1])


def test_semantic_lifecycle_preview_excludes_journals_and_explicit_upgrade_keeps_instance(runtime):
    app, process, calls, tool, ctx, batch, store = runtime
    raw = inbox_program(process)
    current = enable(app, process, raw)
    app.receive_discussion(fixture_discussion(app, "Unit fixture candidate to retain."))
    # An audit journal can grow independently of the semantic activation input.
    app.repo.mutate(app.task, current["id"], lambda value: value.__setitem__("audit_fixture", "x" * 150000))
    raw["intent"] = "Fixture: revised collection"
    preview = app.invoke({"operation": "preview", "program": raw})
    app.invoke({"operation": "publish", "program": raw, "preview_digest": preview["preview_digest"], "expected_head": preview["expected_head"]})
    args = {"operation": "activate", "instance_id": current["id"], "expected_revision": current["revision"],
        "definition_revision": 2, "source_refs": list(source_receipts(process))}
    semantic = app.lifecycle_preview(args)
    import json
    assert len(json.dumps(semantic)) < 20000 and "audit_fixture" not in json.dumps(semantic)
    assert semantic["target_definition_revision"] == 2
    active = app.invoke(args)["instance"]
    assert active["definition_revision"] == 2 and active["epoch"] == 2 and len(active["inbox"]) == 1


def test_stage_entry_defers_only_final_semantic_certificate_and_never_is_delivery(runtime):
    from backend.core.loop.completion_protocol import HostCompletionEvaluator
    app, process, calls, tool, ctx, batch, store = runtime
    raw = {"schema_version": 2, "id": "entry-fixture", "intent": "fixture stage entry versus final delivery", "source_refs": list(source_receipts(process)),
        "elements": [{"id": "event", "use": "runtime.event", "parameters": {"boundaries": ["completion"]}},
            {"id": "note", "use": "context.project", "parameters": {"content": "fixture stage"}}],
        "bindings": [{"kind": "trigger", "from": {"node": "event", "port": "go"}, "to": {"node": "note", "port": "go"}}]}
    current = enable(app, process, raw)
    process.task_kind = "answer"
    process.update_context_snapshot(lambda value: {**value, "alignment_runtime": {"enforced": True,
        "task_id": process.active_task_id, "process_id": process.process_id, "request": "fixture answer"}})
    assert HostCompletionEvaluator.evaluate_stage_entry(process, "fixture candidate").allowed
    assert not HostCompletionEvaluator.evaluate(process, "fixture candidate").allowed
    assert process.status != ProcessStatus.COMPLETED
    process.task_kind, process.required_test_ids = "action", ["unit_unfulfilled_test"]
    missing = HostCompletionEvaluator.evaluate_stage_entry(process, "fixture candidate")
    assert not missing.allowed and any("unit_unfulfilled_test" in reason for reason in missing.reasons)
    app.invoke({"operation": "detach", "instance_id": current["id"], "expected_revision": current["revision"],
                "source_refs": list(source_receipts(process))})
    with pytest.raises(ValueError, match="STAGE_NOT_REGISTERED"):
        HostCompletionEvaluator.evaluate_stage_entry(process, "fixture candidate")


def test_completion_generation_change_during_final_review_cannot_mix_old_result_with_new_policy(runtime):
    from backend.core.loop.delivery_runtime import DeliveryRuntime
    app, process, calls, tool, ctx, batch, store = runtime
    raw = {"schema_version": 2, "id": "completion-generation", "intent": "fixture atomic stage generation", "source_refs": list(source_receipts(process)),
        "elements": [{"id": "event", "use": "runtime.event", "parameters": {"boundaries": ["completion"]}},
            {"id": "note", "use": "context.project", "parameters": {"content": "first generation"}}],
        "bindings": [{"kind": "trigger", "from": {"node": "event", "port": "go"}, "to": {"node": "note", "port": "go"}}]}
    original = enable(app, process, raw)
    checked = []
    def final_review(candidate):
        checked.append(app.repo.instance(app.task, original["id"])[0]["epoch"])
        if len(checked) == 1:
            raw["elements"][1]["parameters"]["content"] = "second generation"
            preview = app.preview({"program": raw})
            app.invoke({"operation": "publish", "program": raw, "preview_digest": preview["preview_digest"], "expected_head": preview["expected_head"]})
            app.invoke({"operation": "activate", "instance_id": original["id"], "definition_revision": 2,
                "expected_revision": original["revision"], "source_refs": list(source_receipts(process))})
        return None
    process.task_kind = "answer"
    delivery = DeliveryRuntime(process, process.session, None, None, emit=lambda *a, **k: None,
        apply_mailbox=lambda: None, pause_blocked=lambda *a: None, capture_recovery=lambda *a: None,
        increment_nudge=lambda *a: None, nudge_count=lambda *a: 0, final_review=final_review)
    window = {"action": "none", "usage_ratio": 0}
    assert delivery.finish("same candidate", 0, window, source="unit_fixture").retry
    assert process.status != ProcessStatus.COMPLETED
    second = delivery.finish("same candidate", 0, window, source="unit_fixture")
    assert second.result["status"] == "completed" and checked == [1, 2]
    value = app.repo.instance(app.task, original["id"])[0]
    assert {run["epoch"] for run in value["runs"].values()} == {1, 2}


def test_context_epoch_is_checkpointed_before_any_registered_compaction_callback(runtime):
    from backend.core.loop.context_compaction import ContextCompaction
    app, process, calls, tool, ctx, batch, store = runtime
    raw = {"schema_version": 2, "id": "compaction-fixture", "intent": "fixture durable context lifecycle", "source_refs": list(source_receipts(process)),
        "elements": [{"id": "event", "use": "runtime.event", "parameters": {"boundaries": ["after_compaction"]}},
            {"id": "note", "use": "context.project", "parameters": {"content": "New durable context epoch"}}],
        "bindings": [{"kind": "trigger", "from": {"node": "event", "port": "go"}, "to": {"node": "note", "port": "go"}}]}
    enable(app, process, raw)
    checked = []
    def project(context, payload):
        assert store.load_session_state(process.process_id)["context_epoch"] == process.session.context_epoch == 1
        checked.append(True)
        return app.project_context(context, payload)
    app.services["context.project"] = project
    class FixtureWindow:
        last_compaction_error = ""
        def compact(self, session, *args, **kwargs):
            session.context_epoch += 1
            return True
    runtime = ContextCompaction(process, process.session, None, lambda: FixtureWindow(), None, lambda *a, **k: None, None)
    assert runtime.run("unit_fixture") == (True, None)
    assert checked == [True]


@pytest.mark.parametrize("actor,profile", [("supervisor", "supervisor.answer"), ("reviewer", "review.independent"), ("worker", "governance.observe")])
def test_readonly_actors_cannot_publish_project_mechanisms_even_with_real_sources(runtime, actor, profile):
    app, process, calls, tool, ctx, batch, store = runtime
    process.actor_kind, process.capability_profile_id = actor, profile
    catalog = app.invoke({"operation": "catalog"})
    assert catalog["configuration_allowed"] is False
    with pytest.raises(ValueError, match="CONFIGURATION_CAPABILITY_REQUIRED"):
        app.invoke({"operation": "publish", "program": checkpoint_program(process), "expected_head": None, "preview_digest": "anything"})
    assert app.repo.definition("checkpoints")[0] is None
