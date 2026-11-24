from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.alignment_routing import reuse_assessment, review_reason


def ready_process():
    process = AgentProcess("p", "worker", RingLevel.RING_3, active_task_id="task", session=AgentSession())
    runtime = AlignmentRuntime(process)
    payload = runtime.payload("Write a specified read-only report")
    runtime.apply({"status": "ready", "questions": [], "interpretations": []}, payload)
    return process, runtime


def test_host_reuses_unchanged_admission_and_plain_read_only_delivery():
    process, runtime = ready_process()
    assert reuse_assessment(process, runtime.payload()) == "unchanged_admission"
    assert runtime.payload()["questions_asked"] == []
    assert runtime.payload()["historical_decisions"] == []
    assert reuse_assessment(process, runtime.payload(candidate="Report")) == "no_new_requirement_dependencies"


def test_control_answer_does_not_trigger_another_interview():
    process, runtime = ready_process()
    process.session.host_ledger.extend([
        {"event": "user_decision_requested", "kind": "permission", "task_id": "task", "decision_id": "control"},
        {"event": "user_decision_received", "task_id": "task", "decision_id": "control"}])
    assert reuse_assessment(process, runtime.payload()) == "unchanged_admission"


def test_tools_reuse_requirement_readiness_but_material_changes_are_not_skipped():
    process, runtime = ready_process()
    process.task_kind = "action"
    assert reuse_assessment(process, runtime.payload(candidate="Done")) == "no_new_requirement_dependencies"
    process.task_kind = "answer"
    process.task_constraints.append("new requirement")
    assert reuse_assessment(process, runtime.payload()) == ""
    process.task_constraints.clear()
    process.session.host_ledger.append({"event": "user_decision_received", "task_id": "task", "decision_id": "actual", "answer": "new domain"})
    assert reuse_assessment(process, runtime.payload()) == ""


def test_complex_delivery_and_question_proposals_do_not_reuse_readiness():
    process, runtime = ready_process()
    process.task_kind = 'plan'
    assert review_reason(process, runtime.payload(candidate='Plan')) == 'complex_delivery'
    assert reuse_assessment(process, runtime.payload(candidate='Plan')) == ''
    assert review_reason(process, runtime.payload(proposed_questions=[])) == 'question_necessity'


def test_semantic_contract_changes_invalidate_certificate_but_bookkeeping_does_not():
    process, runtime = ready_process()
    from backend.core.loop.alignment_evidence import alignment_input_digest
    before = alignment_input_digest(process)
    process.update_context_snapshot(lambda ctx: {**ctx, 'task_contract': {'lease_revision': 17}})
    assert alignment_input_digest(process) == before
    process.update_context_snapshot(lambda ctx: {**ctx, 'task_contract': {'goal': 'A different deliverable'}})
    assert alignment_input_digest(process) != before
    assert reuse_assessment(process, runtime.payload()) == ''


def test_actual_reply_is_interpreted_with_delivery_without_disabling_boundary_review():
    process, runtime = ready_process()
    payload = {**runtime.payload(), 'actual_answers': [{'node_id': 'q', 'answer': 'Choose remaining details'}]}
    assert review_reason(process, payload) == ''
    assert review_reason(process, {**payload, 'stage': 'delivery', 'candidate': 'Plan'}) == 'actual_answer_dependencies'
    assert review_reason(process, payload, 'question_proposal') == 'question_necessity'
    assert review_reason(process, payload, 'execution') == 'effect_boundary'
    assert review_reason(process, payload, 'contract') == 'declared_task_boundary'


def test_confirmed_answers_and_updates_do_not_trigger_review_on_ordinary_reads():
    process, runtime = ready_process()
    payload = {**runtime.payload(), 'confirmed_answers': [{'node_id': 'q', 'answer': 'Known fact'}],
        'user_updates': ['Apply the related consumer too']}
    assert review_reason(process, payload) == ''
    assert review_reason(process, payload, 'contract') == 'declared_task_boundary'
    assert review_reason(process, payload, 'execution') == 'effect_boundary'
    assert review_reason(process, payload, 'question_proposal') == 'question_necessity'
    assert review_reason(process, {**payload, 'stage': 'delivery', 'candidate': 'Plan'}) == 'actual_answer_dependencies'


