import copy
import json

import pytest

from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.decision_support import create_user_decision, validate_round_answer, confirmed_user_state


def setup(root):
    p = AgentProcess(process_id="alignment", role="worker", ring_level=RingLevel.RING_3,
                     active_task_id="task", worktree_path=str(root), session=AgentSession())
    w = EngineeringWorkflow(p)
    q = lambda text: {"question": text, "why_user_must_decide": "Product intent belongs to you", "kind": "clarification", "options": []}
    plan = {"profiles": ["alignment"], "questions": [
        {"id": "scope", "state_topic": "scope", "request": q("Which scope?")},
        {"id": "audience", "state_topic": "audience", "request": q("Which audience?")},
        {"id": "detail", "state_topic": "detail", "depends_on": ["scope"], "when": {"scope": ["large"]}, "request": q("Which detail?")},
    ]}
    w.configure(plan)
    return p, w, plan


def receive(p, request, values):
    instruction = json.dumps({"answers": values})
    answers = validate_round_answer(request, instruction)
    p.session.host_ledger.append({"event": "user_decision_received", "task_id": "task",
                                  "decision_id": request["decision_id"], "answer": instruction, "answers": answers})
    p.pending_decision = None
    w = EngineeringWorkflow(p)
    for node_id, value in values.items():
        w.resolve_answer(node_id, request['decision_id'], {'disposition': 'confirmed', 'value': value, 'quote': value, 'choice_key': value})


def test_round_uses_only_ready_frontier_and_real_answers(tmp_path_factory):
    p, w, _ = setup(tmp_path_factory)
    request = w.invoke({"operation": "ask_frontier"})
    assert [q["node_id"] for q in request["questions"]] == ["scope"]
    assert request['question'] == request['questions'][0]['question']
    assert not w.status()["complete"]
    before = copy.deepcopy(p.context_snapshot)
    with pytest.raises(ValueError):
        validate_round_answer(request, json.dumps({"answers": {"audience": "teams"}}))
    assert p.context_snapshot == before
    assert p.pending_decision["decision_id"] == request["decision_id"]
    receive(p, request, {"scope": "large"})
    assert w.status()["question_frontier"] == ["audience", "detail"]
    audience = w.ask_frontier()
    assert audience['decision_id'] != request['decision_id']
    assert [q['node_id'] for q in audience['questions']] == ['audience']
    receive(p, audience, {"audience": "teams"})
    assert w.status()["question_frontier"] == ["detail"]
    assert len(confirmed_user_state(p.session.host_ledger)) == 2
    second = w.ask_frontier()
    receive(p, second, {"detail": "audit trail"})
    assert w.status()["complete"]


def test_inactive_branch_is_derived_and_changes_reopen_dependents(tmp_path_factory):
    p, w, _ = setup(tmp_path_factory)
    request = w.ask_frontier()
    receive(p, request, {"scope": "small"})
    receive(p, w.ask_frontier(), {"audience": "teams"})
    assert w.status()["complete"]
    assert w.status()["nodes"][2]["evidence"]["inapplicable"]
    revision = w.revise_decision("scope", {"kind": "clarification", "question": "Reconsider scope?", "why_user_must_decide": "You requested a larger scope", "options": []})
    assert revision["supersedes_decision_id"] == request["decision_id"]
    assert not w.status()["complete"]
    p.session.host_ledger.append({"event": "user_decision_received", "task_id": "task", "state_topic": "scope", "decision_id": revision["decision_id"], "answer": "large"})
    p.pending_decision = None
    w.resolve_answer('scope', revision['decision_id'], {'disposition': 'confirmed', 'value': 'large', 'quote': 'large'})
    assert w.status()["question_frontier"] == ["detail"]
    assert any(n.get("code") == "WORKFLOW_EVIDENCE_STALE" for n in p.session.host_ledger)


