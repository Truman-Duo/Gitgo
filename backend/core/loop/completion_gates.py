"""Typed completion facts and source-bound exceptions; no prose classifiers."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json


class CompletionReasons(list):
    """Keep legacy reason text while producers declare the actual gate policy."""
    def __init__(self):
        super().__init__()
        self.gates = []
        self.kind = "completion_integrity"
        self.degradable = False

    @contextmanager
    def scope(self, kind, *, degradable=False):
        previous = self.kind, self.degradable
        self.kind, self.degradable = kind, degradable
        try:
            yield self
        finally:
            self.kind, self.degradable = previous

    def append(self, message):
        super().append(message)
        digest = hashlib.sha256(json.dumps([self.kind, message], ensure_ascii=False).encode()).hexdigest()[:20]
        self.gates.append({"gate_id": "gate_" + digest, "kind": self.kind,
            "message": message, "source": "HostCompletionEvaluator", "blocking": True,
            "degradable_by_explicit_user_decision": self.degradable,
            "resolution_options": (["satisfy the missing evidence", "accept a truthful partial delivery or propose a concrete scope amendment"]
                if self.degradable else ["satisfy the authoritative requirement", "stop safely; no missing fact is a pass"])})

    def extend(self, messages):
        for message in messages:
            self.append(message)


def contract_digest(process):
    context, _ = process.read_context_snapshot()
    workflow = context.get("engineering_workflow") or {}
    value = {"task_id": process.active_task_id or process.process_id,
        "process_id": process.process_id, "task_kind": process.task_kind,
        "required_tests": list(process.required_test_ids), "constraints": list(process.task_constraints),
        "task_contract": context.get("task_contract") or {}, "workflow_plan": workflow.get("plan") or {}}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def accepts_partial(process, gates):
    """A runtime flag alone is never user evidence or permission to waive a gate."""
    exception = process.runtime_preferences.get("completion_exception") or {}
    decision_id = exception.get("decision_id")
    if not gates or not decision_id or not all(g.get("degradable_by_explicit_user_decision") is True for g in gates):
        return False
    if exception.get("contract_digest") != contract_digest(process):
        return False
    task_id = process.active_task_id or process.process_id
    ledger = process.session.host_ledger
    request = next((e for e in reversed(ledger) if e.get("event") == "user_decision_requested"
        and e.get("decision_id") == decision_id and e.get("task_id") == task_id
        and e.get("process_id") == process.process_id and e.get("kind") == "completion_exception"), None)
    answer = next((e for e in reversed(ledger) if e.get("event") == "user_decision_received"
        and e.get("decision_id") == decision_id and e.get("task_id") == task_id
        and e.get("process_id") == process.process_id), None)
    if not request or not answer or answer.get("selected_action") != "accept_partial":
        return False
    if request.get("contract_digest") != exception["contract_digest"]:
        return False
    accepted = set(exception.get("gate_ids") or [])
    requested = set(request.get("completion_gate_ids") or [])
    return accepted == requested and {g["gate_id"] for g in gates}.issubset(accepted)
