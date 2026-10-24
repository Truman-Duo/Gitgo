import copy

import pytest

from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.completion_protocol import HostCompletionEvaluator
from backend.core.loop.decision_support import confirmed_user_state
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession


def process(root):
    return AgentProcess(process_id='p', role='supervisor', ring_level=RingLevel.RING_3,
                        active_task_id='t', actor_kind='supervisor', session=AgentSession(),
                        worktree_path=str(root), task_description='Plan a trip')


def pending_reply(p, answer):
    w = EngineeringWorkflow(p)
    request = w.ask_semantic({'kind': 'clarification', 'question': 'Destination, dates and travellers?',
                             'state_topic': 'trip', 'why_user_must_decide': 'Changes itinerary', 'options': []})
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': 't',
                                 'decision_id': request['decision_id'], 'state_topic': 'trip', 'answer': answer})
    p.pending_decision = None
    return w, request, w.status()['nodes'][0]['id']


def interpretation(node, request, text, disposition='confirmed', remaining=''):
    return {'node_id': node, 'decision_id': request['decision_id'], 'value': text,
            'quote': text, 'disposition': disposition, 'remaining': remaining}


def test_received_text_is_not_confirmed_or_completion_evidence(tmp_path_factory):
    p = process(tmp_path_factory)
    w, _, _ = pending_reply(p, 'Japan with family')
    assert not w.status()['complete']
    assert not confirmed_user_state(p.session.host_ledger)
    assert not HostCompletionEvaluator.evaluate(p, 'A beautiful itinerary').allowed


def test_direct_model_question_retains_origin_for_source_bound_exploratory_delivery(tmp_path_factory):
    p = process(tmp_path_factory)
    p.task_kind = 'answer'
    runtime = AlignmentRuntime(p)
    runtime.payload('Plan a tool')
    w, request, node = pending_reply(p, 'Only compare possible directions; do not choose my purpose or ask again.')
    answer = w._answers(w.status()['nodes'][0])[-1]['answer']
    payload = runtime.payload(candidate='Possible directions with tradeoffs, purpose still undecided.')
    result = runtime.apply({'status': 'ready', 'questions': [],
        'interpretations': [interpretation(node, request, answer, disposition='deferred', remaining='Purpose undecided')],
        'scope_update': {'mode': 'exploration', 'request': answer, 'quote': answer, 'decision_id': request['decision_id']},
        'delivery_check': {'complete': True, 'evidence': [payload['candidate']]}}, payload)
    assert result['status'] == 'ready'
    assert not w.status()['complete']
    assert HostCompletionEvaluator.evaluate(p, payload['candidate']).allowed
    p.session.host_ledger = [e for e in p.session.host_ledger if e['event'] != 'user_decision_received']
    assert not HostCompletionEvaluator.evaluate(p, payload['candidate']).allowed


def test_asking_existing_declared_requirement_does_not_reclassify_it_as_elicited(tmp_path_factory):
    p = process(tmp_path_factory)
    w = EngineeringWorkflow(p)
    w.configure({'profiles': ['alignment'], 'nodes': [{'id': 'fixed', 'kind': 'decision', 'state_topic': 'purpose'}]})
    w.ask_semantic({'kind': 'clarification', 'state_topic': 'purpose', 'question': 'Purpose?',
                    'why_user_must_decide': 'Declared requirement', 'options': []})
    assert not w._load().get('elicitation_tiers')


def test_partial_answer_reopens_only_remaining_issue(tmp_path_factory):
    p = process(tmp_path_factory)
    w, request, node = pending_reply(p, 'Japan with family')
    runtime = AlignmentRuntime(p)
    result = runtime.apply({'status': 'needs_input',
        'interpretations': [interpretation(node, request, 'Japan with family', 'partial', 'Dates and traveller ages')],
        'questions': [{'state_topic': 'trip', 'question': 'Which dates and traveller ages?',
                       'why_user_must_decide': 'Season and walking constraints', 'depends_on': []}]}, runtime.payload())
    assert result['decision']['questions'][0]['node_id'] == node
    assert result['decision']['questions'][0]['question'] == 'Which dates and traveller ages?'
    assert not w.status()['complete']
    assert not confirmed_user_state(p.session.host_ledger)
    assert not HostCompletionEvaluator.evaluate(p, 'Done').allowed


def test_defer_preserves_same_task_and_a_real_pending_question(tmp_path_factory):
    p = process(tmp_path_factory)
    w, request, node = pending_reply(p, 'I have not decided. Wait for me.')
    runtime = AlignmentRuntime(p)
    result = runtime.apply({'status': 'waiting', 'questions': [],
        'interpretations': [interpretation(node, request, 'I have not decided. Wait for me.', 'deferred', 'User undecided')]}, runtime.payload())
    assert result['decision']['task_id'] == 't'
    assert p.pending_decision
    assert not w.status()['complete']
    assert not confirmed_user_state(p.session.host_ledger)


def test_source_forgery_stale_question_and_ready_bypass_fail_atomically(tmp_path_factory):
    p = process(tmp_path_factory)
    _, request, node = pending_reply(p, 'Japan')
    runtime = AlignmentRuntime(p)
    payload = runtime.payload()
    for proposal in [interpretation(node, request, 'France'),
                     {**interpretation(node, request, 'Japan'), 'decision_id': 'foreign'},
                     interpretation(node, request, 'Japan', 'partial', 'dates')]:
        context, ledger = copy.deepcopy(p.context_snapshot), copy.deepcopy(p.session.host_ledger)
        with pytest.raises(ValueError):
            runtime.apply({'status': 'ready', 'interpretations': [proposal], 'questions': []}, payload)
        assert p.context_snapshot == context
        assert p.session.host_ledger == ledger


def test_confirmed_answer_is_used_and_removing_receipt_reblocks_completion(tmp_path_factory):
    p = process(tmp_path_factory)
    w, request, node = pending_reply(p, 'Japan, April, two adults')
    runtime = AlignmentRuntime(p)
    runtime.apply({'status': 'ready', 'questions': [],
                   'interpretations': [interpretation(node, request, 'Japan, April, two adults')]}, runtime.payload())
    assert w.status()['complete']
    assert confirmed_user_state(p.session.host_ledger)[0]['value'] == 'Japan, April, two adults'
    assert not HostCompletionEvaluator.evaluate(p, 'April Japan itinerary for two adults').allowed
    runtime.apply({'status': 'ready', 'questions': [], 'interpretations': []}, runtime.payload(candidate='April Japan itinerary for two adults'))
    assert HostCompletionEvaluator.evaluate(p, 'April Japan itinerary for two adults').allowed
    assert not HostCompletionEvaluator.evaluate(p, 'A different itinerary').allowed
    w.resolve_answer(node, request['decision_id'], {'disposition': 'confirmed', 'value': 'a paraphrase', 'quote': 'Japan'})
    assert w.status()['nodes'][0]['evidence']['value'] == 'Japan, April, two adults'
    p.session.host_ledger = [e for e in p.session.host_ledger if e['event'] != 'user_decision_received']
    assert not HostCompletionEvaluator.evaluate(p, 'I claim it is resolved').allowed


