"""Rejected readiness exposes identities without resolving or dropping them."""
import copy
from types import SimpleNamespace

import pytest

from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.alignment_review import AlignmentReview, AlignmentReviewHooks
from tests.test_alignment_semantics import process, pending_reply, interpretation


def test_readiness_rejection_names_actual_blocker_and_preserves_state(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload('Implement the agreed change')
    w, card, node = pending_reply(p, 'First revise the proposed approach, then ask before implementation.')
    payload = runtime.payload()
    before = copy.deepcopy((p.context_snapshot, p.session.host_ledger, p.pending_decision))
    with pytest.raises(ValueError, match='discard unresolved') as error:
        runtime.apply({'status':'ready','questions':[],'interpretations':[]}, payload)
    details = error.value.validation_details
    assert details['code'] == 'UNRESOLVED_ACTUAL_QUESTIONS'
    assert details['blocked_questions'] == [{'node_id':node,'decision_id':card['decision_id'],'status':w.status()['nodes'][0]['status']}]
    assert details['blocked_count'] == 1 and not details['partial']
    assert (p.context_snapshot, p.session.host_ledger, p.pending_decision) == before
    assert not w.status()['complete']


def test_compiler_retry_receives_blocker_identity_and_still_needs_real_interpretation(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload('Implement the agreed change')
    text = 'First revise the proposed approach, then ask before implementation.'
    w, card, node = pending_reply(p, text)
    calls = []
    def assess(payload, **kwargs):
        calls.append(payload)
        if len(calls) == 1:
            return {'status':'ready','questions':[],'interpretations':[]}
        diagnostic = payload['host_validation_details']['blocked_questions'][0]
        assert diagnostic['node_id'] == node and diagnostic['decision_id'] == card['decision_id']
        assert any(a['answer'] == text for a in payload['actual_answers'])
        return {'status':'ready','questions':[], 'interpretations':[interpretation(node,card,text)]}
    noop = lambda *args, **kwargs: None
    hooks = AlignmentReviewHooks(emit=noop,frontend_emit=None,notice=noop,refresh_prompt=noop,
        before_attempt=noop,route_notice=noop,result=noop,cancellation=noop,budget_exhausted=noop)
    result = AlignmentReview(p,SimpleNamespace(assess_alignment=assess),hooks).check(
        'Implement the agreed change',trigger='contract',proposed_contracts=[{'goal':'Implement the agreed change'}])
    assert result is None and len(calls) == 2
    assert w.status()['complete']
    record = w._load()['records'][node]
    assert record['decision_id'] == card['decision_id']
