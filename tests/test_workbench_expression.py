"""Current-source comparison is available without a notebook or second judge."""
from copy import deepcopy
import json

import pytest

from backend.core.loop.dialogue_context import MAX_VIEW_BYTES
from backend.core.loop.recovery_guidance import lookup_guidance
from backend.core.loop.main_dialogue import activate, apply_answer_updates
from backend.core.loop.models import ProcessStatus
from backend.core.loop.protocol_tools.registry import build_internal_tools
from backend.core.loop.protocol_tools.workbench import invoke
from backend.core.workbench.expression import inspect_delivery
from tests.test_alignment_semantics import process, pending_reply


def source_case(root, quote="Release the held resource after twenty minutes", value=None):
    p = process(root)
    p.status = ProcessStatus.RUNNING
    w, card, node = pending_reply(p, quote)
    activate(p)
    apply_answer_updates(p, [{"state_topic": "trip", "disposition": "confirmed",
                             "quote": quote, "value": value or quote}])
    # Reconstruct the preserved v1 unstructured response contract. Modern
    # completions establish their explicit components in the dedicated tests.
    apply_answer_updates(p, [], {'mode': 'exploration', 'quote': quote[:2000]})
    return p, w, card, node


def test_inspection_preserves_original_evidence_beside_fallible_interpretation(tmp_path_factory):
    p, _, _, _ = source_case(tmp_path_factory, value="Release the resource and cancel the request")
    before = deepcopy((p.context_snapshot, p.session.host_ledger, p.session.messages))
    draft = "The resource is released and the request is cancelled."
    result = invoke({"operation": "inspect_delivery", "candidate": draft}, process=p)
    decision = result["dialogue_evidence"]["current_decisions"][0]
    assert decision["source_quote"] == "Release the held resource after twenty minutes"
    assert 'interpreted_value' not in decision and decision["value_origin"] == "user_quote"
    proposal = result['dialogue_evidence']['interpretation_proposals'][0]
    assert proposal['interpretation'] == "Release the resource and cancel the request"
    assert proposal['status'] == 'unverified_agent_proposal'
    assert result["semantic_verdict"] == "not_assessed" and not result["authority_granted"]
    assert result["status"] == "evidence_ready_for_agent_comparison"
    assert result["candidate_chars"] == len(draft) and "candidate" not in result
    assert (p.context_snapshot["engineering_workflow"], p.context_snapshot["alignment_runtime"],
            p.session.host_ledger, p.session.messages) == (before[0]["engineering_workflow"],
            before[0]["alignment_runtime"], before[1], before[2])
    assert "Release the held resource after twenty minutes" in result["confirmed_excerpt"]
    assert "cancel the request" not in result["confirmed_excerpt"]
    assert not p.approval_grants


def test_formal_tool_route_needs_only_candidate_and_no_bound_sheet_storage(tmp_path_factory):
    p, _, _, _ = source_case(tmp_path_factory)
    assert p.bound_storage is None
    tool = build_internal_tools(p, {}, str(tmp_path_factory), None, None, None)["workbench"]
    result = tool.execute({"operation": "inspect_delivery", "candidate": "The resource is released."})
    assert result["dialogue_evidence"]["current_decisions"][0]["disposition"] == "confirmed"
    assert result["process_id"] == p.process_id and result["task_id"] == p.active_task_id
    assert "inspect_delivery" in tool.parameters["properties"]["operation"]["enum"]


@pytest.mark.parametrize("invalidate", ["cancel", "permission", "foreign-task", "changed-answer"])
def test_invalidated_or_control_input_is_not_reissued_as_confirmed(tmp_path_factory, invalidate):
    p, _, card, _ = source_case(tmp_path_factory)
    if invalidate == "cancel":
        p.session.host_ledger.append({"event": "user_decision_cancelled", "task_id": p.active_task_id,
                                      "decision_id": card["decision_id"]})
    elif invalidate == "permission":
        next(e for e in p.session.host_ledger if e["event"] == "user_decision_requested")["kind"] = "permission"
    else:
        reply = next(e for e in p.session.host_ledger if e["event"] == "user_decision_received")
        reply["task_id" if invalidate == "foreign-task" else "answer"] = "different-source"
    result = inspect_delivery(p, "An unchanged draft")
    assert not result["dialogue_evidence"]["current_decisions"]
    assert result["semantic_verdict"] == "not_assessed"
    from backend.core.loop.decision_support import confirmed_user_state
    assert not confirmed_user_state(p.session.host_ledger, task_id=p.active_task_id)