def test_semantic_frontier_preserves_dependencies_and_simple_tasks_do_not_interview(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    assert runtime.apply({'status': 'ready', 'questions': [], 'interpretations': []}, runtime.payload('Rewrite this sentence'))['status'] == 'ready'
    assert not EngineeringWorkflow(p).status()['active']
    result = runtime.apply({'status': 'needs_input', 'interpretations': [], 'questions': [
        {'state_topic': 'destination', 'question': 'Where?', 'why_user_must_decide': 'Place', 'depends_on': []},
        {'state_topic': 'company', 'question': 'With whom?', 'why_user_must_decide': 'Walking', 'depends_on': []}]}, runtime.payload())
    assert len(result['decision']['questions']) == 1
    assert len(EngineeringWorkflow(p).status()['question_frontier']) == 2


def test_new_undecided_round_answer_removes_previous_confirmed_projection(tmp_path_factory):
    p = process(tmp_path_factory)
    w, request, node = pending_reply(p, 'Japan')
    w.resolve_answer(node, request['decision_id'], {'disposition': 'confirmed', 'value': 'Japan', 'quote': 'Japan'})
    reopened = w.revise_decision(node, {'kind': 'clarification', 'question': 'Reconsider destination?',
                                      'why_user_must_decide': 'Changed intent', 'options': []})
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': 't',
        'decision_id': reopened['decision_id'], 'answer': 'Not decided',
        'answers': {node: {'state_topic': 'trip', 'answer': 'Not decided'}}})
    assert not confirmed_user_state(p.session.host_ledger)


def test_topic_dependencies_and_volunteered_facts_avoid_reasking(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    first = runtime.apply({'status': 'needs_input', 'interpretations': [], 'questions': [
        {'state_topic': 'domain', 'question': 'What is being booked?', 'why_user_must_decide': 'Entities'},
        {'state_topic': 'roles', 'question': 'Who uses it?', 'why_user_must_decide': 'Permissions', 'depends_on_topics': ['domain']}]}, runtime.payload())['decision']
    assert [q['state_topic'] for q in first['questions']] == ['domain']
    node = first['questions'][0]['node_id']
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': 't', 'decision_id': first['decision_id'],
        'answers': {node: {'answer': 'Meeting rooms. Admin and employee.', 'state_topic': 'domain'}}})
    p.pending_decision = None
    graph = EngineeringWorkflow(p).status()
    roles = next(n['id'] for n in graph['nodes'] if n['state_topic'] == 'roles')
    result = runtime.apply({'status': 'ready', 'questions': [], 'interpretations': [
        {**interpretation(node, first, 'Meeting rooms.'), 'source_node_id': node},
        interpretation(roles, first, 'Admin and employee.')]}, runtime.payload())
    assert result['status'] == 'ready'
    assert EngineeringWorkflow(p).status()['complete']
    assert {v['topic'] for v in confirmed_user_state(p.session.host_ledger)} == {'domain', 'roles'}
    p.session.host_ledger = [e for e in p.session.host_ledger if e['event'] != 'user_decision_received']
    assert not EngineeringWorkflow(p).status()['complete']


def test_ambiguous_volunteered_source_is_not_guessed(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    request = runtime.apply({'status': 'needs_input', 'interpretations': [], 'questions': [
        {'state_topic': 'domain', 'question': 'Domain?', 'why_user_must_decide': 'Entities'},
        {'state_topic': 'channel', 'question': 'Channel?', 'why_user_must_decide': 'Clients'},
        {'state_topic': 'roles', 'question': 'Who uses it?', 'why_user_must_decide': 'Permissions', 'depends_on_topics': ['domain']}]}, runtime.payload())['decision']
    # An old persisted multi-question receipt still needs ambiguity checks;
    # fresh requests now expose only one node at a time.
    ids = {q['state_topic']: q['id'] for q in EngineeringWorkflow(p).status()['nodes']}
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': 't', 'decision_id': request['decision_id'],
        'answers': {ids['domain']: {'answer': 'Users are employees', 'state_topic': 'domain'},
                    ids['channel']: {'answer': 'Users are customers', 'state_topic': 'channel'}}})
    roles = next(n['id'] for n in EngineeringWorkflow(p).status()['nodes'] if n['state_topic'] == 'roles')
    with pytest.raises(ValueError, match='actual user receipt'):
        EngineeringWorkflow(p).resolve_answer(roles, request['decision_id'],
            {'disposition': 'confirmed', 'value': 'Employees', 'quote': 'Users are'})


def elicited(p, tier='refinement'):
    runtime = AlignmentRuntime(p)
    request = runtime.apply({'status': 'needs_input', 'interpretations': [], 'questions': [
        {'state_topic': 'format', 'question': 'Preferred report layout?', 'why_user_must_decide': 'Layout',
         'tier': tier}]}, runtime.payload())['decision']
    return runtime, request, request['questions'][0]['node_id']


def default_assessment():
    return {'status': 'ready', 'interpretations': [], 'questions': [], 'assumptions': [
        {'state_topic': 'format', 'tier': 'refinement', 'value': 'Concise sections', 'reason': 'Reversible presentation default'}]}


