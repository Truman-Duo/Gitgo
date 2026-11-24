"""Current incremental meaning reaches the main model without a hidden judge."""
from copy import deepcopy
import json

import pytest

from backend.core.loop.dialogue_context import dialogue_view, MAX_VIEW_BYTES
from backend.core.loop.main_dialogue import activate, apply_answer_updates
from backend.core.loop.turn_guidance import _build_current_turn_envelope
from backend.core.prompt_templates import load_prompt
from tests.test_alignment_semantics import process, pending_reply
from tests.test_dynamic_alignment import question


def partial(p, *, disposition='partial', value='Offline use is established', remaining='Notification preference'):
    workflow, card, node = pending_reply(p, 'Use it offline; notifications are undecided')
    next(e for e in p.session.host_ledger if e.get('event') == 'user_decision_received')['process_id'] = p.process_id
    activate(p)
    workflow.resolve_answer(node, card['decision_id'], {'disposition': disposition,
        'value': value, 'quote': 'Use it offline', 'remaining': remaining})
    return workflow, card, node


def receive_other(p, workflow):
    card = workflow.ask_semantic(question('deadline'))
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': p.active_task_id,
        'decision_id': card['decision_id'], 'process_id': p.process_id, 'answer': 'Next week'})
    p.pending_decision = None


def test_current_question_wording_and_dependency_frontier_reuse_the_existing_graph(tmp_path_factory):
    from backend.core.loop.engineering_workflow import EngineeringWorkflow
    p = process(tmp_path_factory)
    activate(p)
    w = EngineeringWorkflow(p)
    w.ask_semantic({'questions': [
        {**question('audience'), 'question': 'Who should receive the report?', 'answer_kind': 'choice'},
        {**question('format', after=['audience']), 'question': 'Which format does that audience need?'}]})
    before = deepcopy((p.context_snapshot, p.session.host_ledger, p.pending_decision))
    view = dialogue_view(p)
    items = {i['state_topic']: i for i in view['unresolved_decisions']}
    assert items['audience']['question'] == 'Who should receive the report?'
    assert items['audience']['answer_kind'] == 'choice' and items['audience']['graph_state'] == 'ready'
    assert items['format']['graph_state'] == 'blocked'
    assert items['format']['depends_on_topics'] == ['audience']
    assert (p.context_snapshot, p.session.host_ledger, p.pending_decision) == before
    assert not view['authority_granted'] and not p.approval_grants


def test_unanswered_question_detail_is_complete_without_putting_long_wording_in_current_context(tmp_path_factory):
    from backend.core.loop.engineering_workflow import EngineeringWorkflow
    p = process(tmp_path_factory)
    activate(p)
    w = EngineeringWorkflow(p)
    full = 'Which grouping should the report use? ' + 'Detail. ' * 150
    card = w.ask_semantic({**question('grouping'), 'question': full})
    before = deepcopy((p.context_snapshot, p.session.host_ledger, p.pending_decision))
    view = dialogue_view(p)
    item = view['unresolved_decisions'][0]
    assert 'question' not in item and 'question_details' in item
    detail = w.invoke({'operation': 'status', 'node_id': item['question_details']['node_id']})
    assert detail['question_request']['question'] == full
    assert detail['question_request']['recommendation'] == question('grouping')['recommendation']
    assert p.pending_decision['decision_id'] == card['decision_id']
    assert (p.context_snapshot, p.session.host_ledger, p.pending_decision) == before
    assert len(json.dumps(view, ensure_ascii=False).encode()) <= MAX_VIEW_BYTES


def test_scoped_delegation_closes_covered_question_and_keeps_dependent_choice_unconfirmed(tmp_path_factory):
    from backend.core.loop.engineering_workflow import EngineeringWorkflow
    p = process(tmp_path_factory)
    activate(p)
    w = EngineeringWorkflow(p)
    card = w.ask_semantic({'questions': [
        {**question('format'), 'question': 'Which report file format should be used?'},
        {**question('audience'), 'question': 'Who may read the report?'}]})
    source_node = next(q['node_id'] for q in card['questions'] if q['state_topic'] == 'format')
    reply = 'Choose the file format yourself; the audience is still undecided.'
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': p.active_task_id,
        'process_id': p.process_id, 'decision_id': card['decision_id'],
        'answers': {source_node: {'answer': reply}}})
    p.pending_decision = None
    apply_answer_updates(p, [
        {'state_topic': 'format', 'disposition': 'delegated', 'quote': 'Choose the file format yourself'},
        {'state_topic': 'audience', 'disposition': 'deferred', 'quote': 'the audience is still undecided',
         'remaining': 'Choose the intended readers'}])
    view = dialogue_view(p)
    assert {i['state_topic'] for i in view['unresolved_decisions']} == {'audience'}
    assert view['current_decisions'][0]['state_topic'] == 'format'
    assert view['current_decisions'][0]['disposition'] == 'delegated'
    assert sum(e.get('event') == 'user_decision_requested' for e in p.session.host_ledger) == 1
    assert not w.guard('write_file', 'workspace_write', {'path': 'report.md'})['allowed']


