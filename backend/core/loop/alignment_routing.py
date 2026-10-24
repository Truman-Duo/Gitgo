"""Host decides whether a semantic assessment has new work to do."""
from .alignment_evidence import alignment_input_digest, answer_digest

POLICY_VERSION = 6
REVIEW_POLICY = f'on_demand_v{POLICY_VERSION}'


def uses_on_demand_review(state):
    """Keep persisted v2 tasks subject to the same boundary guards."""
    return state.get('review_policy') in {'on_demand_v2', 'on_demand_v3', 'on_demand_v4', 'on_demand_v5', REVIEW_POLICY}


def main_model_initial_question(process, payload):
    """No second semantic judge for an ordinary, undeclared first question.

    The tool still validates identity, request shape and publishes a real gate.
    This is state-based routing, not proof that the proposed question is useful.
    """
    context, _ = process.read_context_snapshot()
    contract = context.get('task_contract') or {}
    return (process.task_kind == 'answer' and process.actor_kind == 'supervisor' and not process.parent_id
            and not complex_delivery(process, payload)
            and not payload.get('workflow', {}).get('nodes')
            and not any(payload.get(k) for k in ('actual_answers', 'confirmed_answers', 'user_updates', 'questions_asked'))
            and not contract.get('requires_user_decision') and not contract.get('uncertainties'))


def complex_delivery(process, payload):
    context, _ = process.read_context_snapshot()
    contract = context.get('task_contract') or {}
    return (process.task_kind in {'plan', 'review'}
            or contract.get('estimated_complexity') in {'moderate', 'high'}
            or contract.get('execution_mode') in {'plan', 'review'}
            or int(contract.get('independent_workstreams') or 1) > 1)


def review_reason(process, payload, trigger='admission'):
    """Route from actual Host state, never from user keywords or answer length.

    Plain messages belong to the main model. An independent semantic review is
    reserved for a proposed question, a declared task/effect boundary, changed
    requirements, or a complex delivery. It grants no tool authority.
    """
    if trigger == 'question_proposal' or payload.get('proposed_questions') is not None:
        return '' if main_model_initial_question(process, payload) else 'question_necessity'
    if trigger in {'contract', 'execution'}:
        return 'declared_task_boundary' if trigger == 'contract' else 'effect_boundary'
    if payload.get('stage') == 'admission' and payload.get('actual_answers'):
        # Let the main Agent use the actual reply first. Interpretation and
        # adoption can be compiled together at delivery. A proposed new question,
        # changed contract or effect boundary above still reviews before acting;
        # unresolved receipt gates have not been discharged by this deferral.
        return ''
    if payload.get('actual_answers') or payload.get('confirmed_answers'):
        return 'actual_answer_dependencies'
    if payload.get('user_updates'):
        return 'user_requirement_update'
    if payload.get('workflow', {}).get('nodes') and payload.get('stage') == 'delivery':
        return 'declared_workflow_dependencies'
    # Reading evidence is not a new proposal or a delivery attempt. Workflow
    # revisions still invalidate cached readiness, but do not turn each status
    # read into a demand for a final answer. Contract/effect/question boundaries
    # above and the mechanical workflow gates remain independently enforced.
    context, _ = process.read_context_snapshot()
    contract = context.get('task_contract') or {}
    if contract.get('requires_user_decision') or contract.get('uncertainties'):
        return 'declared_uncertainty'
    if payload.get('stage') == 'delivery' and complex_delivery(process, payload):
        return 'complex_delivery'
    return ''


def reuse_assessment(process, payload):
    context, _ = process.read_context_snapshot()
    state = context.get("alignment_runtime") or {}
    ready = state.get("admission_review") or {}
    declarations = payload.get('proposed_contracts') or []
    current = alignment_input_digest(process, contract_override=declarations[-1] if declarations else None)
    if ready.get("input_digest") != current or ready.get("status") != "ready":
        return ""
    if payload["stage"] == "admission":
        return "unchanged_admission"
    if (payload["stage"] == "delivery" and not complex_delivery(process, payload)
            and not payload.get("actual_answers") and not payload.get("confirmed_answers")
            and not payload.get("user_updates") and not payload["workflow"].get("nodes")):
        # No new requirement or decision needs an interview classifier, even
        # after tool use. Technical correctness still goes through Host gates;
        # this certificate only reuses the readiness of unchanged requirements.
        process.update_context_snapshot(lambda ctx: {**ctx, "alignment_runtime": {
            **ctx.get("alignment_runtime", {}), "delivery": {
                "candidate_digest": answer_digest(payload["candidate"]), "input_digest": current,
                "source": "host_admission_reuse", "scope": "requirement_readiness_only"}}})
        return "no_new_requirement_dependencies"
    return ""
