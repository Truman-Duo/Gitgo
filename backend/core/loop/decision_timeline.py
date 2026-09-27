"""Public decision cards derived from the durable Host ledger, not a second log."""
from __future__ import annotations


def decision_timeline(ledger: list[dict]) -> list[dict]:
    requests: dict[str, dict] = {}
    answers: dict[str, dict] = {}
    cancellations: dict[str, dict] = {}
    for event in ledger:
        identity = str(event.get("decision_id") or "")
        if not identity:
            continue
        if event.get("event") == "user_decision_requested":
            requests.setdefault(identity, event)
        elif event.get("event") == "user_decision_received":
            answers[identity] = event
        elif event.get("event") == "user_decision_cancelled":
            cancellations[identity] = event
    cards = []
    for identity, request in requests.items():
        answer = answers.get(identity)
        cancellation = cancellations.get(identity)
        public_request = {key: request.get(key) for key in (
            "decision_id", "process_id", "task_id", "question", "why_user_must_decide",
            "options", "allow_free_form", "created_at", "decision_sequence", "context_epoch",
            "kind", "state_topic", "supersedes_decision_id", "permission_request",
            "source_process_id", "source_actor_kind", "source_display_name",
            "owner_process_id",
        )}
        cards.append({
            "message_id": f"decision:{identity}", "turn_id": str(request.get("task_id") or ""),
            "role": "assistant", "kind": "decision", "visibility": "public", "final": True,
            "content": str(request.get("question") or ""),
            "timestamp": str(request.get("created_at") or ""),
            "sequence": int(request.get("message_sequence", 2**31 - 1)),
            "decision_sequence": int(request.get("decision_sequence", 0) or 0),
            "decision": public_request,
            "decision_answer": str(answer.get("answer") or "") if answer else None,
            "decision_cancellation": str(cancellation.get("reason") or "") if cancellation else None,
            "status": "answered" if answer else "cancelled" if cancellation else "awaiting_user",
        })
    return sorted(cards, key=lambda item: (
        int(item.get("decision_sequence", 0) or 0), str(item.get("timestamp") or ""),
        str(item.get("message_id") or ""),
    ))
