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