def test_actual_confirmed_section_cannot_include_an_agent_added_consequence(tmp_path_factory):
    from backend.core.loop.completion_protocol import HostCompletionEvaluator
    p, _, _, _ = source_case(tmp_path_factory, value="Release the resource and cancel the request")
    draft = "## Agreed\n\nRelease the resource and cancel the request.\n\n## Advice\n\nA proposal."
    result = inspect_delivery(p, draft)
    assert result["excerpt_status"] == "replace_confirmed_section"
    rejected = HostCompletionEvaluator.evaluate(p, draft)
    assert not rejected.allowed
    assert rejected.workbench_checks[0]["diagnostic_code"] == "WORKBENCH_EXPRESSION_EXCERPT_CHANGED"
    assert any(g["catalog_id"] == "GITGO-E7609" for g in rejected.gates)
    corrected = result["confirmed_excerpt"] + "\n\n## Advice\n\nCancellation would be a separate proposal."
    evaluation = HostCompletionEvaluator.evaluate(p, corrected)
    assert evaluation.allowed and evaluation.workbench_checks[0]["status"] == "match"
    assert evaluation.workbench_checks[0]["semantic_verdict"] == "not_assessed"
    assert not HostCompletionEvaluator.evaluate(p, corrected + "\n\n" + result["confirmed_excerpt"]).allowed


def test_new_dialogue_evidence_invalidates_the_bound_excerpt(tmp_path_factory):
    from backend.core.loop.completion_protocol import HostCompletionEvaluator
    from tests.test_dynamic_alignment import question
    p, w, _, _ = source_case(tmp_path_factory)
    original = inspect_delivery(p, "## Agreed\n\nDraft")["confirmed_excerpt"]
    card = w.ask_semantic(question("next_rule"))
    p.session.host_ledger.append({"event": "user_decision_received", "task_id": p.active_task_id,
        "decision_id": card["decision_id"], "answer": "The timeout is now forty minutes"})
    p.pending_decision = None
    evaluation = HostCompletionEvaluator.evaluate(p, original)
    assert not evaluation.allowed
    assert evaluation.workbench_checks[0]["diagnostic_code"] == "WORKBENCH_EXPRESSION_EVIDENCE_STALE"


def test_new_user_facts_can_be_bound_before_preparing_the_excerpt(tmp_path_factory):
    p = process(tmp_path_factory)
    p.status = ProcessStatus.RUNNING
    pending_reply(p, "The audience is internal employees")
    activate(p)
    result = inspect_delivery(p, "## Agreed\n\nDraft", answer_updates=[{
        "state_topic": "trip", "disposition": "confirmed", "quote": "The audience is internal employees",
        "value": "Internal employees"}])
    assert "The audience is internal employees" in result["confirmed_excerpt"]
    assert result["excerpt_status"] == "replace_confirmed_section"
    from backend.core.loop.decision_support import confirmed_user_state
    current = confirmed_user_state(p.session.host_ledger, task_id=p.active_task_id)[0]
    assert current['value'] == 'The audience is internal employees'
    assert current['agent_interpretation'] == 'Internal employees'