def test_refinement_default_supports_proposal_but_never_confirmed_fact_or_execution(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime, _, _ = elicited(p)
    p.pending_decision = None
    candidate = 'A proposal with concise sections; layout is an assistant assumption.'
    runtime.apply(default_assessment(), runtime.payload(candidate=candidate))
    assert EngineeringWorkflow(p).status()['nodes'][0]['status'] == 'assumed'
    assert not confirmed_user_state(p.session.host_ledger)
    assert HostCompletionEvaluator.evaluate(p, candidate).allowed
    assert not EngineeringWorkflow(p).guard('write_file', 'workspace_write', {'path': 'app.py'})['allowed']
    p.task_kind = 'action'
    assert not HostCompletionEvaluator.evaluate(p, candidate).allowed
    p.task_kind = 'answer'
    p.session.host_ledger = [e for e in p.session.host_ledger if e['event'] != 'alignment_assumption']
    assert not HostCompletionEvaluator.evaluate(p, candidate).allowed


@pytest.mark.parametrize('kind,tier', [('action', 'refinement'), ('answer', 'foundational')])
def test_defaults_cannot_waive_core_intent_or_action_requirements(tmp_path_factory, kind, tier):
    p = process(tmp_path_factory)
    p.task_kind = kind
    runtime, _, _ = elicited(p, tier)
    context, ledger = copy.deepcopy(p.context_snapshot), copy.deepcopy(p.session.host_ledger)
    with pytest.raises(ValueError):
        runtime.apply(default_assessment(), runtime.payload())
    assert p.context_snapshot == context and p.session.host_ledger == ledger


def test_unasked_fundamental_cannot_be_smuggled_in_as_a_default(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    payload = runtime.payload('Design a concrete application')
    for tier in (None, 'foundational'):
        proposal = default_assessment()
        proposal['assumptions'][0]['state_topic'] = 'actual-domain'
        proposal['assumptions'][0]['tier'] = tier
        with pytest.raises(ValueError, match='foundational'):
            runtime.apply(proposal, payload)
        assert not EngineeringWorkflow(p).status()['active']


def test_explicit_wait_and_user_declared_requirement_cannot_be_defaulted(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime, request, node = elicited(p)
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': 't', 'decision_id': request['decision_id'],
        'answers': {node: {'answer': 'Wait for me.', 'state_topic': 'format'}}})
    p.pending_decision = None
    EngineeringWorkflow(p).resolve_answer(node, request['decision_id'],
        {'disposition': 'deferred', 'value': 'Wait for me.', 'quote': 'Wait for me.', 'remaining': 'User waiting'})
    with pytest.raises(ValueError, match='waiting'):
        runtime.apply(default_assessment(), runtime.payload())
    q = process(tmp_path_factory)
    w, _, _ = pending_reply(q, 'Japan')
    runtime = AlignmentRuntime(q)
    proposal = default_assessment()
    proposal['assumptions'][0]['state_topic'] = 'trip'
    with pytest.raises(ValueError, match='user-declared'):
        runtime.apply(proposal, runtime.payload())
    assert not w.status()['complete']


def test_user_requested_exploration_preserves_original_choices_and_requires_actual_receipt(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime, request, node = elicited(p, 'foundational')
    text = 'Compare provisional directions first.'
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': 't', 'decision_id': request['decision_id'],
        'answers': {node: {'answer': text, 'state_topic': 'format'}}})
    p.pending_decision = None
    assessment = {'status': 'ready', 'questions': [], 'interpretations': [],
        'scope_update': {'mode': 'exploration', 'decision_id': request['decision_id'], 'source_node_id': node,
                         'quote': text, 'request': text}}
    runtime.apply(assessment, runtime.payload())
    runtime.apply({'status': 'ready', 'questions': [], 'interpretations': []}, runtime.payload(candidate='Provisional comparison'))
    assert HostCompletionEvaluator.evaluate(p, 'Provisional comparison').allowed
    assert not EngineeringWorkflow(p).status()['complete']
    assert not EngineeringWorkflow(p).guard('write_file', 'workspace_write', {'path': 'app.py'})['allowed']
    p.session.host_ledger = [e for e in p.session.host_ledger if e['event'] != 'user_decision_received']
    assert not HostCompletionEvaluator.evaluate(p, 'Provisional comparison').allowed


def test_task_scoped_confirmed_projection_does_not_invent_new_task_requirements(tmp_path_factory):
    p = process(tmp_path_factory)
    w, request, node = pending_reply(p, 'Japan')
    w.resolve_answer(node, request['decision_id'], {'disposition': 'confirmed', 'value': 'Japan', 'quote': 'Japan'})
    assert confirmed_user_state(p.session.host_ledger, task_id='t')
    assert not confirmed_user_state(p.session.host_ledger, task_id='next-task')


def test_child_does_not_inherit_parent_alignment_receipt_or_scope(tmp_path_factory):
    p = process(tmp_path_factory)
    AlignmentRuntime(p).payload('Parent report')
    child = process(tmp_path_factory)
    child.process_id = 'child'
    child.parent_id = p.process_id
    child.context_snapshot = copy.deepcopy(p.context_snapshot)
    assert HostCompletionEvaluator.evaluate(child, 'Independent child result').allowed
    payload = AlignmentRuntime(child).payload('Child proposal', proposed_questions=[])
    assert payload['request'] == 'Child proposal'
    assert child.context_snapshot['alignment_runtime']['process_id'] == 'child'
    assert not HostCompletionEvaluator.evaluate(child, 'Independent child result').allowed


@pytest.mark.parametrize('child', [False, True])
def test_executor_first_question_routing_keeps_real_gates_and_worker_review(tmp_path_factory, child):
    from backend.core.loop.executor import agent_step
    from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
    from backend.core.loop.outcome import TaskOutcome, OutcomeStatus
    from backend.core.loop.tools import ToolRegistry
    p = process(tmp_path_factory)
    p.max_steps = 12
    p.tool_registry = ToolRegistry(['request_user_decision'])
    if child:
        p.parent_id = 'parent'
        p.actor_kind = 'worker'
        p.role = 'worker'
    class Provider:
        protocol = type('Protocol', (), {'value': 'openai_responses'})()
        capabilities = type('Capabilities', (), {'prompt_cache': 'automatic'})()
        context_window = 8192
        invocations = 0
        stages = []

        def assess_alignment(self, payload, **kwargs):
            self.stages.append(payload['stage'])
            return {'status': 'ready', 'questions': [], 'interpretations': []}

        def stream_events(self, *args, **kwargs):
            self.invocations += 1
            if self.invocations == 1:
                yield ProviderEvent(ProviderEventType.TOOL_CALL_STARTED, tool_call_id='unnecessary',
                                    tool_name='request_user_decision', output_index=0)
                yield ProviderEvent(ProviderEventType.TOOL_CALL_DONE, tool_call_id='unnecessary',
                                    tool_name='request_user_decision', output_index=0,
                                    arguments='{"kind":"clarification","state_topic":"format","question":"Which layout?","why_user_must_decide":"Layout","options":[]}')
            else:
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text='Clear sentence.\nTASK_COMPLETE')
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)
    provider = Provider()
    outcome = TaskOutcome.from_dict(agent_step(p, provider, instruction='Rewrite the sentence clearly.', workspace_path=str(tmp_path_factory)))
    if not child:
        # An ordinary main Agent may make a poor semantic choice. Runtime does
        # not pretend to prove necessity by adding a mandatory second judge;
        # it still preserves the real request and unresolved completion gate.
        assert outcome.status == OutcomeStatus.AWAITING_USER
        assert provider.stages == []
        assert p.pending_decision['question'] == 'Which layout?'
        assert not HostCompletionEvaluator.evaluate(p, 'Clear sentence.').allowed
        return
    assert outcome.status == OutcomeStatus.COMPLETED, outcome.to_dict()
    assert provider.stages == ['question_proposal']
    assert p.context_snapshot['alignment_runtime']['delivery']['source'] == 'host_admission_reuse'
    assert not p.pending_decision
    assert not any(e.get('event') == 'user_decision_requested' for e in p.session.host_ledger)
    result = next(m for m in p.session.messages if m.get('tool_call_id') == 'unnecessary' and m.get('message_type') == 'tool_result')
    assert 'no tool in this batch ran' in result['content']


