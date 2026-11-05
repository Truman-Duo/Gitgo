"""Real execution/storage boundaries with explicitly stubbed semantic judgments.

These are development regressions, not real API or independent semantic scores.
"""
from copy import deepcopy
from types import SimpleNamespace
import pytest

from tests.test_mechanism_interactions import runtime
from tests.test_mechanism_capability import declaration
from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.alignment_routing import REVIEW_POLICY, reuse_assessment
from backend.core.loop.tool_proposal_runtime import ToolProposalRuntime
from backend.core.loop.task_contract import get_task_contract


def setup(runtime):
    from backend.core.loop.protocol_tools.harness_composition import install as harness
    from backend.core.loop.protocol_tools.task_contracts import install as contracts
    app, p, effects, unit, ctx, batch, store = runtime
    p.actor_kind = 'supervisor'
    p.capability_profile_id = 'supervisor.control'
    ctx_tools = SimpleNamespace(process=p, available_tools={}, workspace_path=ctx.workspace_path,
        manager=p._manager, engineering=ctx.artifacts['engineering_workflow'], tools={'unit': unit},
        llm_provider=None, on_stream_event=None, dispatcher=None)
    harness(ctx_tools)
    contracts(ctx_tools)
    tools = ctx_tools.tools
    for name in tools:
        p.tool_registry.add_scoped(name)
    batch.get_tools = lambda: tools
    judgments = []
    reviewer = AlignmentRuntime(p)
    def review(*args, **kwargs):
        payload = reviewer.payload(proposed_contracts=kwargs.get('proposed_contracts'))
        p.context_snapshot['alignment_runtime']['review_policy'] = REVIEW_POLICY
        if reuse_assessment(p, payload):
            return None
        judgments.append(deepcopy(payload.get('proposed_contracts') or []))
        reviewer.apply({'status': 'ready', 'questions': [], 'interpretations': []}, payload)
    batch.check_alignment = review
    return tools, judgments, review


def calls():
    return [
        {'id': 'apply', 'name': 'harness_mechanism', 'args': {'operation': 'apply', 'declaration': declaration()}},
        {'id': 'contract', 'name': 'declare_task_contract', 'args': {
            'goal': 'Keep the public interface', 'execution_mode': 'self_execute',
            'delegation_required': False, 'deliverables': [{'kind': 'response', 'required': True}],
            'acceptance_criteria': ['Explain checked behavior'], 'uncertainties': [],
            'requires_user_decision': False, 'user_requested_delegation': False, 'user_request_evidence': ''}},
        {'id': 'effect', 'name': 'unit', 'args': {'value': 'once'}},
    ]


def announce(p, values):
    p.session.append_assistant('', tool_calls=deepcopy(values))


def test_control_then_contract_uses_each_committed_basis_without_model_replay(runtime):
    app, p, effects, _, _, batch, _ = runtime
    tools, judgments, review = setup(runtime)
    values = calls()
    announce(p, values)
    assert not ToolProposalRuntime(p, p.session, lambda: tools, review).review(values, '').result
    assert len(judgments) == 1 and len(judgments[0]) == 1
    assert app.repo.definition('guidance') == (None, None)
    outcome = batch.execute_live_batch(values)
    assert outcome.result is None and not outcome.retry
    assert all(not result.is_error for result in outcome.results), [(r.error, r.data) for r in outcome.results if r.is_error]
    assert len(judgments) == 3  # Configuration, task contract, then actual business readiness.
    assert judgments[0][0]['kind'] == 'harness_capability_deployment'
    assert judgments[1][0]['goal'] == 'Keep the public interface'
    assert judgments[2][0]['kind'] == 'business_readiness'
    assert effects == ['once'] and get_task_contract(p)['revision'] == 1
    assert app.repo.definition('guidance')[0]['revision'] == 1
    assert not p.approval_grants and p.capability_lease is None
    outputs = [m for m in p.session.messages if m.get('role') == 'tool']
    assert [m['tool_call_id'] for m in outputs] == ['apply', 'contract', 'effect']
    next_call = [{'id': 'another-effect', 'name': 'unit', 'args': {'value': 'next'}}]
    announce(p, next_call)
    outcome = batch.execute_live_batch(next_call)
    assert all(not result.is_error for result in outcome.results)
    assert len(judgments) == 3 and effects == ['once', 'next']