def test_undecided_choices_and_assistant_proposals_keep_their_roles(tmp_path_factory):
    p = process(tmp_path_factory)
    _, _, _ = pending_reply(p, "Audience is not decided yet")
    activate(p)
    apply_answer_updates(p, [{"state_topic": "trip", "disposition": "deferred",
                             "quote": "Audience is not decided yet", "remaining": "Choose the audience"}])
    p.update_context_snapshot(lambda c: {**c, "alignment_runtime": {**c["alignment_runtime"],
        "assumptions": {"delivery": {"value": "email"}}}})
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    result = inspect_delivery(p, "I recommend email; audience remains open.")
    evidence = result["dialogue_evidence"]
    assert evidence["unresolved_decisions"][0]["interpretation"]["disposition"] == "deferred"
    assert evidence["assistant_proposals_not_user_acceptance"][0]["value"] == "email"
    assert not evidence["current_decisions"]
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_wrong_owner_cannot_inspect_copied_parent_dialogue(tmp_path_factory):
    p, _, _, _ = source_case(tmp_path_factory)
    p.process_id = "child"
    p.parent_id = "p"
    result = inspect_delivery(p, "Draft")
    assert result["error"] == "WORKBENCH_EXPRESSION_SCOPE_UNAVAILABLE"
    assert result["error_info"]["catalog_id"] == "GITGO-E7607"
    assert "dialogue_evidence" not in result


def test_changed_sources_issue_a_specific_error_without_a_partial_view(tmp_path_factory, monkeypatch):
    import backend.core.workbench.expression as expression
    p, _, _, _ = source_case(tmp_path_factory)
    original = expression.dialogue_view

    def changed(current, **kwargs):
        view = original(current, **kwargs)
        current.update_context_snapshot(lambda c: {**c, "new_input_marker": True})
        return view

    monkeypatch.setattr(expression, "dialogue_view", changed)
    result = inspect_delivery(p, "Draft")
    assert result["error"] == "WORKBENCH_EXPRESSION_SOURCES_CHANGED"
    assert result["error_info"]["catalog_id"] == "GITGO-E7608"
    assert "dialogue_evidence" not in result


def test_long_evidence_uses_existing_detail_reader_without_echoing_large_candidate(tmp_path_factory):
    p, _, _, node = source_case(tmp_path_factory, quote="x" * 13000, value="Recorded rule")
    result = inspect_delivery(p, "d" * 64000)
    decision = result["dialogue_evidence"]["current_decisions"][0]
    assert decision["decision_details"]["node_id"] == node
    assert "source_quote" not in decision
    assert result["candidate_chars"] == 64000
    assert len(json.dumps(result, ensure_ascii=False).encode()) < MAX_VIEW_BYTES + 6000
    assert result["semantic_verdict"] == "not_assessed"


@pytest.mark.parametrize("candidate", ["", None, "d" * 64001])
def test_invalid_candidate_has_separate_typed_diagnostic(tmp_path_factory, candidate):
    p = process(tmp_path_factory)
    result = inspect_delivery(p, candidate)
    assert result["error"] == "WORKBENCH_EXPRESSION_CANDIDATE_INVALID"
    assert result["error_info"]["catalog_id"] == "GITGO-E7606"
    assert lookup_guidance("GITGO-E7606")["procedure"] == "workbench"


def test_inspection_without_a_dialogue_does_not_invent_confirmed_facts(tmp_path_factory):
    p = process(tmp_path_factory)
    result = inspect_delivery(p, "A stable direct answer")
    assert result["status"] == "no_current_dialogue_sources"
    assert result["dialogue_evidence"] is None and result["semantic_verdict"] == "not_assessed"


def test_delivery_version_checks_do_not_rebuild_the_full_decision_projection(tmp_path_factory, monkeypatch):
    import backend.core.loop.decision_support as support
    from backend.core.workbench.expression import assess_excerpt
    p, _, _, _ = source_case(tmp_path_factory)
    original = support.confirmed_user_state
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(support, "confirmed_user_state", counted)
    result = inspect_delivery(p, "## Agreed\n\nDraft")
    assert len(calls) == 1
    binding = p.context_snapshot["workbench_delivery"]["dialogue_excerpt"]
    for _ in range(3):
        assert assess_excerpt(p, result["confirmed_excerpt"], binding)["status"] == "match"
    assert len(calls) == 1


def test_saved_first_version_excerpt_remains_valid_after_restart(tmp_path_factory):
    from backend.core.workbench.expression import _basis, assess_excerpt
    p, _, _, _ = source_case(tmp_path_factory)
    result = inspect_delivery(p, "## Agreed\n\nDraft")
    first_version = {"heading": result["excerpt_heading"], "excerpt": result["confirmed_excerpt"],
                     "basis": _basis(p, legacy=True)}
    assert assess_excerpt(p, result["confirmed_excerpt"], first_version)["status"] == "match"