def test_partial_known_value_and_remainder_are_visible_after_another_reply(tmp_path_factory):
    p = process(tmp_path_factory)
    w, card, _ = partial(p)
    receive_other(p, w)
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    view = dialogue_view(p)
    node = next(n for n in view['unresolved_decisions'] if n['state_topic'] == 'trip')
    assert node['interpretation']['disposition'] == 'partial'
    assert node['interpretation']['value'] == 'Use it offline'
    assert node['interpretation']['value_origin'] == 'user_quote'
    assert node['interpretation']['remaining'] == 'Notification preference'
    assert node['interpretation']['quote'] == 'Use it offline'
    assert node['interpretation']['decision_id'] == card['decision_id']
    assert view['current_received_inputs'][0]['answer'] == 'Next week'
    assert not view['authority_granted'] and not p.approval_grants
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_effective_readonly_scope_replaces_old_review_frame_in_the_current_view(tmp_path_factory):
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Only summarize settled and undecided items now')
    next(e for e in p.session.host_ledger if e.get('event') == 'user_decision_received')['process_id'] = p.process_id
    activate(p)
    p.update_context_snapshot(lambda c: {**c, 'alignment_runtime': {**c['alignment_runtime'],
        'request_frame': {'kind': 'deliverable', 'outcome': 'Implement the product', 'source_quote': 'old'}}})
    apply_answer_updates(p, [], {'mode': 'exploration', 'quote': 'Only summarize settled and undecided items now'})
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    view = dialogue_view(p)
    assert view['current_readonly_scope']['request'] == 'Only summarize settled and undecided items now'
    assert view['current_readonly_scope']['implementation_authorized'] is False
    assert 'reviewed_current_outcome' not in view
    envelope = _build_current_turn_envelope(p, [])
    wire = p.session.to_provider_messages(dynamic_envelope=envelope)
    marker = json.loads(wire[-1]['content'].split('\n', 1)[1])
    assert marker['dialogue_reference']['current_readonly_scope'] == view['current_readonly_scope']
    assert (p.context_snapshot, p.session.host_ledger) == before
    assert not w.guard('write_file', 'workspace_write', {'path': 'product.py'})['allowed']


@pytest.mark.parametrize('invalidate', ['cancel', 'changed-reply', 'wrong-task', 'wrong-owner', 'control-source', 'reopened'])
def test_invalidated_partial_interpretation_is_not_presented_as_current(tmp_path_factory, invalidate):
    p = process(tmp_path_factory)
    w, card, node = partial(p)
    event = next(e for e in reversed(p.session.host_ledger) if e.get('event') == 'user_decision_interpreted')
    if invalidate == 'cancel':
        p.session.host_ledger.append({'event': 'user_decision_cancelled', 'task_id': p.active_task_id, 'decision_id': card['decision_id']})
    elif invalidate == 'changed-reply':
        received = next(e for e in p.session.host_ledger if e.get('event') == 'user_decision_received')
        received['answer'] = 'Actually this must work online'
    elif invalidate == 'wrong-task':
        event['task_id'] = 'foreign'
    elif invalidate == 'wrong-owner':
        event['process_id'] = 'foreign'
    elif invalidate == 'control-source':
        next(e for e in p.session.host_ledger if e.get('event') == 'user_decision_requested')['kind'] = 'permission'
    else:
        w.request_decision(node, {'kind': 'clarification', **question('trip')})
    view = dialogue_view(p)
    assert not any(n.get('interpretation') for n in view['unresolved_decisions'])


@pytest.mark.parametrize('invalidate', ['cancel', 'newer-reply', 'execution-contract'])
def test_only_an_effective_readonly_scope_is_projected(tmp_path_factory, invalidate):
    p = process(tmp_path_factory)
    w, card, _ = pending_reply(p, 'Compare only')
    activate(p)
    apply_answer_updates(p, [], {'mode': 'exploration', 'quote': 'Compare only'})
    if invalidate == 'cancel':
        p.session.host_ledger.append({'event': 'user_decision_cancelled', 'task_id': p.active_task_id, 'decision_id': card['decision_id']})
    elif invalidate == 'newer-reply':
        receive_other(p, w)
    else:
        p.update_context_snapshot(lambda c: {**c, 'task_contract': {'execution_mode': 'self_execute'}})
    assert 'current_readonly_scope' not in dialogue_view(p)