def test_two_read_transport_control_calls_are_serial_and_compile_after_commit(runtime):
    app, p, *_ = runtime
    batch = runtime[5]
    tools, judgments, review = setup(runtime)
    values = [calls()[0], {'id': 'revision', 'name': 'harness_mechanism', 'args': {
        'operation': 'revise', 'definition_id': 'guidance',
        'revision': {'intent': 'Keep both interface and behavior stable'}}}]
    announce(p, values)
    ToolProposalRuntime(p, p.session, lambda: tools, review).review(values, '')
    result = batch.execute_live_batch(values)
    assert all(not item.is_error for item in result.results), [i.formatted for i in result.results]
    assert app.repo.definition('guidance')[0]['revision'] == 2
    assert len(judgments) == 2 and judgments[1][0]['previous'] is not None


@pytest.mark.parametrize('terminal', ['repair', 'budget', 'cancel'])
def test_later_review_failure_retains_committed_prefix_and_pairs_untouched_suffix(runtime, terminal):
    app, p, effects, _, _, batch, _ = runtime
    tools, _, review = setup(runtime)
    values = calls()
    announce(p, values)
    ToolProposalRuntime(p, p.session, lambda: tools, review).review(values, '')
    def stop(*args, **kwargs):
        subjects = kwargs.get('proposed_contracts') or []
        if subjects and not subjects[0].get('kind'):
            return {'alignment_repair': 'Fixture semantic contradiction'} if terminal == 'repair' else {'status': terminal}
        return review(*args, **kwargs)
    batch.check_alignment = stop
    outcome = batch.execute_live_batch(values)
    assert (outcome.retry if terminal == 'repair' else outcome.result['status'] == terminal)
    assert app.repo.definition('guidance')[0]['revision'] == 1
    assert get_task_contract(p).get('revision', 0) == 0 and effects == []
    assert p.pending_tool_batch is None and not p.approval_grants
    outputs = [m for m in p.session.messages if m.get('role') == 'tool']
    assert [m['tool_call_id'] for m in outputs] == ['apply', 'contract', 'effect']
    assert [m.get('is_error', False) for m in outputs] == [False, True, True]


def test_later_real_decision_checkpoints_only_suffix_and_resumes_exact_calls(runtime):
    from backend.core.loop.decision_support import create_user_decision
    from backend.core.loop.models import ProcessStatus
    from backend.core.loop.recovery import restore_incomplete_processes
    from backend.core.loop.manager import AgentProcessManager
    app, p, effects, _, _, batch, store = runtime
    tools, _, review = setup(runtime)
    values = calls()
    announce(p, values)
    ToolProposalRuntime(p, p.session, lambda: tools, review).review(values, '')
    def ask(*args, **kwargs):
        subjects = kwargs.get('proposed_contracts') or []
        if subjects and not subjects[0].get('kind'):
            create_user_decision(p, {'question': 'Fixture ownership choice?', 'kind': 'clarification',
                'why_user_must_decide': 'Fixture unresolved owner choice', 'options': [], 'allow_free_form': True})
            p.status = ProcessStatus.AWAITING_USER
            return {'status': 'awaiting_user'}
        return review(*args, **kwargs)
    batch.check_alignment = ask
    assert batch.execute_live_batch(values).result['status'] == 'awaiting_user'
    state = store.load_process_state(p.process_id)
    retained = state['runtime_state']['pending_tool_batch']
    assert retained['semantic_boundary'] and retained['tool_calls'] == values[1:]
    assert app.repo.definition('guidance')[0]['revision'] == 1 and effects == []
    # Rehydrate through the product checkpoint codec. The unit test supplies
    # a new semantic judgment; it does not invent user approval or a grant.
    recovered = AgentProcessManager()
    recovered.storage, recovered.session_store = store._storage, store
    restore_incomplete_processes(store, recovered, p.workspace_root or p.worktree_path,
                                include_process_ids=[p.process_id])
    cold = recovered.get(p.process_id)
    assert cold.pending_tool_batch == retained
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.engineering_workflow import EngineeringWorkflow
    from backend.core.loop.tool_batch_runtime import ToolBatchRuntime
    from backend.core.loop.loop_guard import LoopGuard
    from backend.core.mechanism_runtime.application import Application
    import time
    cold.pending_decision = None
    cold.pending_tool_batch = None
    cold.status = ProcessStatus.RUNNING
    cold_app = Application(cold)
    cold._harness_application = cold_app
    cold_ctx = ExecutionContext(cold, cold.session, p.workspace_root or p.worktree_path, storage=store._storage)
    cold_ctx.artifacts['engineering_workflow'] = EngineeringWorkflow(cold)
    cold_batch = ToolBatchRuntime(cold, cold.session, lambda: {}, LoopGuard(), cold_ctx,
        cold_ctx.workspace_path, time.time(), lambda *a, **k: None)
    setup((cold_app, cold, effects, runtime[3], cold_ctx, cold_batch, store))
    assert cold_batch.execute_resumed_batch(retained) is None
    assert effects == ['once'] and cold_app.repo.definition('guidance')[0]['revision'] == 1
    assert get_task_contract(cold)['revision'] == 1 and not cold.approval_grants
    outputs = [m for m in cold.session.messages if m.get('role') == 'tool']
    assert [m['tool_call_id'] for m in outputs] == ['apply', 'contract', 'effect']


