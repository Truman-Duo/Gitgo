"""Host-owned routing metadata for durable Agent-to-user questions.

The decision ledger remains the single authoritative fact.  This module adds
ownership and presentation routing without copying question state into a
second queue or asking a model to forward messages.
"""
from __future__ import annotations


def root_process(process):
    manager = getattr(process, "_manager", None)
    current = process
    seen: set[str] = set()
    while manager is not None and getattr(current, "parent_id", None):
        if current.process_id in seen:
            break
        seen.add(current.process_id)
        parent = manager.get(current.parent_id)
        if parent is None:
            break
        current = parent
    return current


def route_question(process, request: dict) -> dict:
    """Bind a question to its source B and governing root A."""
    owner = root_process(process)
    source_session = getattr(process, "session", None)
    routed = {
        **request,
        "source_process_id": process.process_id,
        "source_actor_kind": str(getattr(process, "actor_kind", "worker")),
        "source_display_name": str(getattr(source_session, "display_name", "") or ""),
        "owner_process_id": owner.process_id,
    }
    return routed


def pending_questions(processes) -> list[dict]:
    """Project every pending question once, ordered for deterministic UI queues."""
    result = []
    for process in processes:
        request = getattr(process, "pending_decision", None)
        if not isinstance(request, dict):
            continue
        routed = route_question(process, dict(request))
        result.append(routed)
    return sorted(result, key=lambda item: (
        str(item.get("created_at") or ""),
        int(item.get("decision_sequence") or 0),
        str(item.get("decision_id") or ""),
    ))


def decision_evidence(process, *, limit=8):
    """Bounded read projection of this task's canonical decision ledger.

    A broker question can be published during semantic compilation, without
    separately executing the original proposed function batch. Tool counts
    cannot prove that a user question/answer did not exist.
    """
    from .decision_timeline import decision_timeline
    task = process.active_task_id or process.process_id
    cards = [card for card in decision_timeline(process.session.host_ledger)
             if card['decision'].get('task_id') == task]
    items = []
    for card in cards[-limit:]:
        request = card['decision']
        answer = card.get('decision_answer')
        items.append({
            'decision_id': request['decision_id'], 'task_id': task,
            'source_process_id': request.get('source_process_id') or process.process_id,
            'owner_process_id': request.get('owner_process_id') or root_process(process).process_id,
            'kind': request.get('kind'), 'status': card['status'],
            'question': str(request.get('question') or '')[:800],
            'options': [{'label': str(option.get('label') or '')[:240]}
                        for option in request.get('options') or []],
            'answer': answer[:2000] if answer is not None else None,
            'answer_state': 'received' if answer is not None else 'not_received',
            'answer_truncated': bool(answer and len(answer) > 2000),
        })
    return {'authority': 'canonical_decision_ledger', 'items': items,
            'total': len(cards), 'partial': len(cards) > limit,
            'scope': 'current_child_task; receiving a reply is not semantic confirmation or permission'}