def test_source_bound_readonly_summary_is_checked_even_without_a_model_inspection_call(tmp_path_factory):
    from tests.test_workbench_delivery import _delivery
    p, _, _, _ = source_case(tmp_path_factory,
        quote="Use internal staff only. Only summarize; do not implement.", value="Internal staff")
    apply_answer_updates(p, [], {"mode": "exploration", "quote": "Only summarize; do not implement."})
    delivery = _delivery(p)
    window = {"action": "continue", "usage_ratio": 0}
    bad = "## Agreed\n\nInternal staff and email are confirmed.\n\n## Proposal\n\nA separate suggestion."
    accepted = delivery.finish(bad, 0, window, source="complete_task")
    assert p.status is ProcessStatus.COMPLETED
    assert accepted.result["workbench_checks"][0]["status"] == "match"
    delivered = accepted.result["response"]
    assert "> Use internal staff only. Only summarize; do not implement." in delivered
    assert "email are confirmed" not in delivered
    assert "## Proposal\n\nA separate suggestion." in delivered


def test_normal_answer_has_no_automatic_expression_ceremony(tmp_path_factory):
    from tests.test_workbench_delivery import _delivery
    p = process(tmp_path_factory)
    p.status = ProcessStatus.RUNNING
    result = _delivery(p).finish("A direct ordinary answer", 0,
        {"action": "continue", "usage_ratio": 0}, source="complete_task")
    assert not result.retry and p.status is ProcessStatus.COMPLETED
    assert "workbench_delivery" not in (p.context_snapshot or {})


def test_partial_known_constraint_is_delivered_without_resolving_the_open_choice(tmp_path_factory):
    from tests.test_workbench_delivery import _delivery
    p = process(tmp_path_factory)
    p.status = ProcessStatus.RUNNING
    workflow, _, node = pending_reply(p, "Internal staff only. Timing is undecided. Only summarize.")
    activate(p)
    apply_answer_updates(p, [{"state_topic": "trip", "disposition": "partial",
        "quote": "Internal staff only.", "value": "The audience is internal staff",
        "remaining": "Timing is undecided"}], {"mode": "exploration", "quote": "Only summarize."})
    before = deepcopy(workflow._load())
    delivered = _delivery(p).finish("## Agreed\n\nInternal staff and a fixed schedule.\n\n## Open\n\nTiming is undecided.",
        0, {"action": "continue", "usage_ratio": 0}, source="complete_task")
    assert not delivered.retry and p.status is ProcessStatus.COMPLETED
    assert "> Internal staff only." in delivered.result["response"]
    assert "and a fixed schedule" not in delivered.result["response"]
    assert "Timing is undecided." in delivered.result["response"]
    assert node not in workflow._load()["records"]
    assert workflow._load() == before


@pytest.mark.parametrize("draft, heading", [("整理当前规则。", "## 已明确的用户原话"),
                                           ("Summarize the current rules.", "## Confirmed user statements")])
def test_heading_fallback_uses_the_supported_draft_language(draft, heading):
    from backend.core.workbench.expression import _heading
    assert _heading(draft, None) == heading
    assert _heading(draft, "Custom title") == "## Custom title"


