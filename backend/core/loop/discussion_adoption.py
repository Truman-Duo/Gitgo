"""Immutable read-only BTW results and explicit, task-bound adoption commands."""
from __future__ import annotations

import json
from .mechanism_catalog import digest

NAMESPACE = "btw.discussions.v1"


def root_budget_source(manager, origin, *, task_id, session, parent_id, actor_kind):
    """Validate a Host-adopted same-task root before allocating a new lease."""
    from .task_maintenance import state_for
    source = manager.get(str(origin.get('process_id') or ''))
    error = 'BTW_ADOPTION_CONTINUATION_CHANGED'
    if (source is None or parent_id is not None or actor_kind != 'supervisor'
            or source.parent_id is not None or source.actor_kind != 'supervisor'
            or session is not source.session or task_id != source.active_task_id
            or origin.get('task_id') != task_id):
        raise ValueError(error)
    thread = manager._threads.get(source.process_id)
    if (source.status.value not in {'cancelled', 'killed', 'failed', 'timed_out', 'completed'}
            or not source.lifecycle_finalized or source._run_lock.locked()
            or (thread is not None and thread.is_alive())
            or (source.recovery or {}).get('resume_forbidden')
            or source.session.mechanism_activation_blocked):
        raise ValueError('BTW_ADOPTION_CLOSEOUT_REQUIRED')
    if any(e.get('event') == 'adopted_task_execution_continued'
           and e.get('predecessor_process_id') == source.process_id
           and e.get('task_id') == task_id for e in session.host_ledger):
        raise ValueError('BTW_ADOPTION_ALREADY_CONTINUED')
    if any(e.get('event') == 'adopted_task_execution_continued'
           and e.get('process_id') == source.process_id and e.get('task_id') == task_id
           and e.get('budget_task_id') != task_id for e in session.host_ledger):
        # Legacy successors already reset their ledger. Do not pretend that
        # this incomplete accounting can be repaired by preserving its zeroes.
        raise ValueError('BTW_ADOPTION_BUDGET_UNAVAILABLE')
    change = state_for(source).get('changes', {}).get(origin.get('change_id'))
    if (not change or change.get('status') != 'pending' or not change.get('user_adopted')
            or change.get('target_task_id') != task_id or change.get('target_process_id') != source.process_id):
        raise ValueError(error)
    storage = getattr(manager, 'storage', None) or getattr(getattr(manager, 'session_store', None), '_storage', None)
    if storage is None:
        raise ValueError('BTW_ADOPTION_CHECKPOINT_UNAVAILABLE')
    ref = change.get('source_ref')
    record = json.loads(storage.read_blob(ref))
    if (record.get('schema_version') != 1 or record.get('project_id') != storage.paths.project_id
            or record.get('status') != 'completed' or not record.get('read_only')
            or storage.get_state_ref(NAMESPACE, f"revision/{record.get('sidecar_id')}/{record.get('revision')}") != ref):
        raise ValueError('BTW_DISCUSSION_SOURCE_UNREGISTERED')
    # An unchanged pending adoption can survive another interrupted execution.
    # Its registered source still names the original process, not the new lease.
    ancestors = {source.process_id}
    while True:
        prior = {e.get('predecessor_process_id') for e in session.host_ledger
                 if e.get('event') == 'adopted_task_execution_continued'
                 and e.get('task_id') == task_id and e.get('process_id') in ancestors}
        if prior <= ancestors: break
        ancestors.update(prior)
    if not any(target.get('task_id') == task_id and pid in ancestors
               and change['id'] == 'btw-' + digest([ref, pid, task_id])[:24]
               for pid, target in (record.get('targets') or {}).items()):
        raise ValueError(error)
    budget = source.task_budget
    lease = (budget.snapshot().get('process_leases', {}).get(source.process_id, {}) if budget else {})
    if budget is None or budget.task_id != task_id or lease.get('state') not in {'active', 'released'} or lease.get('kind') != 'root':
        raise ValueError('BTW_ADOPTION_BUDGET_UNAVAILABLE')
    return source


