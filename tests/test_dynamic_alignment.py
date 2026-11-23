"""Dependency-ready groups and immediate actual replies, not model taste proofs."""
import copy
import json
import pytest

from tests.test_alignment_rounds import setup, receive
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.decision_support import validate_round_answer, prepare_user_decision
from backend.core.loop.question_surface import question_proposals, question_schema
from backend.core.loop.tool_context import contract_overview
from backend.core.prompt_templates import load_prompt


def question(topic, *, after=()):
    return {'state_topic': topic, 'question': topic + '?',
        'why_user_must_decide': 'Changes the requested business behavior',
        'recommendation': 'Prefer the simpler reversible choice; retain your constraints.',
        **({'depends_on_topics': list(after)} if after else {})}


def test_compact_current_surface_is_fully_available_without_detail_refinement():
    definition = {'name': 'request_user_decision', 'description': load_prompt('tools/request_user_decision'),
        'parameters': question_schema(), 'runtime_behavior': {'effect': 'read', 'cancellation': 'cooperative'}}
    assert len(json.dumps(definition, ensure_ascii=False, separators=(',', ':'))) <= 2048
    assert contract_overview(definition) == definition
    assert 'task_id' not in question_schema()['properties']
    assert 'process_id' not in question_schema()['properties']


@pytest.mark.parametrize('count', [4, 5])
def test_option_count_error_names_the_field_before_consuming_any_answer(tmp_path_factory, count):
    from tests.test_alignment_semantics import process, pending_reply
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Japan')
    before = copy.deepcopy((p.context_snapshot, p.session.host_ledger))
    result = w.invoke({'operation': 'ask_semantic', 'request': {'questions': [
        {**question('policy'), 'options': [{'label': str(i)} for i in range(count)]}]},
        'answer_updates': [{'state_topic': 'trip', 'disposition': 'confirmed', 'quote': 'Japan'}]})
    assert result['error_info']['catalog_id'] == 'GITGO-E6221'
    assert result['error_info']['details']['field_path'] == '/questions/0/options'
    assert result['error_info']['details']['allowed_counts'] == [0, 1, 2, 3]
    assert not any(a.get('operation') == 'status' for a in result['error_info']['next_actions'])
    assert (p.context_snapshot, p.session.host_ledger) == before


@pytest.mark.parametrize('invalid', ['option', 'fact', {}, False])
def test_invalid_question_kind_has_precise_field_error_before_any_state_change(tmp_path_factory, invalid):
    p, w, _ = setup(tmp_path_factory)
    before = copy.deepcopy((p.context_snapshot, p.session.host_ledger))
    result = w.invoke({'operation': 'ask_semantic', 'request': {'questions': [
        {**question('policy'), 'answer_kind': invalid}]}, 'answer_updates': []})
    assert result['error'] == 'QUESTION_ANSWER_KIND_INVALID'
    assert result['error_info']['catalog_id'] == 'GITGO-E6220'
    assert result['error_info']['details']['field_path'] == '/questions/0/answer_kind'
    assert result['error_info']['details']['allowed'] == ['choice', 'evidence']
    assert (p.context_snapshot, p.session.host_ledger) == before and p.pending_decision is None


def test_direct_group_builds_one_graph_and_publishes_only_independent_ready_questions(tmp_path_factory):
    p, _, _ = setup(tmp_path_factory)
    before = copy.deepcopy(p.session.host_ledger)
    # A fresh task has no workflow/configure prerequisite for the question API.
    p.context_snapshot = {}
    w = EngineeringWorkflow(p)
    request = w.ask_semantic({'questions': [question('audience'), question('approval'), question('approver', after=['approval'])]})
    assert request['answer_mode'] == 'incremental'
    assert [q['state_topic'] for q in request['questions']] == ['audience', 'approval']
    assert all(q['recommendation'] for q in request['questions'])
    assert request['task_id'] == 'task' and request['process_id'] == 'alignment'
    assert len(w.status()['nodes']) == 3
    approval = next(q for q in request['questions'] if q['state_topic'] == 'approval')
    received = validate_round_answer(request, json.dumps({'answers': {approval['node_id']: 'approval required'}}))
    assert list(received) == [approval['node_id']]
    receive(p, request, {approval['node_id']: 'approval required'})
    following = w.ask_frontier()
    assert following['decision_id'] != request['decision_id']
    assert [q['state_topic'] for q in following['questions']] == ['audience', 'approver']
    assert len([e for e in p.session.host_ledger if e.get('event') == 'user_decision_received']) == 1