@pytest.mark.parametrize("entry", ["tool", "text"])
def test_source_construction_precedes_outer_completion_checks(tmp_path_factory, entry):
    from types import SimpleNamespace
    from tests.test_workbench_delivery import _delivery
    from backend.core.loop.completion_protocol import CompletionClaim
    from backend.core.loop.tool_outcome_runtime import ToolOutcomeRuntime
    p, _, _, _ = source_case(tmp_path_factory)
    p.steps_used = 1  # The real completion entry follows a model step.
    bad = "## Agreed\n\nThe request is voided.\n\n## Open\n\nOther choices remain open."
    inspect_delivery(p, bad)
    delivery = _delivery(p)
    window = {"action": "continue", "usage_ratio": 0}
    if entry == "tool":
        p.completion_claim = CompletionClaim.from_args({"result": bad}, step=0)
        outcome = SimpleNamespace(tool_name="complete_task", is_error=False, data={"accepted": True})
        runtime = ToolOutcomeRuntime(p, p.session, delivery, lambda _: None, lambda *_: None, lambda _: None)
        delivered = runtime.evaluate([outcome], "", 0, window, [])
    else:
        delivered = delivery.evaluate_text(bad + "\nTASK_COMPLETE", 0, window, [])
    assert not delivered.retry and p.status is ProcessStatus.COMPLETED, p.session.messages[-2:]
    assert "The request is voided" not in delivered.result["response"]
    assert "> Release the held resource after twenty minutes" in delivered.result["response"]
    assert p._nudge_counters.get("workbench_delivery", 0) == 0


def test_empty_provider_output_does_not_start_source_delivery(tmp_path_factory):
    from tests.test_workbench_delivery import _delivery
    p, _, _, _ = source_case(tmp_path_factory, quote="Use internal staff. Only summarize.")
    apply_answer_updates(p, [], {"mode": "exploration", "quote": "Only summarize."})
    result = _delivery(p).evaluate_text("", 0, {"action": "continue", "usage_ratio": 0}, [])
    assert p.status is ProcessStatus.RUNNING
    assert "workbench_delivery" not in p.context_snapshot


@pytest.mark.parametrize('draft', ['**Agreed**\n\nA paraphrase.\n\n**Advice**\n\nMy proposal.',
                                  'A plain answer without Markdown headings.'])
def test_unstructured_source_delivery_needs_no_format_repair(tmp_path_factory, draft):
    from tests.test_workbench_delivery import _delivery
    from backend.core.workbench.expression import _section
    p, _, _, _ = source_case(tmp_path_factory, quote='Keep the item. Only summarize.')
    apply_answer_updates(p, [], {'mode': 'exploration', 'quote': 'Only summarize.'})
    before = deepcopy(p.session.host_ledger)
    result = _delivery(p).finish(draft, 0, {'action': 'continue', 'usage_ratio': 0}, source='complete_task')
    assert not result.retry and p.status is ProcessStatus.COMPLETED
    assert result.result['workbench_checks'][0]['status'] == 'match'
    assert _section(result.result['response'], '## Confirmed user statements') == '## Confirmed user statements\n\n> Keep the item. Only summarize.'
    assert draft in result.result['response'] and p.session.host_ledger == before
    assert not p._nudge_counters.get('workbench_delivery')


def test_decision_summary_constructs_status_and_preserves_requested_comparison_body(tmp_path_factory):
    from tests.test_workbench_delivery import _delivery
    from backend.core.loop.engineering_workflow import EngineeringWorkflow
    from tests.test_dynamic_alignment import question
    p = process(tmp_path_factory)
    p.status = ProcessStatus.RUNNING
    w = EngineeringWorkflow(p)
    card = w.ask_semantic({'questions': [question('audience'), question('format'),
        question('distribution', after=['audience'])]})
    source_node = card['questions'][0]['node_id']
    reply = 'Internal readers only. Choose the file format yourself. Only summarize the decision status.'
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': p.active_task_id,
        'process_id': p.process_id, 'decision_id': card['decision_id'],
        'answers': {source_node: {'answer': reply}}})
    p.pending_decision = None
    activate(p)
    apply_answer_updates(p, [
        {'state_topic': 'audience', 'disposition': 'partial', 'quote': 'Internal readers only.',
         'remaining': 'Which internal group may read the report?'},
        {'state_topic': 'format', 'disposition': 'delegated', 'quote': 'Choose the file format yourself.'}],
        {'mode': 'decision_comparison', 'quote': 'Only summarize the decision status.'})
    assert p.context_snapshot['alignment_runtime']['scope']['mode'] == 'exploration'
    assert p.context_snapshot['alignment_runtime']['scope']['presentation'] == 'decision_status'
    body = 'Two distribution options remain possible; neither is adopted.'
    result = _delivery(p).finish(body, 0, {'action': 'continue', 'usage_ratio': 0}, source='complete_task')
    assert not result.retry and p.status is ProcessStatus.COMPLETED
    response = result.result['response']
    assert '> Internal readers only.' in response
    assert '## Delegated scope' in response and '> Choose the file format yourself.' in response
    assert 'Which internal group may read the report?' in response
    assert 'distribution?' not in response and 'Dependent choices remain' in response
    assert response.endswith('## Discussion\n\n' + body)
    assert all(c['status'] == 'match' for c in result.result['workbench_checks'])
    assert len(w.status()['nodes']) == 3 and not w.status()['complete']
    assert not w.guard('write_file', 'workspace_write', {'path': 'report.py'})['allowed']


