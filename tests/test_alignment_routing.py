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


def test_main_agent_owns_first_ordinary_question_but_not_declared_dependency_review():
    process, runtime = ready_process()
    process.actor_kind = 'supervisor'
    process.role = 'supervisor'
    process.task_kind = 'answer'
    payload = runtime.payload(proposed_questions=[{'question': 'Purpose?'}])
    assert review_reason(process, payload) == ''
    assert review_reason(process, {**payload, 'workflow': {'nodes': [{'id': 'existing'}]}}) == 'question_necessity'
    assert review_reason(process, {**payload, 'actual_answers': [{'answer': 'Partial'}]}) == 'question_necessity'
    process.task_kind = 'plan'
    assert review_reason(process, payload) == 'question_necessity'
    process.task_kind = 'answer'
    process.update_context_snapshot(lambda ctx: {**ctx, 'task_contract': {'requires_user_decision': True}})
    assert review_reason(process, payload) == 'question_necessity'