@pytest.mark.parametrize('mutation', ['cycle', 'unknown', 'duplicate', 'authority'])
def test_bad_question_graph_cannot_publish_or_leave_new_obligations(tmp_path_factory, mutation):
    p, w, _ = setup(tmp_path_factory)
    items = [question('new_a'), question('new_b', after=['new_a'])]
    if mutation == 'cycle':
        items[0]['depends_on_topics'] = ['new_b']
    elif mutation == 'unknown':
        items[0]['depends_on_topics'] = ['missing']
    elif mutation == 'duplicate':
        items[1]['state_topic'] = 'new_a'
    else:
        items[0]['kind'] = 'permission'
    before = copy.deepcopy(p.context_snapshot)
    with pytest.raises(ValueError):
        w.ask_semantic({'questions': items})
    assert p.context_snapshot == before
    assert p.pending_decision is None


def test_new_model_question_needs_advice_but_legacy_evidence_remains_readable(tmp_path_factory):
    p, _, _ = setup(tmp_path_factory)
    before = copy.deepcopy(p.session.host_ledger)
    old = {'question': 'Which policy?', 'why_user_must_decide': 'Business ownership', 'kind': 'clarification'}
    assert prepare_user_decision(p, old)['options'] == []
    with pytest.raises(ValueError, match='recommendation'):
        question_proposals(p, [old], require_recommendation=True)
    old['options'] = [{'label': 'Keep both', 'recommended': True}, {'label': 'Merge'}]
    normalized = question_proposals(p, [old], require_recommendation=True)[0]
    assert normalized['recommendation'] == 'Keep both'
    assert p.session.host_ledger == before


def test_factual_options_cannot_mark_an_unknown_observation_as_recommended(tmp_path_factory):
    from backend.core.loop.alignment_question import question_request
    p, _, _ = setup(tmp_path_factory)
    factual = {**question('observation'), 'answer_kind':'evidence',
        'recommendation':'Check the actual record before concluding.',
        'options':[{'label':'Found','recommended':True},{'label':'Not found'}]}
    normalized = question_proposals(p,[factual],require_recommendation=True)[0]
    assert not any(o.get('recommended') for o in normalized['options'])
    request = question_request(factual, {'stage':'delivery'})
    assert not any(o.get('recommended') for o in request['options'])
    unspecified = {**factual};unspecified.pop('answer_kind')
    assert not any(o.get('recommended') for o in question_request(unspecified,{'stage':'delivery'})['options'])
    choice = {**factual,'answer_kind':'choice','recommendation':'Prefer the reversible option.'}
    assert question_proposals(p,[choice],require_recommendation=True)[0]['options'][0]['recommended']
    assert factual['options'][0]['recommended']  # normalization never changes original evidence


def test_incremental_subset_rejects_unknown_identity_and_empty_reply_atomically(tmp_path_factory):
    p, w, _ = setup(tmp_path_factory)
    request = w.ask_frontier()
    for values in ({'not-published': 'x'}, {}, {request['questions'][0]['node_id']: ''}):
        before = copy.deepcopy(p.context_snapshot)
        with pytest.raises(ValueError):
            validate_round_answer(request, json.dumps({'answers': values}))
        assert p.context_snapshot == before and p.pending_decision['decision_id'] == request['decision_id']
    legacy = {**request}; legacy.pop('answer_mode')
    with pytest.raises(ValueError, match='every question'):
        validate_round_answer(legacy, json.dumps({'answers': {request['questions'][0]['node_id']: 'one'}}))


