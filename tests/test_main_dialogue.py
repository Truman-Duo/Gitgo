"""Main Agent owns meaning; Runtime binds real replies without another LLM."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.core.loop.completion_protocol import HostCompletionEvaluator
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.semantic_review_coordinator import SemanticReviewCoordinator
from tests.test_alignment_semantics import process, pending_reply
from tests.test_dynamic_alignment import question


def coordinator(p):
    def forbidden(*args, **kwargs):
        raise AssertionError('ordinary dialogue must not request a hidden semantic judge')
    return SemanticReviewCoordinator(p, SimpleNamespace(assess_alignment=forbidden),
        p.task_description, SimpleNamespace(route=None), lambda: None,
        SimpleNamespace(notice=lambda *a, **kw: None), lambda *a, **kw: None, None)


def test_ordinary_question_has_no_second_judge(tmp_path_factory):
    p = process(tmp_path_factory)
    assert coordinator(p).check(proposed_questions=[question('audience'), question('scope')]) is None


@pytest.mark.parametrize('trigger', ['admission', 'contract', 'execution', 'completion_claim'])
def test_normal_boundaries_do_not_spawn_an_alignment_request(tmp_path_factory, trigger):
    p = process(tmp_path_factory)
    pending_reply(p, 'Known fact')
    assert coordinator(p).check('Current supported answer?', trigger=trigger) is None
    assert not EngineeringWorkflow(p).status()['complete']
    assert not HostCompletionEvaluator.evaluate(p, 'Unresolved requirements remain').allowed


def update(p, items):
    from backend.core.loop.main_dialogue import apply_answer_updates
    return apply_answer_updates(p, items)


def test_main_can_bind_reply_by_topic_without_copying_ids(tmp_path_factory):
    p = process(tmp_path_factory)
    w, card, node = pending_reply(p, 'Japan with family')
    coordinator(p).check()
    update(p, [{'state_topic': 'trip', 'disposition': 'confirmed',
                'value': 'Japan with family', 'quote': 'Japan with family'}])
    assert w.status()['complete']
    event = next(e for e in reversed(p.session.host_ledger) if e.get('event') == 'user_decision_interpreted')
    assert event['decision_id'] == card['decision_id'] and event['node_id'] == node
    assert event['interpreter'] == 'main_agent'
    from backend.core.loop.main_dialogue import apply_answer_updates
    apply_answer_updates(p, [], {'quote': 'Japan with family', 'discussion': 'A supported itinerary'})
    assert HostCompletionEvaluator.evaluate(p, 'A supported itinerary').allowed


def test_agent_expansion_is_not_persisted_as_the_confirmed_rule(tmp_path_factory):
    from backend.core.loop.decision_support import confirmed_user_state
    p = process(tmp_path_factory)
    w, _, node = pending_reply(p, 'Release the held resource after twenty minutes.')
    update(p, [{'state_topic': 'trip', 'disposition': 'confirmed',
        'quote': 'Release the held resource after twenty minutes.',
        'value': 'Release the resource, void the request and require resubmission.'}])
    state = confirmed_user_state(p.session.host_ledger, task_id=p.active_task_id)[0]
    assert state['value'] == 'Release the held resource after twenty minutes.'
    assert w._load()['records'][node]['value'] == state['value']
    assert state['value_origin'] == 'user_quote'
    assert state['agent_interpretation'] == 'Release the resource, void the request and require resubmission.'
    assert state['interpretation_status'] == 'unverified_proposal'
    assert not p.approval_grants


def test_completion_receipt_does_not_approve_the_agents_paraphrase(tmp_path_factory):
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.loop.model_feedback import completion_feedback
    p = process(tmp_path_factory)
    pending_reply(p, 'Release the resource.')
    args = {'scope_update': {'quote': 'Release the resource.'}, 'answer_updates': [
        {'state_topic': 'trip', 'disposition': 'confirmed', 'quote': 'Release the resource.',
         'value': 'Release it and void the request.'}]}
    result = build_internal_tools(p, {}, str(tmp_path_factory), None, None, None)['complete_task'].execute(args)
    view = completion_feedback(args, result)
    assert view['accepted'] and view['decision_recording']['agent_paraphrases'] == 'unverified_proposals_not_user_acceptance'
    assert view['decision_recording']['accepted_values'] == 'actual_source_words_or_listed_selections'


def test_same_receipt_misinterpretation_can_be_corrected_without_new_user_input(tmp_path_factory):
    from backend.core.loop.decision_support import confirmed_user_state
    from backend.core.workbench.expression import inspect_delivery, assess_excerpt
    p = process(tmp_path_factory)
    w, card, node = pending_reply(p, 'Release the held resource. Only summarize.')
    w.resolve_answer(node, card['decision_id'], {'disposition': 'confirmed',
        'quote': 'Release the held resource.', 'value': 'Release it and void the request.'})
    old = deepcopy(p.session.host_ledger[-1])
    coordinator(p).check()
    inspect_delivery(p, '## Agreed\n\nDraft')
    binding = deepcopy(p.context_snapshot['workbench_delivery']['dialogue_excerpt'])
    requests = sum(e.get('event') == 'user_decision_requested' for e in p.session.host_ledger)
    update(p, [{'state_topic': 'trip', 'disposition': 'confirmed', 'correct': True,
                'quote': 'Release the held resource.'}])
    current = confirmed_user_state(p.session.host_ledger, task_id=p.active_task_id)[0]
    assert current['value'] == 'Release the held resource.'
    assert w._load()['records'][node]['value'] == current['value']
    assert old in p.session.host_ledger and p.session.host_ledger[-1]['corrects_interpretation_digest']
    assert sum(e.get('event') == 'user_decision_requested' for e in p.session.host_ledger) == requests
    assert assess_excerpt(p, binding['excerpt'], binding)['status'] == 'stale'
    assert not p.pending_decision and not p.approval_grants


def test_workflow_alias_cannot_bypass_literal_source_recording(tmp_path_factory):
    p = process(tmp_path_factory)
    w, card, node = pending_reply(p, 'Release the resource.')
    coordinator(p).check()
    result = w.invoke({'operation': 'resolve_answer', 'node_id': node,
        'decision_id': card['decision_id'], 'content': {'disposition': 'confirmed',
            'quote': 'Release the resource.', 'value': 'Release it and void the application.'}})
    assert result['value'] == 'Release the resource.'
    assert result['agent_interpretation'] == 'Release it and void the application.'
    assert result['interpretation_status'] == 'unverified_proposal'
    assert w._load()['records'][node]['value'] == 'Release the resource.'


def test_correction_binds_existing_receipt_even_after_another_user_reply(tmp_path_factory):
    p = process(tmp_path_factory)
    w, card, node = pending_reply(p, 'Release the resource.')
    w.resolve_answer(node, card['decision_id'], {'disposition': 'confirmed',
        'quote': 'Release the resource.', 'value': 'Release and void the application.'})
    coordinator(p).check()
    later = w.ask_semantic(question('schedule'))
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': p.active_task_id,
        'decision_id': later['decision_id'], 'answer': 'Next week'})
    p.pending_decision = None
    update(p, [{'state_topic': 'trip', 'disposition': 'confirmed', 'correct': True,
                'quote': 'Release the resource.'}])
    assert w._load()['records'][node]['value'] == 'Release the resource.'
    corrected = p.session.host_ledger[-1]
    assert corrected['decision_id'] == card['decision_id']
    assert corrected['corrects_interpretation_digest']
    assert 'schedule' in [n['state_topic'] for n in w.status()['nodes'] if n['status'] != 'complete']


def test_correcting_a_source_interpretation_invalidates_its_completed_report(tmp_path_factory):
    from backend.core.loop.engineering_workflow import declared_plan
    p = process(tmp_path_factory)
    w, card, node = pending_reply(p, 'Release the resource.')
    w.resolve_answer(node, card['decision_id'], {'disposition': 'confirmed',
        'quote': 'Release the resource.', 'value': 'Release and void the application.'})
    coordinator(p).check()
    plan = declared_plan(w._load()['plan'])
    plan['nodes'].append({'id': 'summary', 'kind': 'report', 'format': 'alignment', 'depends_on': [node]})
    w.configure(plan)
    w.record('summary', {'decisions': ['Old interpretation'], 'unresolved': []})
    assert 'summary' in w._load()['records']
    update(p, [{'state_topic': 'trip', 'disposition': 'confirmed', 'correct': True,
                'quote': 'Release the resource.'}])
    assert 'summary' not in w._load()['records'] and not w.status()['complete']


def test_legacy_interpretation_change_requires_explicit_correction_intent(tmp_path_factory):
    p = process(tmp_path_factory)
    w, card, node = pending_reply(p, 'Actual user rule')
    w.resolve_answer(node, card['decision_id'], {'disposition': 'confirmed',
        'quote': 'Actual user rule', 'value': 'Extra rule inferred by the Agent'})
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(ValueError) as error:
        update(p, [{'state_topic': 'trip', 'disposition': 'confirmed', 'quote': 'Actual user rule'}])
    assert error.value.code == 'ALIGNMENT_REPLY_CORRECTION_REQUIRED'
    assert (p.context_snapshot, p.session.host_ledger) == before


@pytest.mark.parametrize('flags', [{'correct': True}, {'correct': 'yes'}, {'correct': True, 'revise': True}])
def test_correction_cannot_invent_an_existing_interpretation_or_user_revision(tmp_path_factory, flags):
    p = process(tmp_path_factory)
    pending_reply(p, 'Actual rule')
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(ValueError):
        update(p, [{'state_topic': 'trip', 'disposition': 'confirmed', 'quote': 'Actual rule', **flags}])
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_deferral_binds_the_unique_actual_receipt_without_turning_it_into_confirmation(tmp_path_factory):
    p = process(tmp_path_factory)
    w, card, node = pending_reply(p, 'Only summarize; I have not chosen the itinerary.')
    item = {'state_topic': 'trip', 'disposition': 'deferred', 'remaining': 'Itinerary not chosen'}
    original = deepcopy(item)
    update(p, [item])
    event = next(e for e in reversed(p.session.host_ledger) if e.get('event') == 'user_decision_interpreted')
    assert event['decision_id'] == card['decision_id'] and event['quote'] == 'Only summarize; I have not chosen the itinerary.'
    assert event['disposition'] == 'deferred' and item == original
    assert node not in w._load()['records'] and not w.status()['complete'] and not p.approval_grants


def test_deferral_without_quote_cannot_guess_between_answered_facets(tmp_path_factory):
    import json
    from backend.core.loop.decision_support import validate_round_answer
    from backend.core.loop.main_dialogue import DialogueUpdateError
    p = process(tmp_path_factory)
    w = EngineeringWorkflow(p)
    card = w.ask_semantic({'questions': [question('first'), question('second')]})
    instruction = json.dumps({'answers': {q['node_id']: 'Actual input ' + str(i) for i, q in enumerate(card['questions'])}})
    answers = validate_round_answer(card, instruction)
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': p.active_task_id,
        'decision_id': card['decision_id'], 'answer': instruction, 'answers': answers})
    p.pending_decision = None
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(DialogueUpdateError) as error:
        update(p, [{'state_topic': 'first', 'disposition': 'deferred', 'remaining': 'Still undecided'}])
    assert error.value.code == 'ALIGNMENT_REPLY_SOURCE_AMBIGUOUS'
    assert (p.context_snapshot, p.session.host_ledger) == before


@pytest.mark.parametrize('disposition', ['confirmed', 'partial'])
def test_fact_interpretation_still_requires_an_exact_agent_selected_quote(tmp_path_factory, disposition):
    p = process(tmp_path_factory)
    pending_reply(p, 'An actual fact, with other choices open.')
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(ValueError):
        update(p, [{'state_topic': 'trip', 'disposition': disposition,
                    'value': 'An actual fact', 'remaining': 'Other choices'}])
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_volunteered_partial_fact_keeps_its_known_part_and_remaining_scope(tmp_path_factory):
    from backend.core.loop.dialogue_context import unresolved_interpretations
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Weekdays 8 to 17; the affected activities are not decided.')
    requests = sum(e.get('event') == 'user_decision_requested' for e in p.session.host_ledger)
    update(p, [{'state_topic': 'activity_hours', 'disposition': 'partial',
                'quote': 'Weekdays 8 to 17', 'remaining': 'Which activities use these hours'}])
    node = next(n for n in w._load()['plan']['nodes'] if n.get('state_topic') == 'activity_hours')
    assert node['id'] not in w._load().get('records', {})
    known = unresolved_interpretations(p)[node['id']]
    assert known['value'] == 'Weekdays 8 to 17'
    assert known['remaining'] == 'Which activities use these hours'
    assert sum(e.get('event') == 'user_decision_requested' for e in p.session.host_ledger) == requests
    assert not p.pending_decision and not p.approval_grants
    from backend.core.loop.main_dialogue import apply_answer_updates
    apply_answer_updates(p, [], {'quote': 'Weekdays 8 to 17; the affected activities are not decided.'})
    assert not w.completion_reasons()
    assert HostCompletionEvaluator.evaluate(p, 'Only summarize the current known hours and their unresolved scope.').allowed
    assert node['id'] not in w._load().get('records', {})
    assert not w.guard('write_file', 'workspace_write', {'path': 'implementation.py'})['allowed']
    p.task_kind = 'action'
    assert w.completion_reasons()


def test_volunteered_partial_cannot_reclassify_an_explicit_workflow_obligation(tmp_path_factory):
    from backend.core.loop.main_dialogue import apply_answer_updates
    from backend.core.loop.engineering_workflow import declared_plan
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Weekdays 8 to 17; only summarize.')
    plan = declared_plan(w._load()['plan'])
    plan['nodes'].append({'id': 'required_hours', 'kind': 'decision',
                         'state_topic': 'activity_hours', 'depends_on': []})
    w.configure(plan)
    apply_answer_updates(p, [{'state_topic': 'activity_hours', 'disposition': 'partial',
                             'quote': 'Weekdays 8 to 17', 'remaining': 'Which activities use these hours'}],
                         {'quote': 'only summarize.'})
    assert 'required_hours' not in w._load().get('elicitation_tiers', {})
    assert any('required_hours' in reason for reason in w.completion_reasons())
    assert not HostCompletionEvaluator.evaluate(p, 'Explicit obligation remains unresolved').allowed


@pytest.mark.parametrize('quote,remaining', [(None, 'Unknown scope'), ('Invented hours', 'Unknown scope'),
                                           ('Weekdays 8 to 17', '')])
def test_new_partial_fact_still_requires_actual_source_and_remaining_scope(tmp_path_factory, quote, remaining):
    p = process(tmp_path_factory)
    pending_reply(p, 'Weekdays 8 to 17')
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(ValueError):
        update(p, [{'state_topic': 'activity_hours', 'disposition': 'partial',
                    'quote': quote, 'remaining': remaining}])
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_agent_can_preserve_independent_old_fact_before_narrower_revision(tmp_path_factory):
    from backend.core.loop.decision_support import confirmed_user_state
    p = process(tmp_path_factory)
    w, old, _ = pending_reply(p, 'General deliveries are instant. Restricted deliveries need supervisor review.')
    update(p, [{'state_topic': 'trip', 'disposition': 'confirmed',
                'quote': 'General deliveries are instant. Restricted deliveries need supervisor review.'}])
    card = w.ask_semantic(question('schedule'))
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': p.active_task_id,
        'decision_id': card['decision_id'], 'answer': 'General deliveries now wait ten minutes.'})
    p.pending_decision = None
    update(p, [{'state_topic': 'restricted_delivery_review', 'disposition': 'confirmed',
                'decision_id': old['decision_id'], 'quote': 'Restricted deliveries need supervisor review.'},
               {'state_topic': 'trip', 'disposition': 'confirmed', 'revise': True,
                'quote': 'General deliveries now wait ten minutes.'}])
    values = {v['topic']: v for v in confirmed_user_state(p.session.host_ledger, task_id=p.active_task_id)}
    assert values['trip']['value'] == 'General deliveries now wait ten minutes.'
    assert values['restricted_delivery_review']['value'] == 'Restricted deliveries need supervisor review.'
    assert values['restricted_delivery_review']['decision_id'] == old['decision_id']
    assert not p.approval_grants


def test_forged_quote_and_batch_failure_leave_no_partial_commit(tmp_path_factory):
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Japan with family')
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(ValueError):
        update(p, [{'state_topic': 'trip', 'disposition': 'confirmed',
                    'value': 'Japan', 'quote': 'Japan'},
                   {'state_topic': 'invented', 'disposition': 'confirmed',
                    'value': 'Approved', 'quote': 'Approved'}])
    assert (p.context_snapshot, p.session.host_ledger) == before
    assert not w.status()['complete']


@pytest.mark.parametrize('disposition', ['partial', 'deferred'])
def test_unresolved_reply_does_not_become_confirmation(tmp_path_factory, disposition):
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Not decided')
    coordinator(p).check()
    update(p, [{'state_topic': 'trip', 'disposition': disposition,
                'quote': 'Not decided', 'remaining': 'Destination'}])
    assert not w.status()['complete']
    assert not HostCompletionEvaluator.evaluate(p, 'Claimed completed').allowed


def test_delegation_is_not_user_confirmation_of_chosen_design(tmp_path_factory):
    from backend.core.loop.decision_support import confirmed_user_state
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'You choose the transport')
    update(p, [{'state_topic': 'trip', 'disposition': 'delegated',
                'quote': 'You choose the transport', 'value': 'Transport selection delegated'}])
    assert w.status()['complete']
    assert confirmed_user_state(p.session.host_ledger)[0]['disposition'] == 'delegated'


def revision_case(root):
    p = process(root)
    w, old, node = pending_reply(p, 'Original user value')
    coordinator(p).check()
    update(p, [{'state_topic': 'trip', 'disposition': 'confirmed',
                'quote': 'Original user value', 'value': 'Original user value'}])
    card = w.ask_semantic(question('next_facet'))
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': p.active_task_id,
        'process_id': p.process_id, 'decision_id': card['decision_id'],
        'answer': 'Change the earlier value to the revised user value'})
    p.pending_decision = None
    revised = {'state_topic': 'trip', 'disposition': 'confirmed', 'revise': True,
               'quote': 'Change the earlier value to the revised user value', 'value': 'Revised user value'}
    return p, w, old, card, node, revised


def test_explicit_new_reply_revises_confirmation_without_a_second_question(tmp_path_factory):
    from backend.core.loop.decision_support import confirmed_user_state
    p, w, old, new, node, item = revision_case(tmp_path_factory)
    count = sum(e.get('event') == 'user_decision_requested' for e in p.session.host_ledger)
    update(p, [item])
    assert w._load()['records'][node]['decision_id'] == new['decision_id']
    current = next(v for v in confirmed_user_state(p.session.host_ledger) if v['topic'] == 'trip')
    assert current['value'] == 'Change the earlier value to the revised user value'
    assert current['agent_interpretation'] == 'Revised user value'
    assert current['supersedes_decision_id'] == old['decision_id']
    assert sum(e.get('event') == 'user_decision_requested' for e in p.session.host_ledger) == count
    assert not p.pending_decision and not p.approval_grants


def test_revising_confirmation_requires_explicit_semantic_intent(tmp_path_factory):
    p, _, _, _, _, item = revision_case(tmp_path_factory)
    item.pop('revise')
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(ValueError) as raised:
        update(p, [item])
    assert raised.value.code == 'ALIGNMENT_REPLY_REVISION_REQUIRED'
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_revision_cannot_replay_an_older_reply(tmp_path_factory):
    p, _, old, _, _, item = revision_case(tmp_path_factory)
    update(p, [item])
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(ValueError) as raised:
        update(p, [{**item, 'decision_id': old['decision_id'], 'quote': 'Original user value',
                    'value': 'Original user value'}])
    assert raised.value.code == 'ALIGNMENT_REPLY_REVISION_SOURCE_INVALID'
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_bad_batch_does_not_partially_commit_a_revision(tmp_path_factory):
    p, _, _, _, _, item = revision_case(tmp_path_factory)
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(ValueError):
        update(p, [item, {'state_topic': 'invented', 'disposition': 'confirmed',
                         'value': 'not supplied', 'quote': 'not supplied'}])
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_user_can_reopen_a_choice_with_a_source_bound_revision(tmp_path_factory):
    p, w, _, _, node, item = revision_case(tmp_path_factory)
    update(p, [{**item, 'disposition': 'deferred', 'value': '', 'remaining': 'New value not decided'}])
    assert node not in w._load()['records']
    assert not w.guard('write_file', 'workspace_write', {'path': 'output.py'})['allowed']


def test_revision_invalidates_dependent_document_evidence(tmp_path_factory):
    from pathlib import Path
    from backend.core.loop.engineering_workflow import workspace
    p, w, _, _, node, item = revision_case(tmp_path_factory)
    plan = deepcopy(w._load()['plan'])
    plan['nodes'].append({'id': 'doc', 'kind': 'document', 'path': 'evidence.txt', 'depends_on': [node]})
    w.configure({'profiles': plan['profiles'], 'nodes': plan['nodes']})
    Path(workspace(p), 'evidence.txt').write_text('Evidence for original value', 'utf-8')
    w.record('doc')
    assert 'doc' in w._load()['records']
    update(p, [item])
    assert 'doc' not in w._load()['records']


def test_revision_rejects_a_foreign_process_source(tmp_path_factory):
    p, _, _, new, _, item = revision_case(tmp_path_factory)
    next(e for e in p.session.host_ledger if e.get('event') == 'user_decision_requested'
         and e.get('decision_id') == new['decision_id'])['process_id'] = 'foreign'
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(ValueError) as raised:
        update(p, [item])
    assert raised.value.code == 'ALIGNMENT_REPLY_REVISION_SOURCE_INVALID'
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_direct_workflow_revision_uses_the_same_granular_error(tmp_path_factory):
    _, w, _, new, node, item = revision_case(tmp_path_factory)
    item.pop('revise')
    result = w.invoke({'operation': 'resolve_answer', 'node_id': node,
                       'decision_id': new['decision_id'], 'content': item})
    assert result['error'] == 'ALIGNMENT_REPLY_REVISION_REQUIRED'


def test_unknown_fact_can_stay_unknown_in_readonly_conclusion(tmp_path_factory):
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'This record is unavailable; give a qualified conclusion')
    coordinator(p).check()
    update(p, [{'state_topic': 'trip', 'disposition': 'not_needed',
                'quote': 'This record is unavailable; give a qualified conclusion',
                'reason': 'The bounded conclusion can explicitly retain the missing fact as unknown'}])
    assert w.status()['nodes'][0]['status'] == 'retired'
    from backend.core.loop.main_dialogue import apply_answer_updates
    apply_answer_updates(p, [], {'quote': 'give a qualified conclusion',
                               'discussion': 'Qualified conclusion; record remains unknown'})
    assert HostCompletionEvaluator.evaluate(p, 'Qualified conclusion; record remains unknown').allowed
    p.task_kind = 'action'
    assert not w.guard('write_file', 'workspace_write', {'path': 'report.txt'})['allowed']


def test_user_declared_obligation_cannot_be_retired(tmp_path_factory):
    p = process(tmp_path_factory)
    w, _, node = pending_reply(p, 'Give a summary')
    state = w._load()
    state['elicitation_tiers'] = {}
    w._save(state)
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(ValueError):
        update(p, [{'node_id': node, 'disposition': 'not_needed', 'quote': 'Give a summary',
                    'reason': 'User requests a summary'}])
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_control_or_cancelled_receipt_cannot_supply_a_business_answer(tmp_path_factory):
    p = process(tmp_path_factory)
    _, card, _ = pending_reply(p, 'Japan')
    p.session.host_ledger.append({'event': 'user_decision_cancelled', 'task_id': 't',
                                 'decision_id': card['decision_id']})
    with pytest.raises(ValueError):
        update(p, [{'state_topic': 'trip', 'disposition': 'confirmed', 'value': 'Japan', 'quote': 'Japan'}])


def test_old_auxiliary_certificate_does_not_resolve_real_graph(tmp_path_factory):
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Not decided')
    p.update_context_snapshot(lambda ctx: {**ctx, 'alignment_runtime': {
        'task_id': 't', 'process_id': 'p', 'enforced': True, 'review_policy': 'on_demand_v22',
        'delivery': {'candidate_digest': 'old', 'input_digest': 'old'}}})
    coordinator(p).check()
    assert not HostCompletionEvaluator.evaluate(p, 'Unknown').allowed
    assert not w.status()['complete']


def test_updates_attach_to_the_next_real_question_without_an_extra_semantic_turn(tmp_path_factory):
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Japan')
    result = w.invoke({'operation': 'ask_semantic', 'answer_updates': [
        {'state_topic': 'trip', 'disposition': 'confirmed', 'value': 'Japan', 'quote': 'Japan'}],
        'request': question('dates', after=['trip'])})
    assert result['status'] == 'awaiting_user'
    assert result['state_topic'] == 'dates'
    assert next(n for n in w.status()['nodes'] if n['state_topic'] == 'trip')['status'] == 'complete'


def test_current_candidate_identity_survives_without_a_semantic_verdict(tmp_path_factory):
    p = process(tmp_path_factory)
    c = coordinator(p)
    c.check('Supported result')
    binding = p.context_snapshot['alignment_runtime']['delivery']
    assert binding['scope'] == 'candidate_identity_only'
    assert HostCompletionEvaluator.evaluate(p, 'Supported result').allowed
    changed = HostCompletionEvaluator.evaluate(p, 'Different result')
    assert not changed.allowed
    assert any(g['code'] == 'ALIGNMENT_DELIVERY_BINDING_STALE' for g in changed.gates)


def test_diagnostics_distinguish_target_and_source_failures(tmp_path_factory):
    from backend.core.loop.main_dialogue import DialogueUpdateError
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Japan')
    with pytest.raises(DialogueUpdateError) as missing:
        update(p, [{'node_id': 'nonexistent-node', 'disposition': 'confirmed', 'value': 'Japan', 'quote': 'Japan'}])
    assert missing.value.code == 'ALIGNMENT_REPLY_TARGET_MISSING'
    with pytest.raises(DialogueUpdateError) as source:
        update(p, [{'state_topic': 'trip', 'disposition': 'confirmed', 'value': 'Invented', 'quote': 'Not in the reply'}])
    assert source.value.code == 'ALIGNMENT_REPLY_SOURCE_UNAVAILABLE'


def test_volunteered_new_fact_needs_no_repeated_question(tmp_path_factory):
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Workdays 09:00–18:00; summarize confirmed and undecided items only')
    from backend.core.loop.main_dialogue import apply_answer_updates
    apply_answer_updates(p, [{'state_topic': 'hours', 'disposition': 'confirmed',
        'value': 'Workdays 09:00–18:00', 'quote': 'Workdays 09:00–18:00'}],
        {'mode': 'exploration', 'quote': 'summarize confirmed and undecided items only'})
    assert next(n for n in w.status()['nodes'] if n['state_topic'] == 'hours')['status'] == 'complete'
    coordinator(p).check('Current discussion summary, original choice undecided')
    assert HostCompletionEvaluator.evaluate(p, 'Current discussion summary, original choice undecided').allowed
    assert not w.guard('write_file', 'workspace_write', {'path': 'product.py'})['allowed']
    assert next(n for n in w.status()['nodes'] if n['state_topic'] == 'trip')['status'] == 'ready'


@pytest.mark.parametrize('disposition', ['accepted', 'approved', None, []])
def test_invalid_disposition_has_its_own_diagnostic_and_rolls_back_the_batch(tmp_path_factory, disposition):
    from backend.core.loop.main_dialogue import DialogueUpdateError, update_error
    p = process(tmp_path_factory)
    pending_reply(p, 'Workdays only; dates remain undecided')
    coordinator(p).check()
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(DialogueUpdateError) as invalid:
        update(p, [{'state_topic': 'availability', 'disposition': 'confirmed', 'quote': 'Workdays only'},
                   {'state_topic': 'trip', 'disposition': disposition, 'quote': 'dates remain undecided'}])
    result = update_error(invalid.value)
    assert result['error'] == 'ALIGNMENT_REPLY_DISPOSITION_INVALID'
    assert result['error_info']['catalog_id'] == 'GITGO-E5182'
    assert 'confirmed, partial, deferred, delegated or not_needed' in result['message']
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_compound_volunteered_facts_survive_source_built_summary_without_closing_other_questions(tmp_path_factory):
    from backend.core.loop.main_dialogue import apply_answer_updates
    from backend.core.loop.models import ProcessStatus
    from backend.core.loop.decision_support import confirmed_user_state
    from tests.test_workbench_delivery import _delivery
    p = process(tmp_path_factory)
    reply = ('Travel dates not decided. The group has 6 people. '
             'We can spend 900 dollars total. Only summarize.')
    w, _, original = pending_reply(p, reply)
    coordinator(p).check()
    apply_answer_updates(p, [
        {'state_topic': 'trip', 'disposition': 'deferred', 'quote': 'Travel dates not decided.',
         'remaining': 'Select dates'},
        {'state_topic': 'travellers', 'disposition': 'confirmed', 'quote': 'The group has 6 people.'},
        {'state_topic': 'budget', 'disposition': 'confirmed', 'quote': 'We can spend 900 dollars total.',
         'value': '900 dollars including accommodation'}],
        {'mode': 'exploration', 'quote': 'Only summarize.'})
    delivered = _delivery(p).finish('## Known\n\nDraft facts.\n\n## Open\n\nDates remain open.',
                                  0, {'action': 'continue', 'usage_ratio': 0}, source='complete_task')
    assert not delivered.retry and p.status is ProcessStatus.COMPLETED
    response = delivered.result['response']
    assert '> The group has 6 people.' in response and '> We can spend 900 dollars total.' in response
    assert 'including accommodation' not in response and 'Dates remain open.' in response
    assert {d['topic']: d['value'] for d in confirmed_user_state(p.session.host_ledger)} == {
        'travellers': 'The group has 6 people.', 'budget': 'We can spend 900 dollars total.'}
    assert next(n for n in w.status()['nodes'] if n['id'] == original)['status'] != 'complete'
    assert sum(e.get('event') == 'user_decision_requested' for e in p.session.host_ledger) == 1
    assert not w.guard('write_file', 'workspace_write', {'path': 'booking.py'})['allowed']


@pytest.mark.parametrize('scope_mode', [None, 'exploration', 'decision_summary', 'decision_comparison'])
def test_exploration_cannot_waive_user_authored_obligation_or_execution_contract(tmp_path_factory, scope_mode):
    from backend.core.loop.main_dialogue import apply_answer_updates
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Summarize only')
    state = w._load();state['elicitation_tiers'] = {};w._save(state)
    scope = {'quote': 'Summarize only', **({'mode': scope_mode} if scope_mode else {})}
    apply_answer_updates(p, [], scope)
    coordinator(p).check('Summary')
    assert not HostCompletionEvaluator.evaluate(p, 'Summary').allowed
    p.update_context_snapshot(lambda ctx: {**ctx, 'task_contract': {'execution_mode': 'self_execute'}})
    with pytest.raises(ValueError):
        apply_answer_updates(p, [], scope)


def test_readonly_completion_contract_is_complete_and_long_text_is_not_forced_into_an_action_summary(tmp_path_factory):
    import json
    from backend.core.loop.executor import _build_internal_tools
    from backend.core.loop.tool_surface import _select_authorized_tools
    from backend.core.loop.tool_context import tool_definition, contract_overview
    from backend.core.loop.tools import ToolRegistry
    p = process(tmp_path_factory)
    pending_reply(p, 'Known fact')
    p.tool_registry = ToolRegistry(['complete_task'])
    tools = _build_internal_tools(p, {}, workspace_path=str(tmp_path_factory))
    selected, _ = _select_authorized_tools(p, tools)
    definition = tool_definition(selected['complete_task'])
    assert contract_overview(definition) == definition
    assert len(json.dumps(definition, separators=(',', ':'))) <= 2048
    assert definition['parameters']['properties']['result']['maxLength'] == 32000
    from backend.core.loop.main_dialogue import RESPONSE_SCOPE_SCHEMA
    assert definition['parameters']['properties']['scope_update'] == RESPONSE_SCOPE_SCHEMA
    assert definition['parameters']['required'] == ['scope_update']
    assert 'anyOf' not in definition['parameters']
    assert 'mode' not in definition['parameters']['properties']['scope_update']['properties']
    assert tools['complete_task'].parameters['required'] == ['result', 'verification']


@pytest.mark.parametrize('mode', ['summary', []])
def test_invalid_presentation_mode_has_distinct_diagnostic_and_atomic_rejection(tmp_path_factory, mode):
    from backend.core.loop.main_dialogue import apply_answer_updates, DialogueUpdateError, update_error
    p = process(tmp_path_factory)
    pending_reply(p, 'Japan. Summarize only.')
    coordinator(p).check()
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(DialogueUpdateError) as invalid:
        apply_answer_updates(p, [{'state_topic': 'trip', 'disposition': 'confirmed', 'quote': 'Japan.'}],
            {'mode': mode, 'quote': 'Summarize only.'})
    result = update_error(invalid.value)
    assert result['error_info']['catalog_id'] == 'GITGO-E5183'
    assert 'scope_update.mode' in result['message']
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_status_completion_needs_only_the_source_request_and_actual_answer_updates(tmp_path_factory):
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from tests.test_workbench_delivery import _delivery
    p = process(tmp_path_factory)
    w, _, node = pending_reply(p, 'Six people. Summarize current decisions.')
    args = {'scope_update': {'quote': 'Summarize current decisions.'},
            'answer_updates': [{'state_topic': 'group_size', 'disposition': 'confirmed', 'quote': 'Six people.'}]}
    before_args = deepcopy(args)
    recorded = build_internal_tools(p, {}, str(tmp_path_factory), None, None, None)['complete_task'].execute(args)
    assert recorded['accepted'] and args == before_args
    assert p.context_snapshot['alignment_runtime']['scope']['response_contract'] == 'decision_status_v2'
    result = _delivery(p).finish(p.completion_claim.result, 0, {'action': 'continue', 'usage_ratio': 0}, source='complete_task')
    assert not result.retry and '> Six people.' in result.result['response']
    assert 'Decision status.' not in result.result['response']
    assert node not in w._load()['records']  # No answer to the original question was invented.
    assert not w.guard('write_file', 'workspace_write', {'path': 'trip.py'})['allowed']


@pytest.mark.parametrize('body', ['', 5, 'x' * 32001])
def test_invalid_discussion_rejects_the_whole_completion_before_recording(tmp_path_factory, body):
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    p = process(tmp_path_factory)
    pending_reply(p, 'Japan. Summarize status and compare.')
    before = deepcopy((p.context_snapshot, p.session.host_ledger, p.completion_claim))
    result = build_internal_tools(p, {}, str(tmp_path_factory), None, None, None)['complete_task'].execute({
        'scope_update': {'quote': 'Summarize status and compare.', 'discussion': body},
        'answer_updates': [{'state_topic': 'trip', 'disposition': 'confirmed', 'quote': 'Japan.'}]})
    assert not result['accepted'] and result['error_info']['catalog_id'] == 'GITGO-E5184'
    assert (p.context_snapshot, p.session.host_ledger, p.completion_claim) == before


def test_active_current_response_cannot_be_downgraded_to_legacy_prose(tmp_path_factory):
    from backend.core.loop.main_dialogue import apply_answer_updates, DialogueUpdateError, update_error
    p = process(tmp_path_factory)
    pending_reply(p, 'Japan. Summarize status only.')
    apply_answer_updates(p, [], {'quote': 'Summarize status only.'})
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    with pytest.raises(DialogueUpdateError) as failure:
        apply_answer_updates(p, [{'state_topic': 'trip', 'disposition': 'confirmed', 'quote': 'Japan.'}],
            {'mode': 'exploration', 'quote': 'Summarize status only.'})
    assert update_error(failure.value)['error_info']['catalog_id'] == 'GITGO-E5185'
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_new_actual_reply_does_not_inherit_a_stale_response_contract(tmp_path_factory):
    from backend.core.loop.main_dialogue import apply_answer_updates
    from backend.core.loop.alignment_evidence import exploratory_scope
    p = process(tmp_path_factory)
    pending_reply(p, 'Only summarize status.')
    apply_answer_updates(p, [], {'quote': 'Only summarize status.'})
    p.session.host_ledger.extend([
        {'event': 'user_decision_requested', 'task_id': p.active_task_id, 'process_id': p.process_id,
         'decision_id': 'later-business', 'kind': 'clarification', 'state_topic': 'next', 'question': 'Next?'},
        {'event': 'user_decision_received', 'task_id': p.active_task_id, 'process_id': p.process_id,
         'decision_id': 'later-business', 'answer': 'Compare the alternatives now.'}])
    assert not exploratory_scope(p)
    apply_answer_updates(p, [], {'quote': 'Compare the alternatives now.', 'discussion': 'The requested alternatives.'})
    scope = p.context_snapshot['alignment_runtime']['scope']
    assert exploratory_scope(p) and scope['decision_id'] == 'later-business' and scope['include_discussion']


def test_result_only_completion_cannot_skip_components_even_after_the_last_question_is_answered(tmp_path_factory):
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    p = process(tmp_path_factory)
    w, _, node = pending_reply(p, 'Japan. Summarize current decisions.')
    tool = build_internal_tools(p, {}, str(tmp_path_factory), None, None, None)['complete_task']
    before = deepcopy((p.context_snapshot, p.session.host_ledger, p.completion_claim))
    update_args = [{'state_topic': 'trip', 'disposition': 'confirmed', 'quote': 'Japan.'}]
    bad = tool.execute({'result': 'A free draft with new rules.', 'answer_updates': update_args})
    assert not bad['accepted'] and bad['error_info']['catalog_id'] == 'GITGO-E5186'
    assert (p.context_snapshot, p.session.host_ledger, p.completion_claim) == before
    assert node not in w._load()['records']
    assert tool.execute({'scope_update': {'quote': 'Summarize current decisions.'}, 'answer_updates': update_args})['accepted']
    assert node in w._load()['records']


def test_text_marker_and_workflow_alias_cannot_bypass_the_same_response_boundary(tmp_path_factory):
    from backend.core.loop.main_dialogue import owns_dialogue
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Japan. Summarize current decisions.')
    update(p, [{'state_topic': 'trip', 'disposition': 'confirmed', 'quote': 'Japan.'}])
    assert owns_dialogue(p.context_snapshot['alignment_runtime'])
    assert w.status()['complete']  # Having no open question is not response-scope certification.
    gate = HostCompletionEvaluator.evaluate(p, 'A free draft.\nTASK_COMPLETE')
    assert not gate.allowed and any(g['code'] == 'ALIGNMENT_RESPONSE_COMPONENTS_REQUIRED' for g in gate.gates)
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    alias = w.invoke({'operation': 'status', 'scope_update': {
        'mode': 'exploration', 'quote': 'Summarize current decisions.'}})
    assert alias['error_info']['catalog_id'] == 'GITGO-E5186'
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_workflow_resolve_alias_uses_the_same_retirement_and_source_boundaries(tmp_path_factory):
    p = process(tmp_path_factory)
    w, card, node = pending_reply(p, 'The record is unavailable. Only summarize current status.')
    result = w.invoke({'operation': 'resolve_answer', 'node_id': node,
        'decision_id': card['decision_id'], 'content': {'disposition': 'not_needed',
            'quote': 'Only summarize current status.', 'reason': 'The readonly status can retain the unknown.'},
        'scope_update': {'quote': 'Only summarize current status.'}})
    assert result['status'] == 'retired' and not result['authority_granted']
    assert w._load()['records'][node]['elicitation_retirement']
    assert not w.guard('write_file', 'workspace_write', {'path': 'program.py'})['allowed']


def test_workflow_resolve_alias_reports_invalid_disposition_without_generic_recovery(tmp_path_factory):
    p = process(tmp_path_factory)
    w, card, node = pending_reply(p, 'Japan')
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    result = w.invoke({'operation': 'resolve_answer', 'node_id': node,
        'decision_id': card['decision_id'], 'content': {'disposition': 'accepted', 'quote': 'Japan'}})
    assert result['error_info']['catalog_id'] == 'GITGO-E5182'
    assert (p.context_snapshot, p.session.host_ledger) == before
