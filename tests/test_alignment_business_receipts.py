"""Business cards share the original workflow and retain real answer sources."""
import copy
import json

import pytest

from backend.core.loop.alignment_evidence import business_answer_receipts
from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.decision_support import create_user_decision, confirmed_user_state, validate_round_answer
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from tests.test_alignment_semantics import process, pending_reply, interpretation


def checkpoint(topic='next-phase'):
    return {'kind': 'checkpoint', 'state_topic': topic, 'question': 'Which phase now?',
        'why_user_must_decide': 'The user reserved the timing decision',
        'options': [{key: label for key in ('label', 'principle', 'immediate_effect', 'downstream_effect', 'risks', 'reversibility')}
                    for label in ('Implement now', 'Wait')]}


def receive(p, card, text):
    answers = validate_round_answer(card, json.dumps({'answers': {
        q['node_id']: text for q in card['questions']}})) if card.get('questions') else None
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': p.active_task_id,
        'decision_id': card['decision_id'], 'state_topic': card.get('state_topic', ''),
        'answer': text, **({'answers': answers} if answers else {})})
    p.pending_decision = None


def test_business_checkpoint_enters_same_dag_and_received_answer_stays_unconfirmed(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload('Implement only after the user starts the task')
    w = EngineeringWorkflow(p)
    card = w.ask_semantic(checkpoint())
    node = w.status()['nodes'][0]['id']
    text = 'Now implement the agreed change, keep the original tests.'
    receive(p, card, text)
    payload = runtime.payload()
    assert any(a['decision_id'] == card['decision_id'] and a['answer'] == text for a in payload['actual_answers'])
    assert not w.status()['complete'] and not confirmed_user_state(p.session.host_ledger)
    w.resolve_answer(node, card['decision_id'], interpretation(node, card, text))
    assert w.status()['complete']


def test_legacy_direct_reply_is_visible_and_can_answer_an_older_unresolved_facet(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload('Implement the original change')
    w, old, node = pending_reply(p, 'Wait; first inspect the current state.')
    w.resolve_answer(node, old['decision_id'], interpretation(node, old,
        'Wait; first inspect the current state.', 'deferred', 'Timing remains open'))
    direct = create_user_decision(p, checkpoint())
    text = 'Now implement. Run the original test before and after the change.'
    receive(p, direct, text)
    payload = runtime.payload()
    actual = next(a for a in payload['actual_answers'] if a['decision_id'] == direct['decision_id'])
    assert actual['unbound_to_workflow'] and actual['answer'] == text
    assert not confirmed_user_state(p.session.host_ledger)
    result = runtime.apply({'status': 'ready', 'questions': [],
        'interpretations': [interpretation(node, direct, text)]}, payload)
    assert result['status'] == 'ready' and w.status()['complete']
    assert w._load()['records'][node]['decision_id'] == direct['decision_id']
    assert any(e.get('decision_id') == old['decision_id'] and e.get('disposition') == 'deferred'
               for e in p.session.host_ledger)


@pytest.mark.parametrize('case', ['control', 'cancelled', 'other-task', 'stale', 'confirmed'])
def test_a_different_card_cannot_supply_stale_control_or_confirmed_replacement(tmp_path_factory, case):
    p = process(tmp_path_factory)
    w, old, node = pending_reply(p, 'Keep waiting')
    if case == 'confirmed':
        w.resolve_answer(node, old['decision_id'], interpretation(node, old, 'Keep waiting'))
    direct = create_user_decision(p, checkpoint())
    receive(p, direct, 'Implement now')
    if case == 'control':
        next(e for e in p.session.host_ledger if e.get('decision_id') == direct['decision_id'])['kind'] = 'runtime_checkpoint'
    elif case == 'cancelled':
        p.session.host_ledger.append({'event': 'user_decision_cancelled', 'task_id': 't', 'decision_id': direct['decision_id']})
    elif case == 'other-task':
        for e in p.session.host_ledger:
            if e.get('decision_id') == direct['decision_id']:
                e['task_id'] = 'another-task'
    elif case == 'stale':
        later = create_user_decision(p, checkpoint('later'))
        receive(p, later, 'Wait again')
    before = copy.deepcopy(p.context_snapshot), copy.deepcopy(p.session.host_ledger)
    with pytest.raises(ValueError, match='current question'):
        w.resolve_answer(node, direct['decision_id'], interpretation(node, direct, 'Implement now'))
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_round_receipt_preserves_exact_source_and_cannot_choose_another_round_item(tmp_path_factory):
    p = process(tmp_path_factory)
    w, old, node = pending_reply(p, 'Not yet')
    card = create_user_decision(p, {'kind': 'clarification', 'question': 'Next?', 'why_user_must_decide': 'Timing',
        'questions': [{'node_id': 'new-source', 'state_topic': 'next', 'question': 'Start now?',
            'why_user_must_decide': 'Timing', 'options': []}]})
    text = json.dumps({'answers': {'new-source': 'Now implement'}})
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': 't', 'decision_id': card['decision_id'],
        'answer': text, 'answers': validate_round_answer(card, text)})
    p.pending_decision = None
    receipts = business_answer_receipts(p.session.host_ledger, 't')
    assert receipts[-1]['answer'] == 'Now implement' and receipts[-1]['source_node_id'] == 'new-source'
    proposal = interpretation(node, card, 'Now implement')
    with pytest.raises(ValueError):
        w.resolve_answer(node, card['decision_id'], {**proposal, 'source_node_id': 'unknown'})
    w.resolve_answer(node, card['decision_id'], {**proposal, 'source_node_id': 'new-source'})
    assert w.status()['complete']