def test_real_work_can_reveal_a_new_question_without_redeclaring_the_task(tmp_path_factory):
    p, w, _ = setup(tmp_path_factory)
    p.steps_used = 5
    p.tool_receipts.append({'task_id': 'task', 'receipt_id': 'inspection', 'tool_name': 'read_file',
        'effect': 'read', 'succeeded': True, 'committed': True})
    request = w.ask_semantic(question('new-discovered-choice'))
    assert request['status'] == 'awaiting_user' and request['recommendation']
    assert request['task_id'] == 'task' and p.steps_used == 5
    assert len(w.status()['nodes']) == 4


@pytest.mark.parametrize('include_source_id', [True, False])
def test_one_partial_round_reply_can_resolve_a_published_sibling_with_actual_quoted_evidence(tmp_path_factory, include_source_id):
    from backend.core.loop.alignment_runtime import AlignmentRuntime
    p, _, _ = setup(tmp_path_factory)
    p.context_snapshot = {}
    w = EngineeringWorkflow(p)
    card = w.ask_semantic({'questions': [question('access'), question('policy')]})
    by_topic = {q['state_topic']: q['node_id'] for q in card['questions']}
    reply = 'Use staff access; you decide the technical integration.'
    raw = json.dumps({'answers': {by_topic['policy']: reply}})
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': 'task', 'process_id': p.process_id,
        'decision_id': card['decision_id'], 'kind': 'clarification', 'answer': raw,
        'answers': validate_round_answer(card, raw)})
    p.pending_decision = None
    interpretations = [{'node_id': by_topic['policy'], 'decision_id': card['decision_id'],
        'disposition': 'confirmed', 'value': 'Staff access', 'quote': 'Use staff access',
        'source_node_id': by_topic['policy']},
        {'node_id': by_topic['access'], 'decision_id': card['decision_id'],
         'disposition': 'delegated', 'value': 'Choose technical integration within staff access',
         'quote': 'you decide the technical integration.',
         **({'source_node_id': by_topic['policy']} if include_source_id else {})}]
    runtime = AlignmentRuntime(p)
    runtime.apply({'status': 'ready', 'interpretations': interpretations, 'questions': [], 'assumptions': []}, runtime.payload())
    assert w.status()['complete']
    delegated = [event for event in p.session.host_ledger if event.get('event') == 'user_decision_interpreted'
        and event.get('disposition') == 'delegated']
    assert len(delegated) == 1 and delegated[0]['source_node_id'] == by_topic['policy']
    assert delegated[0]['answer_digest']


@pytest.mark.parametrize('invalid_source', ['cancelled', 'other-task', 'other-process', 'unknown-node', 'wrong-quote', 'control'])
def test_sibling_adoption_cannot_borrow_cancelled_foreign_or_fabricated_evidence(tmp_path_factory, invalid_source):
    p, _, _ = setup(tmp_path_factory)
    p.context_snapshot = {}
    w = EngineeringWorkflow(p)
    card = w.ask_semantic({'questions': [question('access'), question('policy')]})
    ids = {q['state_topic']: q['node_id'] for q in card['questions']}
    raw = json.dumps({'answers': {ids['policy']: 'You decide the technical integration.'}})
    receipt = {'event': 'user_decision_received', 'task_id': 'task', 'process_id': p.process_id,
        'decision_id': card['decision_id'], 'kind': 'clarification', 'answer': raw,
        'answers': validate_round_answer(card, raw)}
    proposal = {'disposition': 'delegated', 'value': 'Choose technical integration',
        'source_node_id': ids['policy'], 'quote': 'You decide the technical integration.'}
    if invalid_source == 'other-task':
        receipt['task_id'] = 'another-task'
    elif invalid_source == 'other-process':
        receipt['process_id'] = 'another-process'
    elif invalid_source == 'unknown-node':
        proposal['source_node_id'] = 'invented'
    elif invalid_source == 'wrong-quote':
        proposal['quote'] = 'Do anything you want.'
    elif invalid_source == 'control':
        receipt['kind'] = 'permission'
    p.session.host_ledger.append(receipt)
    if invalid_source == 'cancelled':
        p.session.host_ledger.append({'event': 'user_decision_cancelled', 'task_id': 'task', 'decision_id': card['decision_id']})
    p.pending_decision = None
    before = copy.deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(ValueError):
        w.resolve_answer(ids['access'], card['decision_id'], proposal)
    assert (p.context_snapshot, p.session.host_ledger) == before
