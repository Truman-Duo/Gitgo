"""Host-owned routing metadata for durable Agent-to-user questions.

The decision ledger remains the single authoritative fact.  This module adds
ownership and presentation routing without copying question state into a
second queue or asking a model to forward messages.
"""
from __future__ import annotations

from copy import deepcopy


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
            'materials': deepcopy(request.get('materials', [])),
        })
    return {'authority': 'canonical_decision_ledger', 'items': items,
            'total': len(cards), 'partial': len(cards) > limit,
            'scope': 'current_child_task; receiving a reply is not semantic confirmation or permission'}


def attach_material(process, decision_id, reference, *, store):
    """Attach immutable evidence without answering or reauthorizing a question.

    The existing ledger and process checkpoint own this association. It is not
    a second question queue, and repeated delivery adds no duplicate fact.
    """
    from .mechanism_lifecycle import fence, commit_activation
    from .decision_timeline import decision_timeline
    import json
    if store is None:
        raise ValueError("QUESTION_MATERIAL_CHECKPOINT_UNAVAILABLE")
    task = process.active_task_id or process.process_id
    with fence(process), process._coordination_lock:
        cards = decision_timeline(process.session.host_ledger)
        card = next((item for item in cards if item['decision']['decision_id'] == decision_id
                     and item['decision']['task_id'] == task), None)
        if card is None or card['status'] in {'cancelled', 'superseded'}:
            raise ValueError("QUESTION_MATERIAL_TARGET_STALE")
        if any(item['decision'].get('supersedes_decision_id') == decision_id for item in cards):
            raise ValueError("QUESTION_MATERIAL_TARGET_STALE")
        old = next((item for item in process.session.host_ledger if item.get('event') == 'user_decision_material_attached'
                    and item.get('task_id') == task and item.get('decision_id') == decision_id
                    and item.get('reference') == reference), None)
        if old:
            return deepcopy(old)
        storage = getattr(process, 'bound_storage', None) or store._storage
        blob = storage.read_blob(reference)
        if len(blob) > 262144:
            raise ValueError("QUESTION_MATERIAL_CAPACITY")
        record = json.loads(blob)
        if record.get('project_id') != storage.paths.project_id:
            raise ValueError("QUESTION_MATERIAL_PROJECT_CHANGED")
        count = sum(item.get('event') == 'user_decision_material_attached' and item.get('decision_id') == decision_id
                    for item in process.session.host_ledger)
        if count >= 32:
            raise ValueError("QUESTION_MATERIAL_CAPACITY")
        fact = {'event': 'user_decision_material_attached', 'task_id': task, 'process_id': process.process_id, 'decision_id': decision_id,
                'reference': reference, 'summary': str(record.get('answer') or '')[:2000],
                'source': 'immutable_discussion', 'authority_granted': False}
        before = deepcopy(process.pending_decision)
        if before and before['decision_id'] == decision_id:
            process.pending_decision = {**before, 'materials': [*before.get('materials', []),
                {key: fact[key] for key in ('reference', 'summary', 'source', 'authority_granted')}]}
        commit_activation(process, fact, store, lambda: setattr(process, 'pending_decision', before))
        return deepcopy(fact)