def test_semantic_calls_keep_actual_usage_without_borrowing_native_conversation_state():
    from backend.core.loop.alignment_runtime import assess_with_provider
    class Provider:
        max_output_tokens = 32768
        def chat(self, messages, **kwargs):
            assert kwargs['preserve_provider_state'] is True
            assert kwargs['max_tokens'] == 8192
            assert len(messages) == 2 and not any(m.get('provider_state') for m in messages)
            return {'content': '{"status":"ready","interpretations":[],"questions":[]}',
                    'usage': {'input_tokens': 100, 'output_tokens': 400, 'reasoning_tokens': 300}}
    result = assess_with_provider(Provider(), {'request': 'Rewrite a sentence'})
    assert result['_provider_usage']['output_tokens'] == 400
    assert result['_provider_usage']['reasoning_tokens'] == 300


def test_plain_response_needs_no_independent_model_but_still_binds_completion(tmp_path_factory):
    from backend.core.loop.executor import agent_step
    from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
    from backend.core.loop.outcome import TaskOutcome, OutcomeStatus
    from backend.core.loop.tools import ToolRegistry
    p = process(tmp_path_factory)
    p.tool_registry = ToolRegistry([])
    class Provider:
        protocol = type('Protocol', (), {'value': 'openai_responses'})()
        capabilities = type('Capabilities', (), {'prompt_cache': 'automatic'})()
        context_window = 16384
        def assess_alignment(self, *args, **kwargs):
            raise AssertionError('Independent review must not run for a plain response')
        def stream_events(self, *args, **kwargs):
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text='Hello.')
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)
    events = []
    result = TaskOutcome.from_dict(agent_step(p, Provider(), instruction='Say hello.',
        workspace_path=str(tmp_path_factory), on_stream_event=events.append))
    assert result.status == OutcomeStatus.COMPLETED, result.to_dict()
    assert not any(e['event'] == 'alignment_assessment_started' for e in events)
    context = copy.deepcopy(p.context_snapshot)
    assert HostCompletionEvaluator.evaluate(p, result.response).allowed
    assert not HostCompletionEvaluator.evaluate(p, 'Different answer').allowed
    assert p.context_snapshot == context
    p.task_kind = 'plan'
    assert not HostCompletionEvaluator.evaluate(p, result.response).allowed


def test_actual_reply_reaches_main_model_before_combined_delivery_review(tmp_path_factory):
    from backend.core.loop.executor import agent_step
    from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
    from backend.core.loop.outcome import TaskOutcome, OutcomeStatus
    from backend.core.loop.tools import ToolRegistry
    p = process(tmp_path_factory)
    answer = 'Japan, April, two adults'
    workflow, request, node = pending_reply(p, answer)
    p.tool_registry = ToolRegistry([])
    p.session.messages.append({'role': 'user', 'content': answer, 'message_type': 'conversation'})
    candidate = 'April Japan itinerary for two adults'
    order = []

    class Provider:
        protocol = type('Protocol', (), {'value': 'openai_responses'})()
        capabilities = type('Capabilities', (), {'prompt_cache': 'automatic'})()
        context_window = 16384

        def assess_alignment(self, payload, **kwargs):
            order.append(payload['stage'])
            assert payload['stage'] == 'delivery'
            assert payload['candidate'] == candidate
            assert payload['actual_answers'][0]['answer'] == answer
            return {'status': 'ready', 'questions': [],
                'interpretations': [interpretation(node, request, answer)],
                'delivery_check': {'complete': True, 'evidence': [candidate]}}

        def stream_events(self, *args, **kwargs):
            order.append('main')
            assert not workflow.status()['complete']
            assert not confirmed_user_state(p.session.host_ledger)
            assert not HostCompletionEvaluator.evaluate(p, candidate).allowed
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=candidate)
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)

    result = TaskOutcome.from_dict(agent_step(p, Provider(), instruction=answer,
        workspace_path=str(tmp_path_factory)))
    assert result.status == OutcomeStatus.COMPLETED, result.to_dict()
    assert order == ['main', 'delivery']
    assert workflow.status()['complete']
    assert confirmed_user_state(p.session.host_ledger)[0]['value'] == answer
    assert HostCompletionEvaluator.evaluate(p, candidate).allowed
    p.session.host_ledger = [e for e in p.session.host_ledger if e['event'] != 'user_decision_received']
    assert not HostCompletionEvaluator.evaluate(p, candidate).allowed


def test_reviewed_contract_preview_matches_actual_publication_without_repeated_review(tmp_path_factory):
    import json
    from backend.core.loop.executor import agent_step
    from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
    from backend.core.loop.outcome import TaskOutcome, OutcomeStatus
    from backend.core.loop.tools import ToolRegistry
    p = process(tmp_path_factory)
    p.tool_registry = ToolRegistry(['declare_task_contract'])
    p.max_steps = 8
    proposal = {'goal': 'Explain the supplied design', 'execution_mode': 'answer',
        'deliverables': [{'kind': 'response', 'required': True}], 'estimated_complexity': 'bounded',
        'delegation_required': False, 'acceptance_criteria': ['Explain the supplied design'],
        'uncertainties': [], 'requires_user_decision': False, 'user_requested_delegation': False,
        'user_request_evidence': '', 'independent_workstreams': 1, 'routing_transition': 'keep_supervisor'}
    class Provider:
        protocol = type('Protocol', (), {'value': 'openai_responses'})()
        capabilities = type('Capabilities', (), {'prompt_cache': 'automatic'})()
        context_window = 16384
        calls = 0
        reviews = 0
        def assess_alignment(self, payload, **kwargs):
            self.reviews += 1
            assert payload['proposed_contracts'][0]['goal'] == proposal['goal']
            return {'status': 'ready', 'questions': [], 'interpretations': []}
        def stream_events(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ProviderEvent(ProviderEventType.TOOL_CALL_STARTED, tool_call_id='declare',
                    tool_name='declare_task_contract', output_index=0)
                yield ProviderEvent(ProviderEventType.TOOL_CALL_DONE, tool_call_id='declare',
                    tool_name='declare_task_contract', output_index=0, arguments=json.dumps(proposal))
            else:
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text='The supplied design.\nTASK_COMPLETE')
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)
    provider = Provider()
    result = TaskOutcome.from_dict(agent_step(p, provider, instruction='Explain the supplied design',
        workspace_path=str(tmp_path_factory)))
    assert result.status == OutcomeStatus.COMPLETED, result.to_dict()
    assert provider.reviews == 1
    receipt = next(m for m in p.session.messages if m.get('tool_call_id') == 'declare' and m.get('message_type') == 'tool_result')
    assert not receipt.get('is_error'), receipt
    assert p.context_snapshot['task_contract']['goal'] == proposal['goal']


