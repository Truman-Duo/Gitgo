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


def test_invalid_review_is_reported_as_evidence_validation_not_network_failure(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload('Implement the agreed change')
    _, card, _ = pending_reply(p, 'Keep this decision open for my reply.')
    ledger = copy.deepcopy(p.session.host_ledger)
    events, notices, calls = [], [], []
    def assess(payload, **kwargs):
        calls.append(payload)
        return {'status': 'ready', 'interpretations': [], 'questions': []}
    noop = lambda *args, **kwargs: None
    hooks = AlignmentReviewHooks(emit=lambda event, **kwargs: events.append(event), frontend_emit=None,
        notice=lambda *args, **kwargs: notices.append((args, kwargs)), refresh_prompt=noop,
        before_attempt=noop, route_notice=noop, result=lambda response, **kwargs: kwargs,
        cancellation=noop, budget_exhausted=noop)
    result = AlignmentReview(p, SimpleNamespace(assess_alignment=assess), hooks).check(
        'Implement the agreed change', candidate='The requested implementation is ready.', trigger='delivery')
    assert len(calls) == 2
    assert result['error_code'] == 'ALIGNMENT_REVIEW_FAILED'
    failure = result['error_details']['failure']
    assert failure['reason'] == 'assessment_invalid'
    assert failure['recovery_class'] == 'inspect_assessment_bindings'
    assert not failure['retryable'] and 'validation_message' in failure
    assert any(event.get('failure', {}).get('reason') == 'assessment_invalid' for event in events)
    assert any(args[0] == 'ALIGNMENT_REVIEW_FAILED' for args, _ in notices)
    assert p.session.host_ledger == ledger and not p.pending_decision


@pytest.mark.parametrize('cache_phase,code,review_state', [
    ('reuse_unresolved', 'ALIGNMENT_CACHE_INVALID', 'fresh_review_required'),
    ('remember_unresolved', 'ALIGNMENT_CACHE_RECORD_FAILED', 'applied'),
])
def test_cache_notices_do_not_replay_or_discard_a_real_answer(tmp_path_factory, monkeypatch,
                                                           cache_phase, code, review_state):
    from backend.core.errors import diagnostic_entry
    import backend.core.loop.answer_review_cache as cache
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload('Implement the agreed change')
    text = 'Use the proposed approach; proceed with implementation.'
    workflow, card, node = pending_reply(p, text)
    calls, notices = [], []

    def fail(*args, **kwargs):
        raise ValueError('synthetic invalid cache record')

    def assess(payload, **kwargs):
        calls.append(payload)
        return {'status': 'ready', 'questions': [],
                'interpretations': [interpretation(node, card, text)]}

    monkeypatch.setattr(cache, cache_phase, fail)
    noop = lambda *args, **kwargs: None
    hooks = AlignmentReviewHooks(emit=noop, frontend_emit=None,
        notice=lambda *args, **kwargs: notices.append((args, kwargs)), refresh_prompt=noop,
        before_attempt=noop, route_notice=noop, result=noop, cancellation=noop, budget_exhausted=noop)
    result = AlignmentReview(p, SimpleNamespace(assess_alignment=assess), hooks).check(
        'Implement the agreed change', trigger='contract',
        proposed_contracts=[{'goal': 'Implement the agreed change'}])
    assert result is None and len(calls) == 1
    assert workflow.status()['complete']
    actual = [(args, fields) for args, fields in notices if args[0] == code]
    assert len(actual) == 1 and actual[0][1]['review_state'] == review_state
    assert diagnostic_entry(code)['kind'] == 'warning'
    receipts = [event for event in p.session.host_ledger
                if event.get('event') == 'user_decision_interpreted' and event.get('decision_id') == card['decision_id']]
    assert len(receipts) == 1