def record_continuation(successor, predecessor):
    """Record the Host allocation once, before any admission checkpoint can fail."""
    existing = next((e for e in successor.session.host_ledger
        if e.get('event') == 'adopted_task_execution_continued'
        and e.get('process_id') == successor.process_id), None)
    if existing is not None:
        return
    event = {"event": "adopted_task_execution_continued",
        "task_id": successor.active_task_id, "process_id": successor.process_id,
        "predecessor_process_id": predecessor.process_id,
        "receipt_ids": [str(r.get("receipt_id") or "") for r in predecessor.tool_receipts]}
    if successor.task_budget is not None and successor.task_budget is predecessor.task_budget:
        event['budget_task_id'] = successor.task_budget.task_id
    successor.session.host_ledger.append(event)


def retain_failed_continuation(process, error):
    """A published admission is visible but cannot run after storage failure."""
    from backend.core.storage import StorageCommitUnknown
    from .models import ProcessStatus
    if isinstance(error, StorageCommitUnknown):
        from .lifecycle import block_uncertain_checkpoint
        block_uncertain_checkpoint(process)
    with process._coordination_lock:
        process.status = ProcessStatus.RECOVERY_REVIEW_REQUIRED
        reasons = list((process.recovery or {}).get('reasons') or [])
        reason = {'code': 'ADOPTED_CONTINUATION_ADMISSION_FAILED'}
        if reason not in reasons:
            reasons.append(reason)
        process.recovery = {**(process.recovery or {}),
            'state': 'recovery_review_required', 'resume_forbidden': True,
            'requires_manual_verification': True, 'reasons': reasons}


def checkpoint_retained_continuation(manager, process):
    """Best-effort recovery flag; the prior admission checkpoint stays factual."""
    store = getattr(manager, 'session_store', None)
    result = {'process_id': process.process_id, 'task_id': process.active_task_id,
              'admission_retained': True, 'recovery_checkpointed': False}
    try:
        if store is None:
            raise ValueError('continuation checkpoint store unavailable')
        store.save_process_checkpoint(process)
        result['recovery_checkpointed'] = True
    except Exception as exc:
        result['persistence_error'] = f'{type(exc).__name__}: {exc}'
    return result


def budget_predecessor_id(state):
    """Only Host-marked shared-budget receipts can join root recovery groups."""
    process = state.get('process') or {}
    budget = (state.get('runtime_state') or {}).get('task_budget') or {}
    if process.get('parent_process_id') or process.get('actor_kind') != 'supervisor':
        return ''
    ledger = (state.get('session') or {}).get('host_ledger') or []
    for entry in reversed(ledger):
        if (entry.get('event') == 'adopted_task_execution_continued'
                and entry.get('process_id') == process.get('process_id')
                and entry.get('task_id') == process.get('task_id') == budget.get('task_id')
                and entry.get('budget_task_id') == budget.get('task_id')):
            return str(entry.get('predecessor_process_id') or '')
    return ''


def shared_budget_predecessor(state, states):
    predecessor_id = budget_predecessor_id(state)
    if not predecessor_id:
        return ''
    previous = states.get(predecessor_id)
    if not previous:
        raise ValueError('CONTINUATION_STATE_UNAVAILABLE: adopted budget predecessor missing')
    old, current = previous['process'], state['process']
    budget = state['runtime_state']['task_budget']
    prior_budget = (previous.get('runtime_state') or {}).get('task_budget') or {}
    leases = budget.get('process_leases') or {}
    if (old.get('parent_process_id') or old.get('actor_kind') != 'supervisor'
            or old.get('task_id') != current.get('task_id')
            or previous.get('session', {}).get('session_id') != state.get('session', {}).get('session_id')
            or old.get('status') not in {'cancelled', 'killed', 'failed', 'timed_out', 'completed'}
            or previous.get('runtime_state', {}).get('lifecycle_finalized') is not True
            or prior_budget.get('task_id') != budget.get('task_id')
            or leases.get(predecessor_id, {}).get('state') != 'released'
            or leases.get(current.get('process_id'), {}).get('kind') != 'root'):
        raise ValueError('CONTINUATION_STATE_UNAVAILABLE: adopted budget lineage changed')
    return predecessor_id


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
    record_continuation(successor, predecessor)


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