def test_status_summary_is_idempotent_without_another_model_or_hidden_status_write(tmp_path_factory):
    from backend.core.workbench.expression import prepare_task_delivery, compose_task_delivery
    p, _, _, _ = source_case(tmp_path_factory, quote='Keep the resource. Summarize status only.')
    apply_answer_updates(p, [], {'mode': 'decision_summary', 'quote': 'Summarize status only.'})
    prepare_task_delivery(p, 'This round ends here.')
    first = compose_task_delivery(p, 'This round ends here.')
    before = deepcopy((p.context_snapshot, p.session.host_ledger))
    assert prepare_task_delivery(p, first) is None
    assert compose_task_delivery(p, first) == first
    assert first.count('## Confirmed') == 1 and first.count('## Open decisions') == 1
    assert 'This round ends here.' not in first
    assert (p.context_snapshot, p.session.host_ledger) == before


def test_explicit_discussion_is_the_actual_delivery_body_without_a_duplicate_draft(tmp_path_factory):
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.workbench.expression import prepare_task_delivery, compose_task_delivery, assess_excerpt
    p, _, _, _ = source_case(tmp_path_factory,
        quote='Keep the item. Summarize status and compare options.')
    body = 'Option A preserves access. Option B expands it. Neither has been adopted.'
    args = {'result': 'AN_UNPUBLISHED_REDUNDANT_DRAFT', 'scope_update': {
        'quote': 'Summarize status and compare options.', 'discussion': body}}
    recorded = build_internal_tools(p, {}, str(tmp_path_factory), None, None, None)['complete_task'].execute(args)
    assert recorded['accepted'] and p.completion_claim.result == body
    assert args['result'] == 'AN_UNPUBLISHED_REDUNDANT_DRAFT'  # Original tool evidence is retained.
    prepare_task_delivery(p, p.completion_claim.result)
    delivered = compose_task_delivery(p, p.completion_claim.result)
    binding = deepcopy(p.context_snapshot['workbench_delivery']['dialogue_excerpt'])
    assert delivered.endswith('## Discussion\n\n' + body) and 'AN_UNPUBLISHED' not in delivered
    assert assess_excerpt(p, delivered, binding)['status'] == 'match'
    assert 'discussion' not in p.context_snapshot['alignment_runtime']['scope']  # No second long-body copy.
    changed = compose_task_delivery(p, body + ' A new rule is adopted.')
    check = assess_excerpt(p, changed, binding)
    assert check['status'] == 'contradiction' and check['diagnostic_code'] == 'WORKBENCH_DISCUSSION_CHANGED'
    assert assess_excerpt(p, binding['excerpt'], binding)['diagnostic_code'] == 'WORKBENCH_DISCUSSION_CHANGED'


def test_recorded_scope_survives_following_completion_without_resubmitting_it(tmp_path_factory):
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.workbench.expression import prepare_task_delivery, compose_task_delivery
    p, _, _, _ = source_case(tmp_path_factory, quote='Keep the item. Summarize status only.')
    apply_answer_updates(p, [], {'quote': 'Summarize status only.'})
    before = deepcopy((p.session.host_ledger, p.context_snapshot['alignment_runtime']['scope']))
    tool = build_internal_tools(p, {}, str(tmp_path_factory), None, None, None)['complete_task']
    assert tool.execute({'result': 'A redundant new draft.'})['accepted']
    prepare_task_delivery(p, p.completion_claim.result)
    delivered = compose_task_delivery(p, p.completion_claim.result)
    assert 'redundant new draft' not in delivered and '> Keep the item.' in delivered
    assert (p.session.host_ledger, p.context_snapshot['alignment_runtime']['scope']) == before


