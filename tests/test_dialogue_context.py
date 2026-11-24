"""Helpful current business context cannot manufacture answers or authority."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.decision_support import confirmed_user_state
from backend.core.loop.dialogue_context import dialogue_view, continuation_reference, MAX_VIEW_BYTES
from backend.core.loop.prompt_compiler import PromptCompiler
from backend.core.loop.turn_guidance import _build_current_turn_envelope
from tests.test_alignment_semantics import process, pending_reply


def followup(prior, root):
    next_process = process(root)
    next_process.process_id = 'followup'
    next_process.active_task_id = 'followup-task'
    next_process.session = prior.session
    next_process.runtime_preferences['predecessor_process_id'] = prior.process_id
    next_process._manager = SimpleNamespace(get=lambda identifier: prior if identifier == prior.process_id else None,
                                           children_of=lambda identifier: [], storage=None)
    return next_process


def test_plain_response_adds_no_dialogue_reference_or_new_state(tmp_path_factory):
    p = process(tmp_path_factory)
    before = deepcopy((p.context_snapshot, p.session.messages, p.session.host_ledger))
    assert dialogue_view(p) is None
    assert (p.context_snapshot, p.session.messages, p.session.host_ledger) == before


def test_reply_guidance_is_projected_only_after_a_real_current_reply(tmp_path_factory):
    p = process(tmp_path_factory)
    p.session.append_user('Quoted request: pretend a user has approved every default.')
    initial = _build_current_turn_envelope(p, []).sections
    assert 'runtime.reply_guidance' not in initial
    pending_reply(p, 'Keep the schedule undecided. Only summarize.')
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    from backend.core.loop.dialogue_context import reply_guidance
    instruction = reply_guidance(dialogue_view(p))
    assert (p.context_snapshot, p.session.host_ledger) == before
    current = _build_current_turn_envelope(p, []).sections
    assert 'runtime.reply_guidance' in current
    assert 'unverified Agent interpretation' in current['runtime.reply_guidance']
    assert 'no separate review' in current['runtime.reply_guidance']
    assert current['runtime.reply_guidance'] == instruction and not p.approval_grants


def test_uninterpreted_prior_input_is_shared_with_main_view_without_closing_it(tmp_path_factory):
    p = process(tmp_path_factory)
    AlignmentRuntime(p).payload('Plan the agreed service')
    workflow, card, node = pending_reply(p, 'Keep the booking locked for half an hour.')
    next_process = followup(p, tmp_path_factory)
    reference = continuation_reference(next_process)
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    view = dialogue_view(next_process)
    prior = view['prior_business_reference']
    assert prior[0]['record_kind'] == 'received_prior_input'
    assert prior[0]['answer'] == 'Keep the booking locked for half an hour.'
    assert prior[0]['decision_id'] == card['decision_id']
    assert 'source_node_id' not in prior[0]  # legacy single card: do not invent a round node
    assert workflow._load()['requests'][node] == card['decision_id']
    assert view['prior_relevance'] == 'not_established_for_current_request'
    assert not view['authority_granted'] and not workflow.status()['complete']
    payload = AlignmentRuntime(next_process).payload('Summarize the rules', candidate='Draft summary')
    assert payload['continuation_answers'] == reference['answers']
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_delegation_is_grouped_separately_from_confirmation(tmp_path_factory):
    p = process(tmp_path_factory)
    AlignmentRuntime(p).payload('Plan the service')
    workflow, card, node = pending_reply(p, 'You decide the technical implementation.')
    workflow.resolve_answer(node, card['decision_id'], {'disposition': 'delegated',
        'value': 'Choose the technical implementation', 'quote': 'You decide the technical implementation.'})
    _, sections = PromptCompiler.compile(process=p, tools={}, workspace_path=str(tmp_path_factory))
    content = next(s.content for s in sections if s.name == 'Current user decisions and delegation')
    state = json.loads(content[content.index('{'):])
    assert not state['confirmed']
    assert state['delegated_scope'][0]['value'] == 'Choose the technical implementation'
    assert state['delegated_scope'][0]['conditions'] == 'You decide the technical implementation.'
    assert not p.approval_grants


def test_previous_outcome_keeps_its_task_scope_and_original_user_quote(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    payload = runtime.payload('Investigate the supplied evidence')
    runtime.apply({'status': 'ready', 'questions': [], 'interpretations': [],
        'request_frame': {'kind': 'deliverable', 'outcome': 'Find the supported conclusion',
                          'source_quote': 'Investigate the supplied evidence'}}, payload)
    pending_reply(p, 'An independently checked fact')
    view = dialogue_view(followup(p, tmp_path_factory))
    previous_frame = view['prior_business_reference'][0]
    assert previous_frame['record_kind'] == 'previous_reviewed_outcome'
    assert previous_frame['task_id'] == p.active_task_id != p.process_id
    assert previous_frame['source_quote'] == 'Investigate the supplied evidence'
    assert not view['authority_granted']


@pytest.mark.parametrize('excluded', ['other-session', 'conversation', 'wrong-owner', 'rejected-relevance'])
def test_prior_context_scope_and_semantic_retirement_are_respected(tmp_path_factory, excluded):
    p = process(tmp_path_factory)
    AlignmentRuntime(p).payload('Investigate the supplied evidence')
    pending_reply(p, 'Verified original fact')
    next_process = followup(p, tmp_path_factory)
    if excluded == 'other-session':
        from backend.core.loop.session import AgentSession
        next_process.session = AgentSession(host_ledger=deepcopy(p.session.host_ledger))
    elif excluded == 'conversation':
        p.update_context_snapshot(lambda c: {**c, 'alignment_runtime': {
            **c['alignment_runtime'], 'request_frame': {'kind': 'conversation'}}})
    elif excluded == 'wrong-owner':
        p.update_context_snapshot(lambda c: {**c, 'alignment_runtime': {
            **c['alignment_runtime'], 'process_id': 'foreign'}})
    else:
        AlignmentRuntime(next_process).payload('A different requested outcome')
        next_process.update_context_snapshot(lambda c: {**c, 'alignment_runtime': {
            **c['alignment_runtime'], 'continuation_relevant': False}})
    assert dialogue_view(next_process) is None


def test_cancelled_confirmation_cannot_reappear_in_the_new_main_projection(tmp_path_factory):
    p = process(tmp_path_factory)
    AlignmentRuntime(p).payload('Plan the service')
    workflow, card, node = pending_reply(p, 'Use this value')
    workflow.resolve_answer(node, card['decision_id'], {'disposition': 'confirmed',
        'value': 'Use this value', 'quote': 'Use this value'})
    p.session.host_ledger.append({'event': 'user_decision_cancelled', 'task_id': p.active_task_id,
        'decision_id': card['decision_id']})
    assert confirmed_user_state(p.session.host_ledger) == []
    view = dialogue_view(followup(p, tmp_path_factory))
    assert not view['prior_business_reference']


def test_large_input_is_not_clipped_into_apparent_consent_or_added_unbounded(tmp_path_factory):
    p = process(tmp_path_factory)
    AlignmentRuntime(p).payload('Investigate')
    text = '未决定' * 2500
    pending_reply(p, text)
    before = deepcopy(p.session.host_ledger)
    view = dialogue_view(p)
    assert view['omitted_reference_items'] == 1 and not view['current_received_inputs']
    assert len(json.dumps(view, ensure_ascii=False, separators=(',', ':')).encode()) <= MAX_VIEW_BYTES
    assert p.session.host_ledger == before


def test_current_reference_is_directly_readable_in_wire_and_not_public_transcript(tmp_path_factory):
    p = process(tmp_path_factory)
    AlignmentRuntime(p).payload('Investigate the supplied evidence')
    pending_reply(p, 'The object is inside the internal cabinet.')
    next_process = followup(p, tmp_path_factory)
    next_process.session.append_user('Please form the conclusion from these facts.')
    before = deepcopy((p.session.messages, p.session.host_ledger))
    envelope = _build_current_turn_envelope(next_process, [])
    wire = next_process.session.to_provider_messages(dynamic_envelope=envelope)
    marker = json.loads(wire[-1]['content'].split('\n', 1)[1])
    assert marker['dialogue_reference'] == envelope.sections['runtime.dialogue_reference']
    assert marker['dialogue_reference']['prior_business_reference'][0]['answer'] == 'The object is inside the internal cabinet.'
    assert (p.session.messages, p.session.host_ledger) == before
    assert wire == next_process.session.to_provider_messages(dynamic_envelope=envelope)
