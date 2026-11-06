"""Regression of received answers being re-published as the old question."""
import copy
import json

import pytest

from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.decision_support import validate_round_answer
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from tests.test_alignment_semantics import process, pending_reply


def choice():
    option = lambda label: {key: label for key in ('label', 'principle', 'immediate_effect', 'downstream_effect', 'risks', 'reversibility')}
    return {'kind': 'choice', 'question': 'Sort by name or size?', 'why_user_must_decide': 'Choose presentation',
            'options': [option('name'), option('size')]}


def receive_choice(p, request):
    p.session.host_ledger.append({'event': 'user_decision_received', 'kind': request['kind'], 'task_id': 't',
        'decision_id': request['decision_id'], 'state_topic': request['state_topic'],
        'answer': 'Choose option 2: size', 'selected_label': 'size'})
    p.pending_decision = None


def test_choice_without_model_supplied_topic_joins_the_existing_workflow(tmp_path_factory):
    p = process(tmp_path_factory); runtime = AlignmentRuntime(p)
    runtime.payload('List files after a choice')
    w = EngineeringWorkflow(p)
    request = w.ask_semantic(choice())
    assert request['state_topic'].startswith('choice:')
    receive_choice(p, request)
    payload = runtime.payload()
    assert payload['confirmed_answers'] and not payload['actual_answers']
    assert w.status()['complete']


def test_legacy_standalone_card_can_be_interpreted_without_republishing(tmp_path_factory):
    from backend.core.loop.decision_support import create_user_decision
    p = process(tmp_path_factory); runtime = AlignmentRuntime(p)
    runtime.payload('List files after a choice')
    request = create_user_decision(p, choice())
    receive_choice(p, request)
    payload = runtime.payload()
    assert payload['actual_answers'][0]['node_id'] == request['decision_id']
    result = runtime.apply({'status': 'ready', 'questions': [], 'interpretations': [{
        'node_id': request['decision_id'], 'decision_id': request['decision_id'],
        'disposition': 'confirmed', 'quote': 'Choose option 2: size', 'value': 'size'}]}, payload)
    assert result['status'] == 'ready' and EngineeringWorkflow(p).status()['complete']
    assert sum(e.get('event') == 'user_decision_requested' for e in p.session.host_ledger) == 1


def test_finite_choice_inside_round_is_consumed_before_next_question(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload("Inspect a document")
    workflow = EngineeringWorkflow(p)
    option = lambda label: {"label": label, "principle": label, "immediate_effect": label,
                            "downstream_effect": label, "risks": label, "reversibility": label}
    question = {"kind": "choice", "state_topic": "input-route",
        "question": "How will you supply the document?", "why_user_must_decide": "Choose the source",
        "options": [option("Paste contents"), option("Supply file")]}
    workflow.configure({"profiles": ["alignment"], "nodes": [{"id": "route", "kind": "decision",
        "state_topic": "input-route", "request": question}]})
    request = workflow.ask_frontier()
    node = request["questions"][0]["node_id"]
    answers = validate_round_answer(request, json.dumps({"answers": {node: "Choose option 1: Paste contents"}}))
    p.session.host_ledger.append({"event": "user_decision_received", "kind": "clarification", "task_id": "t",
        "decision_id": request["decision_id"], "answers": answers})
    p.pending_decision = None
    result = runtime.apply({"status": "needs_input", "interpretations": [], "questions": [
        {"state_topic": "contents", "question": "Paste the document contents.",
         "why_user_must_decide": "The chosen source has not been supplied", "tier": "foundational"}]}, runtime.payload())
    assert result["decision"]["question"] == "Paste the document contents."
    assert workflow._load()["records"][node]["value"] == "Paste contents"
    assert result["decision"]["decision_id"] != request["decision_id"]


def test_free_form_answer_is_not_reasked_when_assessor_forgets_its_interpretation(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload("Plan a trip")
    _, _, _ = pending_reply(p, "Japan, dates not decided")
    before = copy.deepcopy(p.context_snapshot)
    with pytest.raises(ValueError, match="Interpret the received answers"):
        runtime.apply({"status": "needs_input", "interpretations": [], "questions": [
            {"state_topic": "dates", "question": "Which dates?", "why_user_must_decide": "Needed for the plan",
             "tier": "foundational"}]}, runtime.payload())
    assert p.context_snapshot == before
    assert p.pending_decision is None