def test_large_interpretation_retains_topic_and_a_scoped_full_detail_reader(tmp_path_factory):
    p = process(tmp_path_factory)
    w, _, node = partial(p, value='x' * 9000, remaining='y' * 4000)
    view = dialogue_view(p)
    item = next(n for n in view['unresolved_decisions'] if n['state_topic'] == 'trip')
    assert item['interpretation']['value'] == 'Use it offline'
    assert 'agent_interpretation' not in item['interpretation']
    assert item['interpretation_details']['tool'] == 'engineering_workflow'
    assert item['interpretation_details']['node_id'] == node
    detail = w.invoke({'operation': 'status', 'node_id': node})['interpretation']
    assert detail['value'] == 'Use it offline' and detail['remaining'] == 'y' * 4000
    assert detail['agent_interpretation'] == 'x' * 9000 and detail['interpretation_status'] == 'unverified_proposal'
    assert len(json.dumps(view, ensure_ascii=False, separators=(',', ':')).encode()) <= MAX_VIEW_BYTES
    assert view['omitted_reference_items'] == 0


def test_current_governance_is_in_the_actual_provider_projection_without_changing_state(tmp_path_factory):
    p = process(tmp_path_factory)
    partial(p)
    before = deepcopy((p.context_snapshot, p.session.host_ledger, p.session.messages))
    envelope = _build_current_turn_envelope(p, [])
    wire = p.session.to_provider_messages(dynamic_envelope=envelope)
    marker = json.loads(wire[-1]['content'].split('\n', 1)[1])
    assert marker['dialogue_reference']['unresolved_decisions'][0]['interpretation']['remaining'] == 'Notification preference'
    assert (p.context_snapshot, p.session.host_ledger, p.session.messages) == before


def test_tool_guidance_assigns_incremental_semantics_to_the_main_agent():
    mechanism = load_prompt('tools/harness_mechanism')
    task = load_prompt('tools/task_mechanism')
    assert 'obtains the normal semantic boundary review' not in mechanism
    assert 'performs the normal semantic review' not in mechanism
    assert 'Grill assesses whether' not in task
    assert 'remains visible to Grill' not in task
    assert 'You judge' in mechanism
    assert 'You judge' in task
    assert 'related_review' in task and 'acceptance' in task and 'source_ref' in task


def test_deferred_meaning_keeps_waiting_and_known_constraints_visible(tmp_path_factory):
    p = process(tmp_path_factory)
    w, _, _ = partial(p, disposition='deferred', value='', remaining='Wait for the user to decide notification behavior')
    view = dialogue_view(p)
    record = view['unresolved_decisions'][0]['interpretation']
    assert record['disposition'] == 'deferred'
    assert record['remaining'] == 'Wait for the user to decide notification behavior'
    assert not w.status()['complete']


def test_predecessor_partial_meaning_is_reference_not_a_new_task_obligation(tmp_path_factory):
    from tests.test_dialogue_context import followup
    p = process(tmp_path_factory)
    partial(p)
    next_process = followup(p, tmp_path_factory)
    view = dialogue_view(next_process)
    prior = next(r for r in view['prior_business_reference'] if r.get('record_kind') == 'prior_unresolved_interpretation')
    assert prior['disposition'] == 'partial' and prior['remaining'] == 'Notification preference'
    assert prior['task_id'] == p.active_task_id != next_process.active_task_id
    assert not (next_process.context_snapshot or {}).get('engineering_workflow')
    assert not view['authority_granted']


