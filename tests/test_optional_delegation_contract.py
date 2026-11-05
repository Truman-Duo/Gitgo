from types import SimpleNamespace

from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.task_contract import validate_contract_proposal, compile_routing_proposal
from backend.core.loop.completion_protocol import HostCompletionEvaluator


def test_explicit_zero_preserves_creation_without_forcing_result_and_omission_stays_strict(tmp_path_factory):
    args = {'goal': 'Create an auxiliary readonly observer, do not wait for its answer',
        'execution_mode': 'either', 'user_requested_delegation': True,
        'user_request_evidence': 'Create an independent observer; its answer is not required by the parent.'}
    assert validate_contract_proposal(args, str(tmp_path_factory))['minimum_delegated_outcomes'] == 1
    proposal = validate_contract_proposal({**args, 'minimum_delegated_outcomes': 0}, str(tmp_path_factory))
    compiled, _ = compile_routing_proposal({}, proposal, workflow_started=False)
    assert compiled['delegation_required'] and compiled['minimum_delegated_outcomes'] == 0


def test_optional_observer_wait_is_not_parent_gate_but_missing_creation_and_required_child_still_block():
    parent = AgentProcess('owner', 'supervisor', RingLevel.RING_0, actor_kind='supervisor',
        task_kind='supervisor', active_task_id='task', session=AgentSession())
    child = AgentProcess('observer', 'worker', RingLevel.RING_3, parent_id='owner',
        status=ProcessStatus.AWAITING_USER, session=AgentSession())
    parent._manager = SimpleNamespace(get=lambda identifier: child if identifier == child.process_id else None)
    parent.update_context_snapshot(lambda value: {**value, 'task_contract': {
        'delegation_required': True, 'minimum_delegated_outcomes': 0}})
    reasons = []
    HostCompletionEvaluator._evaluate_supervisor(parent, 'Observer requested.', reasons)
    assert any('none exists' in reason for reason in reasons)
    parent.child_ids = [child.process_id]
    parent.delegated_contracts[child.process_id] = {'required_for_parent_completion': False}
    reasons = []
    HostCompletionEvaluator._evaluate_supervisor(parent, 'Observer actually created.', reasons)
    assert not reasons
    full = HostCompletionEvaluator.evaluate(parent, 'Parent scope finished; observer still awaits its own user.')
    assert not any('unresolved' in reason for reason in full.reasons)
    parent.delegated_contracts[child.process_id]['required_for_parent_completion'] = True
    full = HostCompletionEvaluator.evaluate(parent, 'Must still wait for this required child.')
    assert any('unresolved' in reason for reason in full.reasons)
    assert any('did not complete' in reason for reason in full.reasons)