def test_invalid_contract_is_reported_without_paid_semantic_review(tmp_path_factory):
    from backend.core.loop.executor import agent_step
    from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
    from backend.core.loop.outcome import TaskOutcome, OutcomeStatus
    from backend.core.loop.tools import ToolRegistry
    p = process(tmp_path_factory)
    p.tool_registry = ToolRegistry(['declare_task_contract'])
    class Provider:
        protocol = type('Protocol', (), {'value': 'openai_responses'})()
        capabilities = type('Capabilities', (), {'prompt_cache': 'automatic'})()
        context_window = 16384
        calls = 0
        def assess_alignment(self, *args, **kwargs):
            raise AssertionError('Invalid compilation must not create semantic work')
        def stream_events(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ProviderEvent(ProviderEventType.TOOL_CALL_STARTED, tool_call_id='invalid',
                    tool_name='declare_task_contract', output_index=0)
                yield ProviderEvent(ProviderEventType.TOOL_CALL_DONE, tool_call_id='invalid',
                    tool_name='declare_task_contract', output_index=0,
                    arguments='{"goal":"Explain the supplied design","execution_mode":"invalid"}')
            else:
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text='The supplied design.')
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)
    result = TaskOutcome.from_dict(agent_step(p, Provider(), instruction='Explain the supplied design',
        workspace_path=str(tmp_path_factory)))
    assert result.status == OutcomeStatus.COMPLETED, result.to_dict()
    invalid = next(m for m in p.session.messages if m.get('tool_call_id') == 'invalid' and m.get('message_type') == 'tool_result')
    assert invalid['is_error'] and 'INVALID_TOOL_ARGUMENTS' in str(invalid)
    assert not p.context_snapshot.get('task_contract')


def test_invalid_compiler_cannot_hide_other_effectful_calls_in_the_batch():
    from backend.core.loop.tool_boundary_preview import preview_tool_boundaries
    from backend.core.loop.agent_tool import AgentTool, ToolEffect
    def invalid(args):
        raise ValueError('invalid contract')
    tools = {'invalid_contract': AgentTool('invalid_contract', 'test', {}, lambda args: {},
        effect=ToolEffect.PROCESS, alignment_contract=invalid),
        'write': AgentTool('write', 'test', {}, lambda args: {}, effect=ToolEffect.WORKSPACE_WRITE)}
    calls = [{'name': name, 'args': {}} for name in tools]
    assert preview_tool_boundaries(calls[:1], tools).effectful is False
    assert preview_tool_boundaries(calls, tools).effectful is True


def test_plain_provider_stop_does_not_invent_a_semantic_review_or_question(tmp_path_factory):
    from backend.core.loop.alignment_review import AlignmentReview, AlignmentReviewHooks
    p = process(tmp_path_factory)
    events = []
    class Provider:
        def assess_alignment(self, payload, **kwargs):
            raise AssertionError('Host cannot infer question intent from ordinary response text')
    hooks = AlignmentReviewHooks(emit=lambda e, **kw: events.append(e), frontend_emit=None,
        notice=lambda *a, **kw: None, refresh_prompt=lambda: None, before_attempt=lambda *a: None,
        route_notice=lambda *a: None, result=lambda *a, **kw: kw, cancellation=lambda: {}, budget_exhausted=lambda e: {})
    provider = Provider()
    result = AlignmentReview(p, provider, hooks).check('Help clarify requirements', 'I will ask next; see the card below:')
    assert result is None
    assert not any(event['event'] == 'alignment_assessment_started' for event in events)
    assert not p.pending_decision
    # This receipt binds the actual candidate to the declared Host facts. It
    # certifies no semantic correctness and does not create a fictional card.
    assert p.context_snapshot['alignment_runtime']['delivery']['scope'] == 'no_declared_semantic_dependency'


def test_truncated_delivery_checks_and_persists_the_whole_public_answer(tmp_path_factory):
    from backend.core.loop.executor import agent_step
    from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
    from backend.core.loop.outcome import TaskOutcome, OutcomeStatus
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.answer_continuation import pending_answer_text
    p = process(tmp_path_factory)
    p.task_kind = 'answer'
    p.max_steps = 10
    p.tool_registry = ToolRegistry([])
    events = []
    class Provider:
        protocol = type('Protocol', (), {'value': 'openai_responses'})()
        capabilities = type('Capabilities', (), {'prompt_cache': 'automatic'})()
        context_window = 16384
        calls = 0
        def assess_alignment(self, payload, **kwargs):
            if payload['stage'] == 'delivery':
                assert payload['candidate'] == 'Modules. Data model. Tests.'
            return {'status': 'ready', 'questions': [], 'interpretations': []}
        def stream_events(self, *args, **kwargs):
            self.calls += 1
            yield ProviderEvent(ProviderEventType.REASONING_DELTA, reasoning='private analysis')
            yield ProviderEvent(ProviderEventType.TEXT_DELTA,
                text=['Modules. ', 'Data model. ', 'Tests.\nTASK_COMPLETE'][self.calls - 1])
            if self.calls < 3:
                yield ProviderEvent(ProviderEventType.RESPONSE_INCOMPLETE, artifact={'reason': 'max_output_tokens'})
            else:
                yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)
    result = TaskOutcome.from_dict(agent_step(p, Provider(), instruction='Provide modules, data and tests.',
        workspace_path=str(tmp_path_factory), on_stream_event=events.append))
    assert result.status == OutcomeStatus.COMPLETED, result.to_dict()
    assert result.response == 'Modules. Data model. Tests.'
    assert HostCompletionEvaluator.evaluate(p, result.response).allowed
    assert not HostCompletionEvaluator.evaluate(p, 'Tests.').allowed
    assert not pending_answer_text(p)
    starts = [e for e in events if e.get('event') == 'provider_request_started']
    assert [e['answer_continuation'] for e in starts] == [False, True, True]
    # Native provider replay retains actual turn segmentation and reasoning.
    assert [m['content'] for m in p.session.messages if m['role'] == 'assistant'] == [
        'Modules. ', 'Data model. ', 'Tests.\nTASK_COMPLETE']


def test_delivery_verdict_cannot_accept_a_partial_candidate_or_invent_evidence(tmp_path_factory):
    runtime = AlignmentRuntime(process(tmp_path_factory))
    payload = runtime.payload('Give the entire architecture proposal', candidate='Introduction. The first module is')
    proposal = {'status': 'ready', 'questions': [], 'interpretations': [],
        'delivery_check': {'complete': False, 'reason': 'Only an unfinished introduction is present', 'evidence': []}}
    assert runtime.apply(proposal, payload)['status'] == 'repair'
    proposal['delivery_check'] = {'complete': True, 'evidence': ['Modules, storage and validation are delivered.']}
    with pytest.raises(ValueError, match='actual candidate'):
        runtime.apply(proposal, payload)
    proposal['delivery_check'] = {'complete': True, 'evidence': []}
    with pytest.raises(ValueError, match='candidate evidence'):
        runtime.apply(proposal, payload)