def test_main_agent_owns_first_ordinary_question_but_not_declared_dependency_review():
    process, runtime = ready_process()
    process.actor_kind = 'supervisor'
    process.role = 'supervisor'
    process.task_kind = 'answer'
    payload = runtime.payload(proposed_questions=[{'question': 'Purpose?'}])
    assert review_reason(process, payload) == ''
    group = {**payload, 'proposed_questions': [{'question': 'Purpose?'}, {'question': 'Audience?'}]}
    assert review_reason(process, group) == 'question_necessity'
    assert review_reason(process, {**payload, 'proposed_questions': [
        {'question': 'Audience?', 'depends_on_topics': ['purpose']} ]}) == 'question_necessity'
    assert review_reason(process, {**payload, 'workflow': {'nodes': [{'id': 'existing'}]}}) == 'question_necessity'
    assert review_reason(process, {**payload, 'actual_answers': [{'answer': 'Partial'}]}) == 'question_necessity'
    process.task_kind = 'plan'
    assert review_reason(process, payload) == 'question_necessity'
    process.task_kind = 'answer'
    process.update_context_snapshot(lambda ctx: {**ctx, 'task_contract': {'requires_user_decision': True}})
    assert review_reason(process, payload) == 'question_necessity'


def test_plain_question_hint_reuses_semantic_review_but_never_read_admission():
    from backend.core.loop.alignment_routing import candidate_question_hint
    process, runtime = ready_process()
    payload = runtime.payload(candidate='Which source record can you check?')
    assert review_reason(process, payload) == 'unstructured_question_candidate'
    assert reuse_assessment(process, payload) == ''
    assert review_reason(process, runtime.payload()) == ''
    assert not candidate_question_hint('```python\nquestion = "?"\n```\n> A quoted question?\nDone.')
    assert not candidate_question_hint('~~~\nquery?\n~~~\nDone.')
    # Syntax deliberately cannot decide teaching or whether an answer blocks.
    assert candidate_question_hint('What is 2 + 2?')
    assert candidate_question_hint('请核对哪一份记录？')
    assert not candidate_question_hint('Tell me which source to use')


def test_same_session_business_review_survives_task_change_without_inheriting_authority():
    from types import SimpleNamespace
    session = AgentSession()
    previous = AgentProcess('old', 'supervisor', RingLevel.RING_0, active_task_id='old-task', session=session)
    session.host_ledger.append({'event': 'user_decision_requested', 'kind': 'clarification',
        'task_id': 'old-task', 'decision_id': 'real-question', 'question':'Which rule?', 'state_topic':'rule'})
    session.host_ledger.append({'event':'user_decision_received','task_id':'old-task',
        'decision_id':'real-question','answer':'Keep the exact half-hour rule.'})
    previous.update_context_snapshot(lambda ctx: {**ctx, 'task_contract': {'goal': 'Old goal'},
        'alignment_runtime': {'request_frame': {'kind': 'deliverable'}}})
    processes = {previous.process_id: previous}

    def next_process(identifier, predecessor):
        result = AgentProcess(identifier, 'supervisor', RingLevel.RING_0, active_task_id=identifier,
            task_description='Summarize the agreed rules.', session=session)
        result.runtime_preferences['predecessor_process_id'] = predecessor.process_id
        result._manager = SimpleNamespace(get=processes.get, children_of=lambda _id: [])
        processes[identifier] = result
        return result

    successor = next_process('new', previous)
    runtime = AlignmentRuntime(successor)
    candidate = 'Only the actual confirmed condition.'
    payload = runtime.payload(candidate=candidate)
    assert payload['continuation_review']['source_task_id'] == 'old-task'
    assert 'task_contract' not in successor.context_snapshot
    assert payload['actual_answers'] == []
    assert payload['continuation_answers'][0]['answer'] == 'Keep the exact half-hour rule.'
    assert payload['continuation_answers'][0]['task_id'] == 'old-task'
    assert review_reason(successor, payload) == 'business_dialogue_continuation'
    assert reuse_assessment(successor, payload) == ''
    assessment = {'status': 'ready', 'questions': [], 'interpretations': [],
        'request_frame': {'kind': 'deliverable', 'source_quote': 'Summarize', 'outcome': 'Rule summary'},
        'delivery_check': {'complete': True, 'evidence': [candidate]}, 'continuation_relevant': True}
    runtime.apply(assessment, payload)
    later = next_process('later', successor)
    later_runtime = AlignmentRuntime(later)
    later_payload = later_runtime.payload(candidate=candidate)
    assert later_payload['continuation_review']['source_task_id'] == 'old-task'
    later_runtime.apply({**assessment, 'continuation_relevant': False}, later_payload)
    unrelated = next_process('unrelated', later)
    assert 'continuation_review' not in AlignmentRuntime(unrelated).payload(candidate=candidate)


def test_production_assessor_retains_first_structural_failure():
    from types import SimpleNamespace
    from backend.core.loop.alignment_runtime import assess_with_provider
    provider = SimpleNamespace(chat=lambda *a, **kw: {'content': 'not json'})
    result = assess_with_provider(provider, {'stage': 'delivery', 'candidate': 'Done'})
    assert result['schema_error'] == 'invalid_json'