def test_rich_answer_cannot_silently_disable_a_matching_conditional_branch(tmp_path_factory):
    p, w, _ = setup(tmp_path_factory)
    request = w.ask_frontier()
    answers = validate_round_answer(request, json.dumps({'answers': {'scope': 'large, shared by a whole team'}}))
    p.session.host_ledger.append({'event': 'user_decision_received', 'task_id': 'task',
        'decision_id': request['decision_id'], 'answers': answers})
    p.pending_decision = None
    proposal = {'disposition': 'confirmed', 'value': 'A large shared application', 'quote': 'large, shared by a whole team'}
    with pytest.raises(ValueError, match='choice_key'):
        w.resolve_answer('scope', request['decision_id'], proposal)
    with pytest.raises(ValueError, match='choice_key'):
        w.resolve_answer('scope', request['decision_id'], {**proposal, 'value': proposal['quote']})
    assert 'scope' in w.status()['question_frontier']
    w.resolve_answer('scope', request['decision_id'], {**proposal, 'choice_key': 'large'})
    receive(p, w.ask_frontier(), {'audience': 'teams'})
    assert w.status()['question_frontier'] == ['detail']
    assert not w.status()['nodes'][2]['evidence']


def test_dynamic_addition_and_failed_round_leave_no_unanswerable_pending(tmp_path_factory):
    p, w, plan = setup(tmp_path_factory)
    plan["questions"].append({"id": "locale", "state_topic": "locale", "depends_on": ["audience"], "request": {"question": "Language?", "why_user_must_decide": "Preference", "kind": "clarification", "options": []}})
    w.configure(plan)
    assert len(w.status()["nodes"]) == 4
    bad = copy.deepcopy(plan)
    bad["questions"][0]["request"]["options"] = [{"label": "Grant", "action": "allow_once"}]
    p2 = AgentProcess(process_id="other", role="worker", ring_level=RingLevel.RING_3, active_task_id="task", worktree_path=str(tmp_path_factory), session=AgentSession())
    w2 = EngineeringWorkflow(p2)
    w2.configure(bad)
    assert w2.invoke({"operation": "ask_frontier"})["error"]
    assert p2.pending_decision is None
    assert not w2.status()["complete"]


def test_clarification_does_not_weaken_permission_or_choices(tmp_path_factory):
    p, _, _ = setup(tmp_path_factory)
    assert create_user_decision(p, {"kind": "clarification", "question": "Which users?", "why_user_must_decide": "Intent", "options": []})["options"] == []
    p.pending_decision = None
    for kind in ("permission", "choice", "direction"):
        with pytest.raises(ValueError):
            create_user_decision(p, {"kind": kind, "state_topic": "scope", "question": "Allow?", "why_user_must_decide": "Reason", "options": []})


def test_default_question_enters_graph_and_duplicate_uses_confirmed_answer(tmp_path_factory):
    from backend.core.loop.executor import _build_internal_tools
    p = AgentProcess(process_id="default", role="worker", ring_level=RingLevel.RING_3, active_task_id="task", worktree_path=str(tmp_path_factory), session=AgentSession())
    tool = _build_internal_tools(p, {}, str(tmp_path_factory))["request_user_decision"]
    request = {"kind": "clarification", "question": "Target user?", "why_user_must_decide": "Your intent", "options": [], "state_topic": "audience"}
    result = tool.execute(request)
    w = EngineeringWorkflow(p)
    assert w.status()["active"]
    p.session.host_ledger.append({"event": "user_decision_received", "decision_id": result["decision_id"], "task_id": "task", "state_topic": "audience", "answer": "teams"})
    p.pending_decision = None
    w.resolve_answer(w.status()['nodes'][0]['id'], result['decision_id'], {'disposition': 'confirmed', 'value': 'teams', 'quote': 'teams'})
    assert w.status()["complete"]
    assert tool.execute(request)["status"] == "resolved"
    assert p.pending_decision is None


def test_publication_failure_cancels_orphan_card_and_notifies(tmp_path_factory, monkeypatch):
    p, w, _ = setup(tmp_path_factory)
    original = copy.deepcopy(p.context_snapshot)
    monkeypatch.setattr(w, "_save", lambda _: (_ for _ in ()).throw(OSError("disk failed")))
    assert w.invoke({"operation": "ask_frontier"})["error"]
    assert p.pending_decision is None
    assert p.context_snapshot == original
    assert any(n.get("event") == "user_decision_cancelled" for n in p.session.host_ledger)
    assert any(n.get("code") == "WORKFLOW_RECOVERY_REQUIRED" for n in p.session.host_ledger)