def test_provider_delivery_requires_explicit_complete_verdict():
    import json
    from backend.core.loop.alignment_runtime import assess_with_provider
    class Provider:
        def chat(self, *args, **kwargs):
            return {'content': json.dumps({'status': 'ready', 'questions': [], 'interpretations': []}),
                    'usage': {'input_tokens': 10, 'output_tokens': 20}}
    result = assess_with_provider(Provider(), {'stage': 'delivery', 'candidate': 'Introduction'})
    assert result['schema_error'] == 'delivery_check_required'
    assert result['status'] != 'ready'
    assert result['_provider_usage']['output_tokens'] == 20


def test_valid_delivery_quotes_are_bounded_mechanically_without_changing_verdict(tmp_path_factory):
    runtime = AlignmentRuntime(process(tmp_path_factory))
    payload = runtime.payload('Give a proposal', candidate='Modules, storage and validation are delivered.')
    proposal = {'status': 'ready', 'questions': [], 'interpretations': [],
        'delivery_check': {'complete': True, 'evidence': ['Modules'] * 9}}
    assert runtime.apply(proposal, payload)['status'] == 'ready'
    # Even the quote beyond the retained audit sample must be validated.
    proposal['delivery_check']['evidence'][-1] = 'invented proof'
    with pytest.raises(ValueError, match='actual candidate'):
        runtime.apply(proposal, payload)


def test_delivery_references_preserve_source_and_reject_stale_blank_or_forged_refs(tmp_path_factory):
    from backend.core.loop.candidate_evidence import candidate_segments
    runtime = AlignmentRuntime(process(tmp_path_factory))
    candidate = '# Proposal\n' + '\n' * 2000 + 'Module and data. ' * 160 + '\nTests.'
    segments = candidate_segments(candidate)
    assert ''.join(part['text'] for part in segments) == candidate
    assert all(len(part['text']) <= 1000 for part in segments)
    assert len(candidate_segments('x\n' * 20000)) == 40
    payload = runtime.payload('Give a proposal', candidate=candidate)
    refs = [part['id'] for part in segments if part['text'].strip()]
    assessment = {'status': 'ready', 'questions': [], 'interpretations': [],
        'delivery_check': {'complete': True, 'evidence_refs': refs}}
    assert runtime.apply(assessment, payload)['status'] == 'ready'
    for invalid in ['invented', candidate_segments('Different proposal')[0]['id'],
                    next(part['id'] for part in segments if not part['text'].strip()), {}, False]:
        before = copy.deepcopy(runtime.process.context_snapshot)
        with pytest.raises(ValueError, match='actual nonblank'):
            runtime.apply({**assessment, 'delivery_check': {'complete': True, 'evidence_refs': [invalid]}}, payload)
        assert runtime.process.context_snapshot == before
    # A valid reference never excuses a forged legacy quote in the same proposal.
    with pytest.raises(ValueError, match='actual candidate'):
        runtime.apply({**assessment, 'delivery_check': {'complete': True, 'evidence_refs': refs,
            'evidence': ['Module ... Tests']}}, payload)


def test_delivery_wire_uses_source_segments_instead_of_duplicate_candidate_text():
    import json
    from backend.core.loop.alignment_runtime import assess_with_provider
    candidate = 'First paragraph.\n\nSecond paragraph.'
    class Provider:
        def chat(self, messages, **kwargs):
            wire = json.loads(messages[1]['content'])
            assert ''.join(p['text'] for p in wire['candidate']['segments']) == candidate
            reference = wire['candidate']['segments'][0]['id']
            return {'content': json.dumps({'status': 'ready', 'questions': [], 'interpretations': [],
                'request_frame': {'kind': 'deliverable', 'source_quote': 'Give a proposal', 'outcome': 'Proposal'},
                'delivery_check': {'complete': True, 'evidence_refs': [reference]}})}
    payload = {'request': 'Give a proposal', 'stage': 'delivery', 'candidate': candidate}
    result = assess_with_provider(Provider(), payload)
    assert result['delivery_check']['evidence_refs']
    assert payload['candidate'] == candidate


def test_truncated_semantic_response_keeps_usage_and_has_bounded_recovery_allowance():
    from backend.core.loop.alignment_runtime import assess_with_provider
    from backend.core.loop.provider_protocol import ProviderResponseIncomplete
    allocations = []
    class Provider:
        max_output_tokens = 32000
        def chat(self, *args, **kwargs):
            allocations.append(kwargs['max_tokens'])
            assert kwargs['max_retries'] == 0
            raise ProviderResponseIncomplete('max_output_tokens', {'input_tokens': 100, 'output_tokens': 8192})
    first = assess_with_provider(Provider(), {'stage': 'admission'})
    second = assess_with_provider(Provider(), {'stage': 'admission', 'provider_output_recovery': True})
    assert allocations == [8192, 16384]
    assert first['schema_error'] == second['schema_error'] == 'provider_response_incomplete'
    assert first['_provider_usage']['output_tokens'] == 8192


def test_completed_provider_fragment_is_repaired_before_task_finality(tmp_path_factory):
    from backend.core.loop.executor import agent_step
    from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
    from backend.core.loop.tools import ToolRegistry
    p = process(tmp_path_factory)
    p.task_kind = 'answer'
    p.max_steps = 10
    p.tool_registry = ToolRegistry([])
    workflow, request, node = pending_reply(p, 'Personal offline reader')
    workflow.resolve_answer(node, request['decision_id'], {
        'disposition': 'confirmed', 'value': 'Personal offline reader', 'quote': 'Personal offline reader'})
    partial = 'Introduction. The first module is'
    complete = 'Modules: loader and renderer. Data: immutable document. Tests: offline and read-only.'
    observed = []
    class Provider:
        protocol = type('Protocol', (), {'value': 'openai_responses'})()
        capabilities = type('Capabilities', (), {'prompt_cache': 'automatic'})()
        context_window = 16384
        calls = 0
        def assess_alignment(self, payload, **kwargs):
            result = {'status': 'ready', 'questions': [], 'interpretations': []}
            if payload['stage'] == 'delivery':
                candidate = payload['candidate']
                result['delivery_check'] = {'complete': candidate == complete,
                    'reason': 'The first attempt is an unfinished introduction', 'evidence': [candidate]}
            return result
        def stream_events(self, *args, **kwargs):
            self.calls += 1
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=partial if self.calls == 1 else complete)
            # The endpoint incorrectly labels a partial draft as completed.
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)
    provider = Provider()
    result = agent_step(p, provider, instruction='Provide the complete modules, data and tests proposal.',
                        workspace_path=str(tmp_path_factory), on_stream_event=observed.append)
    assert provider.calls == 2
    assert result['status'] == 'completed'
    assert result['response'] == complete
    assert partial not in result['response']
    assert any(e.get('event') == 'alignment_assessment_completed' and e.get('status') == 'repair' for e in observed)


