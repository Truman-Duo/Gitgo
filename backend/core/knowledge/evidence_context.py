"""Native task-context identity; matching contracts do not establish causality."""
from __future__ import annotations
import hashlib
import json

def task_context_snapshot(context: dict) -> dict:
    from backend.core.loop.task_contract import semantic_contract
    contract = dict(context.get("task_contract") or {})
    semantic = semantic_contract(contract)
    digest = hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str).encode()).hexdigest() if semantic else ""
    return {**{key: contract.get(key) for key in ("revision", "goal", "acceptance_criteria", "deliverables")},
            "semantic_digest": digest}

def correlation_hint(previous: dict, current_context: dict, current_receipt: dict) -> dict:
    old = previous.get("task_context") or {}
    old_digest, new_digest = old.get("semantic_digest"), current_context.get("semantic_digest")
    same = old_digest == new_digest if old_digest and new_digest else None
    old_receipt = previous.get("receipt") or {}
    command = old_receipt.get("command_digest")
    current_command = current_receipt.get("command_digest")
    return {"same_contract": same, "same_revision": old.get("revision") == current_context.get("revision")
                if old.get("revision") is not None and current_context.get("revision") is not None else None,
            "same_command": command == current_command if command and current_command else None,
            "relation": "different_contract" if same is False else "candidate_relation",
            "causal_repair": "not_established"}
