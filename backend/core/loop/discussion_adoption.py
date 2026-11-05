"""Immutable read-only BTW results and explicit, task-bound adoption commands."""
from __future__ import annotations

import json
from .mechanism_catalog import digest

NAMESPACE = "btw.discussions.v1"


def inherit_interrupted_task(successor, predecessor):
    """Continue the same adopted task on a fresh execution lease, not a new goal.

    Keep original receipt/task identities. No permissions, pending calls or
    consumed advancement tickets are transferred or replayed.
    """
    from copy import deepcopy
    if (successor.active_task_id != predecessor.active_task_id
            or successor.session.session_id != predecessor.session.session_id
            or predecessor.status.value not in {"cancelled", "killed", "failed", "timed_out", "completed"}):
        raise ValueError("BTW_ADOPTION_CONTINUATION_CHANGED")
    successor.tool_receipts = deepcopy(predecessor.tool_receipts)
    successor.required_test_ids = list(predecessor.required_test_ids)
    successor.task_constraints = list(predecessor.task_constraints)
    successor.task_kind = predecessor.task_kind
    successor.task_description = predecessor.task_description
    _, contracts, _ = predecessor.coordination_snapshot()
    with successor._coordination_lock:
        for child_id, contract in contracts.items():
            if child_id in successor.delegated_contracts:
                successor.delegated_contracts[child_id]["required_for_parent_completion"] = bool(
                    contract.get("required_for_parent_completion", True))
    def rebind(context):
        updated = dict(context)
        alignment = deepcopy(updated.get("alignment_runtime") or {})
        if alignment:
            alignment.update(process_id=successor.process_id)
            alignment.pop("delivery", None)
            alignment.pop("admission_review", None)
            updated["alignment_runtime"] = alignment
        return updated
    successor.update_context_snapshot(rebind)
    successor.session.host_ledger.append({"event": "adopted_task_execution_continued",
        "task_id": successor.active_task_id, "process_id": successor.process_id,
        "predecessor_process_id": predecessor.process_id,
        "receipt_ids": [str(r.get("receipt_id") or "") for r in successor.tool_receipts]})


def save_discussion(storage, identifier, question, result, sources):
    if not identifier or len(identifier) > 128:
        raise ValueError("invalid discussion identity")
    head = "head/" + identifier
    previous_ref = storage.get_state_ref(NAMESPACE, head)
    previous = json.loads(storage.read_blob(previous_ref)) if previous_ref else {}
    revision = int(previous.get("revision", 0)) + 1
    record = {"schema_version": 1, "sidecar_id": identifier, "revision": revision,
        "project_id": storage.paths.project_id, "question": question,
        "answer": result["answer"], "status": result["status"], "read_only": True,
        "targets": {source["process_id"]: {"task_id": source.get("task_id"),
            "contract_revision": source.get("contract_revision", 0)} for source in sources},
        "previous_ref": previous_ref}
    ref = storage.put_blob(json.dumps(record, ensure_ascii=False, sort_keys=True).encode(), media_type="application/json")
    if not storage.commit_state_revision(NAMESPACE, head, ref, expected_ref=previous_ref,
                                         revision_key=f"revision/{identifier}/{revision}"):
        raise ValueError("BTW_DISCUSSION_REVISION_CONFLICT")
    return {"discussion_ref": ref, "discussion_revision": revision}


def adopt_discussion(process, storage, *, sidecar_id, discussion_ref, expected_task_id, expected_contract_revision, store):
    if store is None:
        raise ValueError("BTW_ADOPTION_CHECKPOINT_UNAVAILABLE")
    if not discussion_ref or not sidecar_id:
        raise ValueError("adoption requires the exact completed discussion reference")
    record = json.loads(storage.read_blob(discussion_ref))
    if (record.get("schema_version") != 1 or record.get("project_id") != storage.paths.project_id
            or record.get("sidecar_id") != sidecar_id or record.get("status") != "completed"
            or not record.get("read_only")):
        raise ValueError("BTW_DISCUSSION_NOT_ADOPTABLE")
    pinned = storage.get_state_ref(NAMESPACE, f"revision/{sidecar_id}/{record['revision']}")
    if pinned != discussion_ref:
        raise ValueError("BTW_DISCUSSION_SOURCE_UNREGISTERED")
    target = record.get("targets", {}).get(process.process_id)
    if target is None or target.get("task_id") != expected_task_id:
        raise ValueError("BTW_ADOPTION_TARGET_CHANGED")
    # A newer discussion cannot silently substitute for the answer the user saw.
    identifier = "btw-" + digest([discussion_ref, process.process_id, expected_task_id])[:24]
    from .task_maintenance import add_change
    change = add_change(process, "BTW discussion:\nQuestion: " + record["question"] + "\nConclusion: " + record["answer"],
        identifier=identifier, source_ref=discussion_ref, expected_task_id=expected_task_id,
        expected_contract_revision=expected_contract_revision, store=store, source_contract_revision=target.get("contract_revision"))
    return {"status": change["status"], "change": change, "authority_granted": False}