def test_expired_read_scope_is_not_presented_as_the_current_request(tmp_path_factory):
    from tests.test_alignment_observation_scope import setup
    p, runtime, _, proposal, _, _ = setup(tmp_path_factory)
    runtime.apply(proposal, runtime.payload(observation_only=True))
    assert runtime.payload(observation_only=True)['request'] == proposal['scope_update']['request']
    card = create_user_decision(p, checkpoint())
    receive(p, card, 'Start the original work now')
    payload = runtime.payload(observation_only=True)
    assert payload['request'] == payload['original_request']
    assert any(a['answer'] == 'Start the original work now' for a in payload['actual_answers'])


def test_identical_text_in_different_round_items_keeps_each_source_identity(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload('Use independent source identities')
    w, old, node = pending_reply(p, 'Earlier reply')
    # One actual legacy round has a source for the existing facet and another
    # independent item. Equal wording must not erase the latter identity.
    p.session.host_ledger.append({'event': 'user_decision_requested', 'task_id': 't', 'decision_id': 'round',
        'kind': 'clarification', 'questions': [
            {'node_id': node, 'state_topic': 'trip', 'question': 'First facet?'},
            {'node_id': 'other-source', 'state_topic': 'other-facet', 'question': 'Other facet?'}]})
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': 't', 'decision_id': 'round',
        'answers': {node: {'state_topic': 'trip', 'answer': 'Same exact reply'},
                    'other-source': {'state_topic': 'other-facet', 'answer': 'Same exact reply'}}})
    p.pending_decision = None
    payload = runtime.payload()
    sources = {a.get('source_node_id') for a in payload['actual_answers'] if a['decision_id'] == 'round'}
    assert sources == {node, 'other-source'}


@pytest.mark.parametrize('kind', ['checkpoint', 'verification'])
def test_business_card_kinds_use_question_review_before_any_batch_execution(tmp_path_factory, kind):
    from backend.core.loop.tool_proposal_runtime import ToolProposalRuntime
    p = process(tmp_path_factory)
    observed = []
    def review(*args, **kwargs):
        observed.append(kwargs)
        return {'alignment_repair': 'Unit fixture: do not publish this unnecessary card'}
    boundary = ToolProposalRuntime(p, p.session, lambda: {}, review).review([
        {'name': 'request_user_decision', 'id': 'card', 'args': {**checkpoint(), 'kind': kind}},
        {'name': 'write_file', 'id': 'write', 'args': {}}], '')
    assert boundary.retry and observed[0]['proposed_questions'][0]['kind'] == kind
    assert not p.pending_decision and not p.tool_receipts