def test_cancelled_batch_does_not_schedule_semantic_calls_or_effects(runtime):
    app, p, effects, _, ctx, batch, _ = runtime
    _, judgments, _ = setup(runtime)
    ctx.cancellation.set()
    outcome = batch.execute_live_batch(calls())
    assert judgments == [] and effects == []
    assert outcome.retry
    outputs = [m for m in p.session.messages if m.get('role') == 'tool']
    assert len(outputs) == 3 and all(m.get('is_error') for m in outputs)
    assert app.repo.definition('guidance') == (None, None)


@pytest.mark.parametrize('denied', [False, True])
def test_semantic_pause_inside_permission_prefix_retains_entire_tail_and_existing_denial(runtime, denied):
    from backend.core.loop.agent_tool import ApprovalMode
    from backend.core.loop.decision_support import create_user_decision
    from backend.core.loop.models import ProcessStatus
    app, p, effects, unit, _, batch, _ = runtime
    _, _, review = setup(runtime)
    values = calls()
    announce(p, values)
    unit.approval = ApprovalMode.ASK
    def ask(*args, **kwargs):
        subjects = kwargs.get('proposed_contracts') or []
        if subjects and not subjects[0].get('kind'):
            create_user_decision(p, {'question': 'Fixture unresolved choice?', 'kind': 'clarification',
                'why_user_must_decide': 'Owned by the fixture user', 'options': [], 'allow_free_form': True})
            p.status = ProcessStatus.AWAITING_USER
            return {'status': 'awaiting_user'}
        return review(*args, **kwargs)
    batch.check_alignment = ask
    suspended = batch.execute_resumed_batch({'execution_id': 'permission-fixture', 'tool_calls': values,
        'decision_action': 'deny' if denied else 'allow', 'permission_call_index': 2})
    assert suspended['status'] == 'awaiting_user'
    retained = deepcopy(p.pending_tool_batch)
    assert retained['tool_calls'] == values[1:]
    assert retained.get('prior_permission_denial') == (1 if denied else None)
    assert effects == [] and app.repo.definition('guidance')[0]['revision'] == 1
    p.pending_decision = p.pending_tool_batch = None
    p.status = ProcessStatus.RUNNING
    batch.check_alignment = review
    result = batch.execute_resumed_batch(retained)
    assert effects == [] and not p.approval_grants
    if denied:
        assert result is None and get_task_contract(p)['revision'] == 1
        outputs = [m for m in p.session.messages if m.get('role') == 'tool']
        assert [m['tool_call_id'] for m in outputs] == ['apply', 'contract', 'effect']
        assert outputs[-1]['is_error']
    else:
        assert result['status'] == 'awaiting_user'
        assert p.pending_decision['kind'] == 'permission'
        assert p.pending_tool_batch['tool_calls'] == values[2:]
