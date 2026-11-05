from copy import deepcopy

import pytest

from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
from backend.core.loop.session import AgentSession
from backend.core.loop.discussion_adoption import inherit_interrupted_task


def test_interrupt_keeps_task_evidence_obligations_and_decisions_without_grants():
    session = AgentSession()
    source = AgentProcess('old', 'supervisor', RingLevel.RING_0, actor_kind='supervisor',
        session=session, active_task_id='original', task_kind='action',
        status=ProcessStatus.CANCELLED, task_description='Original goal')
    source.tool_receipts = [{'receipt_id': 'actual-write', 'task_id': 'original', 'succeeded': True}]
    source.required_test_ids = ['actual-test']
    source.task_constraints = ['Keep accepted API']
    source.approval_grants = [{'tool_name': 'write_file'}]
    source.delegated_contracts = {'required-child': {'required_for_parent_completion': True}}
    context = {'engineering_workflow': {'task_id': 'original', 'records': {'decision': {'decision_id': 'real-answer'}}},
               'alignment_runtime': {'task_id': 'original', 'process_id': 'old', 'request': 'Original goal',
                                     'delivery': {'candidate_digest': 'stale'}, 'admission_review': {'status': 'ready'}}}
    target = AgentProcess('new', 'supervisor', RingLevel.RING_0, actor_kind='supervisor',
        session=session, active_task_id='original', context_snapshot=deepcopy(context))
    target.delegated_contracts = {'required-child': {'required_for_parent_completion': False}}
    inherit_interrupted_task(target, source)
    assert target.tool_receipts == source.tool_receipts and target.tool_receipts is not source.tool_receipts
    assert target.required_test_ids == ['actual-test'] and target.task_constraints == ['Keep accepted API']
    assert target.task_kind == 'action' and target.task_description == 'Original goal'
    assert target.delegated_contracts['required-child']['required_for_parent_completion']
    assert not target.approval_grants and not target.pending_decision and not target.completion_claim
    result = target.read_context_snapshot()[0]
    assert result['engineering_workflow'] == context['engineering_workflow']
    assert result['alignment_runtime']['process_id'] == 'new'
    assert 'delivery' not in result['alignment_runtime'] and 'admission_review' not in result['alignment_runtime']
    assert session.host_ledger[-1]['predecessor_process_id'] == 'old'


@pytest.mark.parametrize('change', ['task', 'session', 'live'])
def test_interrupt_evidence_cannot_be_imported_into_unrelated_or_live_task(change):
    session = AgentSession()
    source = AgentProcess('old', 'worker', RingLevel.RING_3, session=session,
                          active_task_id='original', status=ProcessStatus.CANCELLED)
    target = AgentProcess('new', 'worker', RingLevel.RING_3, session=session, active_task_id='original')
    if change == 'task': target.active_task_id = 'unrelated'
    if change == 'session': target.session = AgentSession()
    if change == 'live': source.status = ProcessStatus.RUNNING
    with pytest.raises(ValueError, match='CONTINUATION_CHANGED'):
        inherit_interrupted_task(target, source)
    assert target.tool_receipts == [] and not target.session.host_ledger