def test_retired_irrelevant_question_is_reported_as_unknown_not_as_a_confirmed_fact(tmp_path_factory):
    from backend.core.workbench.expression import prepare_task_delivery, compose_task_delivery
    p = process(tmp_path_factory)
    p.status = ProcessStatus.RUNNING
    w, _, node = pending_reply(p, 'This record is unavailable. Only summarize current status.')
    apply_answer_updates(p, [{'state_topic': 'trip', 'disposition': 'not_needed',
        'quote': 'Only summarize current status.', 'reason': 'The current readonly status can retain the missing fact.'}],
        {'quote': 'Only summarize current status.'})
    prepare_task_delivery(p, 'An unused draft.')
    response = compose_task_delivery(p, 'An unused draft.')
    assert 'not needed for this response; still unknown' in response
    assert 'No open item' not in response and 'No confirmed decision' in response
    assert w.status()['nodes'][0]['status'] == 'retired'
    assert w._load()['records'][node]['elicitation_retirement']


def test_volunteered_delegation_displays_its_actual_scope_without_an_internal_topic_label(tmp_path_factory):
    from backend.core.workbench.expression import prepare_task_delivery, compose_task_delivery
    p, _, _, _ = source_case(tmp_path_factory,
        quote='Keep the item. You choose implementation. Summarize status only.')
    apply_answer_updates(p, [{'state_topic': 'internal.generated.scope', 'disposition': 'delegated',
        'quote': 'You choose implementation.'}], {'mode': 'decision_summary', 'quote': 'Summarize status only.'})
    prepare_task_delivery(p, 'This round ends here.')
    result = compose_task_delivery(p, 'This round ends here.')
    assert '## Delegated scope\n\n> You choose implementation.' in result
    assert 'internal.generated.scope' not in result


def test_summary_and_comparison_are_explicit_versioned_presentation_choices(tmp_path_factory):
    from backend.core.workbench.expression import prepare_task_delivery, compose_task_delivery, assess_excerpt
    p, _, _, _ = source_case(tmp_path_factory,
        quote='Keep the resource. Summarize status and compare two options.')
    requested = 'Summarize status and compare two options.'
    apply_answer_updates(p, [], {'mode': 'decision_summary', 'quote': requested})
    prepare_task_delivery(p, 'AN_UNPUBLISHED_DRAFT')
    candidate = compose_task_delivery(p, 'AN_UNPUBLISHED_DRAFT')
    old = deepcopy(p.context_snapshot['workbench_delivery']['dialogue_excerpt'])
    assert 'AN_UNPUBLISHED_DRAFT' not in candidate and not old['include_discussion']
    apply_answer_updates(p, [], {'mode': 'decision_comparison', 'quote': requested})
    assert assess_excerpt(p, candidate, old)['status'] == 'stale'
    prepare_task_delivery(p, 'The two requested alternatives.')
    current = compose_task_delivery(p, 'The two requested alternatives.')
    assert current.endswith('## Discussion\n\nThe two requested alternatives.')
    assert p.context_snapshot['workbench_delivery']['dialogue_excerpt']['include_discussion']


def test_earlier_status_binding_without_a_body_flag_preserves_its_original_discussion_contract(tmp_path_factory):
    from backend.core.workbench.expression import prepare_task_delivery, compose_task_delivery
    p, _, _, _ = source_case(tmp_path_factory, quote='Keep the resource. Summarize status only.')
    apply_answer_updates(p, [], {'mode': 'decision_summary', 'quote': 'Summarize status only.'})
    # Reproduce the earlier stored presentation schema, without rewriting its receipt.
    p.context_snapshot['alignment_runtime']['scope'].pop('include_discussion')
    before = deepcopy(p.session.host_ledger)
    prepare_task_delivery(p, 'The retained discussion.')
    delivered = compose_task_delivery(p, 'The retained discussion.')
    assert delivered.endswith('## Discussion\n\nThe retained discussion.')
    assert p.session.host_ledger == before