def test_public_fragments_do_not_cross_task_or_process_ownership(tmp_path_factory):
    from backend.core.loop.answer_continuation import pending_answer_text, remember_answer_fragment
    p = process(tmp_path_factory)
    remember_answer_fragment(p, 'Old answer prefix')
    assert pending_answer_text(p) == 'Old answer prefix'
    child = process(tmp_path_factory)
    child.process_id = 'child'
    child.context_snapshot = copy.deepcopy(p.context_snapshot)
    assert not pending_answer_text(child)
    p.active_task_id = 'next-task'
    assert not pending_answer_text(p)


@pytest.mark.parametrize('empty', [None, {}, {'mode': '', 'decision_id': '', 'source_node_id': '', 'request': '', 'quote': ''}])
def test_nullable_scope_form_does_not_discard_real_answer_or_create_scope(tmp_path_factory, empty):
    p = process(tmp_path_factory)
    w, request, node = pending_reply(p, 'Japan')
    runtime = AlignmentRuntime(p)
    runtime.apply({'status': 'ready', 'questions': [], 'scope_update': empty,
        'interpretations': [interpretation(node, request, 'Japan')]}, runtime.payload(candidate='Japan itinerary'))
    assert w.status()['complete']
    assert not p.context_snapshot['alignment_runtime'].get('scope')
    assert HostCompletionEvaluator.evaluate(p, 'Japan itinerary').allowed
    assert not any(e.get('event') == 'alignment_scope_updated' for e in p.session.host_ledger)


@pytest.mark.parametrize('invalid', [{'mode': 'exploration'}, 'exploration', {'mode': '', 'request': 'Compare directions'}])
def test_substantive_incomplete_scope_is_still_rejected_atomically(tmp_path_factory, invalid):
    p = process(tmp_path_factory)
    _, request, node = pending_reply(p, 'Japan')
    runtime = AlignmentRuntime(p)
    payload = runtime.payload()
    before = copy.deepcopy(p.context_snapshot), copy.deepcopy(p.session.host_ledger)
    with pytest.raises(ValueError):
        runtime.apply({'status': 'ready', 'questions': [], 'scope_update': invalid,
            'interpretations': [interpretation(node, request, 'Japan')]}, payload)
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_partial_facet_cannot_be_duplicated_under_a_new_label(tmp_path_factory):
    p = process(tmp_path_factory)
    w, request, node = pending_reply(p, 'Japan with family')
    runtime = AlignmentRuntime(p)
    payload = runtime.payload()
    question = {'state_topic': 'departure_city', 'question': 'Which city do you depart from?',
                'why_user_must_decide': 'Changes transport', 'depends_on': []}
    proposal = {'status': 'needs_input', 'questions': [question],
        'interpretations': [interpretation(node, request, 'Japan with family', 'partial', 'Departure city')]}
    before = copy.deepcopy(p.context_snapshot), copy.deepcopy(p.session.host_ledger)
    with pytest.raises(ValueError, match='SAME node_id'):
        runtime.apply(proposal, payload)
    assert (p.context_snapshot, p.session.host_ledger) == before
    question['node_id'] = node
    result = runtime.apply(proposal, payload)
    assert len(w.status()['nodes']) == 1
    assert result['decision']['questions'][0]['node_id'] == node
    assert result['decision']['questions'][0]['question'] == 'Which city do you depart from?'


def test_same_question_identity_cannot_appear_twice_with_different_labels(tmp_path_factory):
    p = process(tmp_path_factory)
    _, _, node = pending_reply(p, 'Japan')
    runtime = AlignmentRuntime(p)
    question = {'node_id': node, 'state_topic': 'departure', 'question': 'From which city?',
                'why_user_must_decide': 'Changes transport', 'depends_on': []}
    with pytest.raises(ValueError, match='only be asked once'):
        runtime.apply({'status': 'needs_input', 'interpretations': [],
            'questions': [question, {**question, 'state_topic': 'origin'}]}, runtime.payload())


def test_mixed_known_facts_and_delegation_cannot_overwrite_same_node_twice(tmp_path_factory):
    p = process(tmp_path_factory)
    text = 'Japan; choose remaining details yourself, do not ask.'
    w, request, node = pending_reply(p, text)
    runtime = AlignmentRuntime(p)
    payload = runtime.payload()
    before = copy.deepcopy(p.context_snapshot), copy.deepcopy(p.session.host_ledger)
    with pytest.raises(ValueError, match='ONE interpretation'):
        runtime.apply({'status': 'ready', 'questions': [], 'interpretations': [
            interpretation(node, request, 'Japan'),
            interpretation(node, request, text, 'delegated')]}, payload)
    assert (p.context_snapshot, p.session.host_ledger) == before
    result = runtime.apply({'status': 'ready', 'questions': [], 'interpretations': [
        {'node_id': node, 'decision_id': request['decision_id'], 'disposition': 'delegated',
         'quote': text, 'value': 'Destination is Japan; remaining choices delegated, not user-selected facts.'}]},
        runtime.payload(candidate='Japan plan with labelled assistant choices'))
    assert result['status'] == 'ready'
    assert w.status()['complete']
    assert HostCompletionEvaluator.evaluate(p, 'Japan plan with labelled assistant choices').allowed
    p.session.host_ledger = [e for e in p.session.host_ledger if e.get('event') != 'user_decision_received']
    assert not HostCompletionEvaluator.evaluate(p, 'Japan plan with labelled assistant choices').allowed


def test_observation_boundary_does_not_demand_delivery_but_effects_and_delivery_still_review(tmp_path_factory):
    from backend.core.loop.alignment_routing import review_reason
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload('Inspect prerequisites and report')
    EngineeringWorkflow(p).configure({'profiles': ['retrospective']})
    payload = runtime.payload()
    assert payload['stage'] == 'admission'
    assert review_reason(p, payload) == ''
    assert review_reason(p, payload, 'contract') == 'declared_task_boundary'
    assert review_reason(p, payload, 'execution') == 'effect_boundary'
    assert review_reason(p, runtime.payload(candidate='Report')) == 'declared_workflow_dependencies'
    assert not HostCompletionEvaluator.evaluate(p, 'Report').allowed


@pytest.mark.parametrize('stage', ['admission', 'question_proposal'])
def test_delivery_verdict_cannot_be_applied_at_another_stage_atomically(tmp_path_factory, stage):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    payload = runtime.payload(proposed_questions=[] if stage == 'question_proposal' else None)
    before = copy.deepcopy(p.context_snapshot)
    with pytest.raises(ValueError, match='only valid at delivery'):
        runtime.apply({'status': 'repair', 'interpretations': [], 'questions': [],
                       'delivery_check': {'complete': False, 'reason': 'No final answer yet'}}, payload)
    assert p.context_snapshot == before
    assert not p.pending_decision