def test_interpreted_value_keeps_its_exact_source_separate_in_main_input(tmp_path_factory):
    from backend.core.loop.decision_support import confirmed_user_state
    from backend.core.loop.prompt_compiler import PromptCompiler
    p = process(tmp_path_factory)
    w, card, node = pending_reply(p, 'Stop accepting entries at noon')
    next(e for e in p.session.host_ledger if e.get('event') == 'user_decision_received')['process_id'] = p.process_id
    w.resolve_answer(node, card['decision_id'], {'disposition': 'confirmed',
        'value': 'Noon in an assistant-chosen timezone', 'quote': 'at noon'})
    activate(p)  # a retained legacy interpretation, not a new main-owned write
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    state = confirmed_user_state(p.session.host_ledger)[0]
    assert state['value_origin'] == 'agent_interpretation'
    assert state['source_quote'] == 'at noon'
    assert state['value'] == 'Noon in an assistant-chosen timezone'
    _, sections = PromptCompiler.compile(process=p, tools={}, workspace_path=str(tmp_path_factory))
    assert not any(s.name == 'Current user decisions and delegation' for s in sections)
    wire = p.session.to_provider_messages(dynamic_envelope=_build_current_turn_envelope(p, []))
    marker = json.loads(wire[-1]['content'].split('\n', 1)[1])
    record = marker['dialogue_reference']['current_decisions'][0]
    assert record['source_quote'] == 'at noon' and record['value_origin'] == 'agent_interpretation'
    assert 'interpreted_value' not in record and record['interpretation_status'] == 'unverified_proposal'
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_actual_listed_selection_is_distinguished_from_agent_interpretation():
    from backend.core.loop.decision_support import confirmed_user_state
    ledger = [
        {'event': 'user_decision_requested', 'task_id': 't', 'decision_id': 'd',
         'kind': 'choice', 'state_topic': 'audience', 'options': [{'label': 'Teams'}]},
        {'event': 'user_decision_received', 'task_id': 't', 'decision_id': 'd',
         'state_topic': 'audience', 'answer': 'Choose option 1', 'selected_label': 'Teams'}]
    record = confirmed_user_state(ledger)[0]
    assert record['value_origin'] == 'user_selection'
    assert record['selected_label'] == 'Teams'
    assert 'source_quote' not in record  # the label is a real selection, not a fabricated quote


def test_scope_registered_during_needed_workflow_read_reaches_the_next_request(tmp_path_factory):
    p = process(tmp_path_factory)
    request = 'Leave the implementation choices open; compare only the current options'
    w, _, _ = pending_reply(p, request)
    next(e for e in p.session.host_ledger if e.get('event') == 'user_decision_received')['process_id'] = p.process_id
    activate(p)
    result = w.invoke({'operation': 'status', 'scope_update': {'quote': request}})
    assert result['task_id'] == p.active_task_id and not result.get('error')
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    wire = p.session.to_provider_messages(dynamic_envelope=_build_current_turn_envelope(p, []))
    marker = json.loads(wire[-1]['content'].split('\n', 1)[1])
    assert marker['dialogue_reference']['current_readonly_scope']['request'] == request
    assert marker['dialogue_reference']['current_readonly_scope']['implementation_authorized'] is False
    assert not w.status()['complete']
    assert (p.context_snapshot, p.session.host_ledger) == before
    assert not w.guard('write_file', 'workspace_write', {'path': 'app.py'})['allowed']


def test_current_settled_evidence_stays_in_the_next_reference_after_other_replies(tmp_path_factory):
    from backend.core.loop.main_dialogue import apply_answer_updates
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Release the resource; no other rule was agreed')
    activate(p)
    apply_answer_updates(p, [{'state_topic': 'trip', 'disposition': 'confirmed',
        'quote': 'Release the resource', 'value': 'Release the resource'}])
    receive_other(p, w)
    view = dialogue_view(p)
    rule = view['current_decisions'][0]
    assert rule['source_quote'] == 'Release the resource'
    assert 'interpreted_value' not in rule
    assert rule['disposition'] == 'confirmed' and rule['value_origin'] == 'user_quote'
    assert view['current_received_inputs'][0]['answer'] == 'Next week'
    assert not view['authority_granted']


def test_large_current_decision_has_a_complete_scoped_reader(tmp_path_factory):
    from backend.core.loop.main_dialogue import apply_answer_updates
    p = process(tmp_path_factory)
    w, _, node = pending_reply(p, 'A short literal user rule')
    activate(p)
    apply_answer_updates(p, [{'state_topic': 'trip', 'disposition': 'confirmed',
        'quote': 'literal user rule', 'value': 'x' * 13000}])
    view = dialogue_view(p)
    item = view['current_decisions'][0]
    assert item['interpretation_details']['node_id'] == node
    assert 'interpreted_value' not in item
    assert len(json.dumps(view, ensure_ascii=False, separators=(',', ':')).encode()) <= MAX_VIEW_BYTES
    full = w.invoke(item['interpretation_details'])['current_decision']
    assert full['value'] == 'literal user rule' and full['source_quote'] == 'literal user rule'
    assert full['agent_interpretation'] == 'x' * 13000 and full['interpretation_status'] == 'unverified_proposal'