def test_changing_declared_open_graph_invalidates_status_even_without_a_new_answer(tmp_path_factory):
    from backend.core.workbench.expression import prepare_task_delivery, compose_task_delivery, assess_excerpt
    from tests.test_dynamic_alignment import question
    p, w, _, _ = source_case(tmp_path_factory, quote='Keep the resource. Summarize status only.')
    apply_answer_updates(p, [], {'mode': 'decision_summary', 'quote': 'Summarize status only.'})
    prepare_task_delivery(p, 'No further action.')
    candidate = compose_task_delivery(p, 'No further action.')
    binding = deepcopy(p.context_snapshot['workbench_delivery']['dialogue_excerpt'])
    w.ask_semantic(question('delivery'))
    assert assess_excerpt(p, candidate, binding)['status'] == 'stale'
    assert assess_excerpt(p, candidate, binding)['diagnostic_code'] == 'WORKBENCH_EXPRESSION_EVIDENCE_STALE'


def test_oversized_status_blocks_delivery_instead_of_certifying_a_partial_list(tmp_path_factory):
    from tests.test_workbench_delivery import _delivery
    from tests.test_dynamic_alignment import question
    from backend.core.workbench.expression import prepare_task_delivery
    p, w, _, _ = source_case(tmp_path_factory, quote='Keep the resource. Summarize status only.')
    apply_answer_updates(p, [], {'mode': 'decision_summary', 'quote': 'Summarize status only.'})
    # Actual declared questions exceed the complete render bound; no truncation.
    w.ask_semantic({'questions': [{**question('group'+str(i)), 'question': 'Detail '+str(i)+' '+('x'*2700)} for i in range(7)]})
    p.pending_decision = None
    preparation = prepare_task_delivery(p, 'No more details.')
    assert preparation['error'] == 'WORKBENCH_EXPRESSION_STATUS_TOO_LARGE'
    assert preparation['error_info']['catalog_id'] == 'GITGO-E7611'
    result = _delivery(p).finish('No more details.', 0, {'action': 'continue', 'usage_ratio': 0}, source='complete_task')
    assert result.retry and p.status is not ProcessStatus.COMPLETED
    assert 'dialogue_excerpt' not in p.context_snapshot.get('workbench_delivery', {})


def test_status_coalesces_literal_nested_quotes_from_the_same_actual_receipt(tmp_path_factory):
    from backend.core.workbench.expression import prepare_task_delivery, compose_task_delivery
    p, _, _, _ = source_case(tmp_path_factory,
        quote='Keep the resource. Ship the report. Summarize status only.')
    apply_answer_updates(p, [
        {'state_topic': 'combined_source', 'disposition': 'confirmed',
         'quote': 'Keep the resource. Ship the report.'},
        {'state_topic': 'report_fact', 'disposition': 'confirmed', 'quote': 'Ship the report.'}],
        {'quote': 'Summarize status only.'})
    before = deepcopy(p.session.host_ledger)
    prepare_task_delivery(p, 'Unused draft')
    delivered = compose_task_delivery(p, 'Unused draft')
    assert '> Keep the resource. Ship the report.' in delivered
    assert '\n> Ship the report.' not in delivered
    assert p.session.host_ledger == before


def test_status_literal_coalescing_does_not_guess_semantic_or_cross_receipt_equivalence():
    from backend.core.workbench.expression import _distinct_source_quotes
    quotes = ['Keep the resource.', 'Keep the resource. Ship the report.', 'Retain the resource.']
    records = [{'decision_id': source, 'source_quote': quote, 'disposition': 'confirmed'}
               for source, quote in zip(['one', 'two', 'one'], quotes)]
    assert _distinct_source_quotes(quotes, records, []) == quotes
    records[1]['decision_id'] = 'one'
    assert _distinct_source_quotes(quotes, records, []) == quotes[1:]
    records[1]['source_node_id'] = 'a_different_submitted_answer'
    assert _distinct_source_quotes(quotes, records, []) == quotes