def test_incomplete_delivery_can_pause_for_a_valid_user_choice_without_certifying_delivery(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload('Plan a trip')
    payload = runtime.payload(candidate='I need your destination before choosing a route.')
    result = runtime.apply({'status': 'needs_input', 'interpretations': [],
        'questions': [{'state_topic': 'destination', 'question': 'Which destination?',
                       'why_user_must_decide': 'Determines the route'}],
        'delivery_check': {'complete': False, 'reason': 'Destination is unresolved'}}, payload)
    assert result['status'] == 'needs_input'
    assert result['decision']['decision_id'] == p.pending_decision['decision_id']
    assert not p.context_snapshot['alignment_runtime'].get('delivery')
    assert not HostCompletionEvaluator.evaluate(p, payload['candidate']).allowed


def test_boundary_repairs_do_not_consume_delivery_repair_budget_and_keep_reason(tmp_path_factory):
    from backend.core.loop.agent_tool import AgentTool, ToolEffect
    from backend.core.loop.execution_contract import data_broker
    from backend.core.loop.tool_proposal_runtime import ToolProposalRuntime
    p = process(tmp_path_factory)
    p._nudge_counters['alignment_repair'] = 2
    tool = AgentTool(execution_contract=data_broker('test.fixture'), name='observe',
                     description='Read', parameters={'type': 'object'}, effect=ToolEffect.READ,
                     execute=lambda _args: {})
    review = ToolProposalRuntime(p, p.session, lambda: {'observe': tool},
                                 lambda **_kwargs: {'alignment_repair': 'Proposed scope contradicts the request'})
    assert review.review([{'name': 'observe', 'args': {}, 'id': 'one'}], '').retry
    assert review.review([{'name': 'observe', 'args': {}, 'id': 'two'}], '').retry
    result = review.review([{'name': 'observe', 'args': {}, 'id': 'three'}], '').result
    assert result['error']['code'] == 'ALIGNMENT_BOUNDARY_REJECTED'
    assert result['error']['message'] == 'Proposed scope contradicts the request'
    assert p._nudge_counters['alignment_repair'] == 2


def test_stage_confusion_uses_existing_bounded_compiler_recovery(tmp_path_factory):
    from types import SimpleNamespace
    from backend.core.loop.alignment_review import AlignmentReview, AlignmentReviewHooks
    p = process(tmp_path_factory)
    AlignmentRuntime(p).payload('Inspect and report')
    events = []
    calls = []
    def assess(payload, **_kwargs):
        calls.append(payload)
        result = {'status': 'ready', 'interpretations': [], 'questions': []}
        if len(calls) == 1:
            result.update(status='repair', delivery_check={'complete': False, 'reason': 'No final answer'})
        return result
    noop = lambda *_args, **_kwargs: None
    hooks = AlignmentReviewHooks(emit=lambda event, **_kwargs: events.append(event),
        frontend_emit=None, notice=noop, refresh_prompt=noop, before_attempt=noop,
        route_notice=noop, result=noop, cancellation=noop, budget_exhausted=noop)
    result = AlignmentReview(p, SimpleNamespace(assess_alignment=assess), hooks).check(
        'Inspect and report', trigger='contract', proposed_contracts=[{'goal': 'Inspect and report'}])
    assert result is None
    assert len(calls) == 2
    assert 'only valid at delivery' in calls[1]['host_validation_error']
    assert [e['status'] for e in events if e['event'] == 'alignment_assessment_completed'] == ['ready']
    assert any(e['event'] == 'alignment_assessment_rejected' for e in events)


def test_review_selected_question_preserves_options_and_is_a_real_broker_card(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    request = {'kind': 'choice', 'question': 'Chinese or English?',
               'why_user_must_decide': 'This determines the report language',
               'allow_free_form': False,
               'options': [{'label': label, 'principle': label, 'immediate_effect': label,
                            'downstream_effect': label, 'risks': 'None', 'reversibility': 'Can change later'}
                           for label in ['Chinese', 'English']]}
    payload = runtime.payload('Choose report language', proposed_questions=[request])
    question = {'proposal_index': 0, 'state_topic': 'report_language', 'question': 'Which report language?',
                'why_user_must_decide': request['why_user_must_decide']}
    result = runtime.apply({'status': 'needs_input', 'interpretations': [], 'questions': [question]}, payload)
    actual = result['decision']['questions'][0]
    assert actual['question'] == request['question']
    assert actual['options'] == request['options']
    assert actual['allow_free_form'] is True  # user can reject every semantic option
    assert p.pending_decision['decision_id'] == result['decision']['decision_id']
    assert any(e.get('event') == 'user_decision_requested' for e in p.session.host_ledger)


@pytest.mark.parametrize('index', [-1, 2, True, '0'])
def test_question_proposal_reference_cannot_select_foreign_or_coerced_input(tmp_path_factory, index):
    from backend.core.loop.alignment_question import question_request
    with pytest.raises(ValueError, match='actual proposed'):
        question_request({'proposal_index': index}, {'stage': 'question_proposal', 'proposed_questions': [{}]})


def test_discussion_delivery_keeps_original_choice_open_without_republishing(tmp_path_factory):
    p=process(tmp_path_factory)
    p.task_kind='answer'
    runtime=AlignmentRuntime(p)
    runtime.payload('Rewrite an introduction')
    answer='Discuss the scientific introduction for beginners, its origins and current value.'
    w,request,node=pending_reply(p,answer)
    candidate='Here are the origins, development and current value for the beginner introduction.'
    payload=runtime.payload(candidate=candidate)
    assessment={'status':'ready','questions':[],
        'request_frame':{'kind':'deliverable','source_quote':answer,'outcome':'Discuss the introduction before choosing'},
        'interpretations':[interpretation(node,request,answer,disposition='deferred',remaining='Original choice open')],
        'delivery_check':{'complete':True,'evidence':[candidate]}}
    with pytest.raises(ValueError,match='scope_update'):
        runtime.apply(assessment,payload)
    assert p.pending_decision is None
    assessment['scope_update']={'mode':'exploration','decision_id':request['decision_id'],'quote':answer,'request':answer}
    count=len([e for e in p.session.host_ledger if e.get('event')=='user_decision_requested'])
    assert runtime.apply(assessment,payload)['status']=='ready'
    assert p.pending_decision is None
    assert not w.status()['complete']
    assert HostCompletionEvaluator.evaluate(p,candidate).allowed
    assert len([e for e in p.session.host_ledger if e.get('event')=='user_decision_requested'])==count
