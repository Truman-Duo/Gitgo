"""Host-owned Agent relationship and coordination protocol.

The protocol deliberately separates governance ownership from execution edges.
Workers never address each other directly: dependency updates are observed,
versioned and routed by the Host, while semantic boundary changes are resolved
by the governing supervisor (and, when needed, the user).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RELATIONSHIP_SCHEMA_VERSION = 1
COORDINATION_EVENT_SCHEMA_VERSION = 1
MAX_PROJECTED_EVENTS = 64

_ACTIVE_STATUSES = {
    "running", "waiting", "awaiting_user", "cancelling", "recovering",
    "resume_available", "recovery_review_required",
}


def relationship_policy(
    *,
    owner_process_id: str,
    depends_on: list[str] | None = None,
    continuation_process_id: str = "",
    required_for_parent_completion: bool = True,
    capability_profile_id: str = "",
    task_kind: str = "answer",
) -> dict[str, Any]:
    """Return one explicit, serialisable relationship/communication policy."""
    dependencies = list(dict.fromkeys(str(item) for item in (depends_on or []) if str(item)))
    if continuation_process_id:
        context_mode = "continue_session"
    elif dependencies:
        context_mode = "upstream_handoff"
    elif not required_for_parent_completion:
        context_mode = "advisory_snapshot"
    else:
        context_mode = "task_snapshot"
    return {
        "schema_version": RELATIONSHIP_SCHEMA_VERSION,
        "ownership": {
            "owner_process_id": str(owner_process_id),
            "worker_may_change_task_boundary": False,
        },
        "execution": {
            "depends_on": dependencies,
            "dependency_updates": "host_routed_versioned_events",
        },
        "context_transfer": {
            "mode": context_mode,
            "copies_parent_reasoning": False,
            "copies_parent_tool_receipts": False,
        },
        "communication": {
            "private_peer_chat": False,
            "supervisor_to_worker": "safe_boundary_mailbox",
            "worker_to_supervisor": "host_coordination_event",
            "worker_to_worker": "host_dependency_projection_only",
            "user_questions": "host_question_broker",
        },
        "completion": {
            "required_for_owner": bool(required_for_parent_completion),
        },
        "capability_profile_id": str(capability_profile_id),
        "task_kind": str(task_kind),
    }


def current_coordination_events(ledger: list[dict] | None) -> list[dict]:
    """Project current event state from the append-only supervisor ledger."""
    events: dict[str, dict] = {}
    order: list[str] = []
    for raw in list(ledger or []):
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("event") or "")
        event_id = str(raw.get("coordination_event_id") or raw.get("event_id") or "")
        if not event_id:
            continue
        if kind == "agent_coordination_event":
            if event_id not in events:
                order.append(event_id)
            events[event_id] = {
                key: value for key, value in raw.items() if key != "event"
            }
        elif kind == "agent_coordination_resolution" and event_id in events:
            events[event_id] = {
                **events[event_id],
                "status": str(raw.get("status") or "resolved"),
                "resolution": {
                    key: value for key, value in raw.items()
                    if key not in {"event", "coordination_event_id", "event_id"}
                },
            }
    return project_draft_versions([events[event_id] for event_id in order])


def project_draft_versions(events: list[dict]) -> list[dict]:
    latest = {}
    for event in events:
        if event.get("draft"):
            key = (event["owner_process_id"], event["source_task_id"], event["source_process_id"])
            latest[key] = event["draft"]
    projected = []
    for event in events:
        if draft := event.get("draft"):
            key = (event["owner_process_id"], event["source_task_id"], event["source_process_id"])
            current = latest[key]["id"]
            event = {**event, "current_draft_id": current}
            if current != draft["id"] and event.get("status") in {"pending", "awaiting_supervisor", "draft_adopted"}:
                event["status"] = "superseded"
        projected.append(event)
    return projected


def pending_coordination_events(ledger: list[dict] | None) -> list[dict]:
    return [
        item for item in current_coordination_events(ledger)
        if str(item.get("status") or "pending") in {
            "pending", "awaiting_supervisor", "awaiting_user", "rework_requested",
        }
    ]


def _pending_user_change_for_wait(supervisor, selected: set[str]) -> dict | None:
    from .assignment_handoff import withdrawal_source
    from .lifecycle import ACTIVE

    task_id = supervisor.active_task_id or supervisor.process_id
    context, _ = supervisor.read_context_snapshot()
    state = context.get("task_maintenance") or {}
    current = state.get("task_id") == task_id and state.get("phase") == "needs_review"
    pending = {
        (task_id, change_id, change.get("authority_sequence", 0), change.get("base_contract_revision", 0)): change
        for change_id, change in state.get("changes", {}).items()
        if change.get("status") == "pending" and change.get("target_task_id") == task_id
    } if current else {}
    # Transient notification deduplication only. The task record continues to
    # own whether each change has been resolved. wait_many serializes callbacks.
    seen = supervisor._wait_feedback_notices
    seen.intersection_update(pending)
    unseen = pending.keys() - seen
    manager = getattr(supervisor, "_manager", None)
    mailbox = supervisor.mailbox
    if not unseen or not selected or manager is None or mailbox is None:
        return None
    affected = []
    with supervisor._coordination_lock:
        for child_id in sorted(selected):
            contract = supervisor.delegated_contracts.get(child_id)
            child = manager.get(child_id)
            # This is the inherited maintenance boundary for live direct
            # assignments. The model still interprets what to change.
            if (child and child.parent_id == supervisor.process_id and child.status.value in ACTIVE
                    and contract is not None and not (contract.get("superseded_by") or contract.get("returned_assignment"))):
                affected.append(child_id)
    if not affected:
        return None
    changes = []
    for key, change in pending.items():
        if key not in unseen or not key[1].startswith("feedback-"):
            continue
        message = mailbox.get(key[1].removeprefix("feedback-"))
        if (message and message.status == "applied" and message.applied_task_id == task_id
                and withdrawal_source(supervisor, change)):
            changes.append(key)
            if len(changes) == 8:
                break
    if not changes:
        return None
    seen.update(changes)
    return {"reason": "user_change_pending", "change_ids": [key[1] for key in changes],
            "affected_process_ids": affected,
            "next_actions": ["declare_task_contract", "task_mechanism"]}


def supervisor_wait_interruption(supervisor, process_ids: list[str]) -> dict | None:
    """Return only actual unresolved owner actions relevant to the selected wait.

    Notification is not approval. Rework already requested is left to the worker;
    resolved events and unrelated siblings must not make a normal wait spin.
    """
    if supervisor.mailbox is not None and supervisor.mailbox.has_pending_instruction():
        return {"reason": "instruction_pending"}
    selected = set(process_ids)
    if feedback := _pending_user_change_for_wait(supervisor, selected):
        return feedback
    with supervisor._coordination_lock:
        events = pending_coordination_events(supervisor.session.host_ledger)
    actionable = [event for event in events
        if event.get("owner_process_id") == supervisor.process_id
        and event.get("requires_supervisor_action")
        and event.get("status") in {"pending", "awaiting_supervisor", "awaiting_user"}
        and selected.intersection([event.get("source_process_id"),
                                   *list(event.get("affected_process_ids") or [])])]
    if actionable:
        return {"reason": "supervisor_action_required",
                "event_ids": [event["coordination_event_id"] for event in actionable],
                "source_process_ids": list(dict.fromkeys(event["source_process_id"] for event in actionable)),
                "next_actions": ["list_coordination_events", "resolve_coordination_event"]}
    from .task_maintenance import retained_execution_block
    from .lifecycle import ACTIVE
    manager = getattr(supervisor, "_manager", None)
    if not manager or not selected or not (blocked := retained_execution_block(supervisor)):
        return None
    key = (supervisor.active_task_id or supervisor.process_id, blocked[0])
    if supervisor._wait_maintenance_notice == key:
        return None
    with supervisor._coordination_lock:
        affected = [
            child_id for child_id in sorted(selected)
            if (child := manager.get(child_id)) is not None and child.parent_id == supervisor.process_id
            and child.status.value in ACTIVE and child.task_kind == "action"
            and (contract := supervisor.delegated_contracts.get(child_id, {})).get("continuation", {}).get("retained_obligations")
            and not (contract.get("superseded_by") or contract.get("returned_assignment"))
        ]
    if not affected:
        return None
    supervisor._wait_maintenance_notice = key
    return {"reason": "supervisor_maintenance_required", "owner_process_id": supervisor.process_id,
            "source_process_ids": affected, "message": blocked[1], "next_actions": ["declare_task_contract"]}


def coordination_summary(process) -> dict[str, Any]:
    context, _version = process.read_context_snapshot()
    events = project_draft_versions(list(context.get("coordination_events") or []))
    blocks = list(context.get("coordination_blocks") or [])
    return {
        "pending_count": sum(
            1 for item in events
            if str(item.get("status") or "pending") in {
                "pending", "awaiting_supervisor", "awaiting_user", "rework_requested",
            }
        ),
        "blocked_by_event_ids": blocks,
        "latest": events[-8:],
    }


def coordination_summary_from_context(context: dict | None) -> dict[str, Any]:
    """Build the same lightweight projection for restored/offline runtimes."""
    snapshot = dict(context or {})
    events = project_draft_versions(list(snapshot.get("coordination_events") or []))
    blocks = [str(item) for item in list(snapshot.get("coordination_blocks") or []) if str(item)]
    return {
        "pending_count": sum(
            1 for item in events
            if str(item.get("status") or "pending") in {
                "pending", "awaiting_supervisor", "awaiting_user", "rework_requested",
            }
        ),
        "blocked_by_event_ids": blocks,
        "latest": events[-8:],
    }


def _governing_root(manager, process):
    current = process
    seen: set[str] = set()
    while current is not None and getattr(current, "parent_id", None):
        if current.process_id in seen:
            break
        seen.add(current.process_id)
        parent = manager.get(current.parent_id)
        if parent is None:
            break
        current = parent
    return current


def coordination_owner(manager, process):
    """Resolve the existing ownership graph; a standalone worker has no owner."""
    if manager is None or process is None:
        return None
    owner = _governing_root(manager, process)
    if (owner is None or owner.process_id == process.process_id
            or getattr(owner, "parent_id", None)
            or getattr(owner, "actor_kind", None) != "supervisor"):
        return None
    return owner


class CoordinationOwnerUnavailable(ValueError):
    """Ownership changed or no supervisor ever owned this worker."""


def ownership_error():
    from backend.core.errors import error_payload
    return {"accepted": False, **error_payload("COORDINATION_OWNER_UNAVAILABLE",
                details={"fault_domain": "gitgo_runtime", "component": "coordination"}),
            "authority_granted": False}


def _append_projection(process, event: dict, *, block: bool = False) -> None:
    event_id = str(event.get("coordination_event_id") or "")

    def update(context: dict) -> dict:
        updated = dict(context)
        projected = [
            dict(item) for item in list(updated.get("coordination_events") or [])
            if str(item.get("coordination_event_id") or "") != event_id
        ]
        projected.append(dict(event))
        updated["coordination_events"] = projected[-MAX_PROJECTED_EVENTS:]
        blocks = [str(item) for item in list(updated.get("coordination_blocks") or []) if str(item)]
        if block and event_id and event_id not in blocks:
            blocks.append(event_id)
        updated["coordination_blocks"] = blocks
        return updated

    process.update_context_snapshot(update)


def _format_notice(event: dict, *, audience: str) -> str:
    event_id = str(event.get("coordination_event_id") or "")
    kind = str(event.get("kind") or "coordination_update")
    summary = str(event.get("summary") or "")
    interfaces = ", ".join(str(item) for item in event.get("affected_interfaces") or [])
    lines = [
        f"[HOST COORDINATION EVENT {event_id}]",
        f"kind={kind}",
        f"source_process_id={event.get('source_process_id', '')}",
        f"summary={summary}",
    ]
    if interfaces:
        lines.append(f"affected_interfaces={interfaces}")
    if draft := event.get("draft"):
        lines.append(f"Draft v{draft['version']} ({draft['id']}), commit {draft['commit']}. A draft is not a delivery.")
    if audience == "supervisor":
        if event.get("scope_change_id"):
            lines.append("The draft is bound to the adopted withdrawal. Close out its prior executor, "
                         "then call delegate_task with this draft_id and the remaining work.")
        lines.append(
            "This is a Host-routed event, not a private worker message. Inspect it with "
            "list_coordination_events and resolve it with resolve_coordination_event. "
            "If product judgement is genuinely required, ask the user with request_user_decision."
        )
    else:
        lines.append(
            "Do not contact another worker directly. The Host has notified the governing "
            "supervisor; follow only the resolved interface revision or supervisor feedback."
        )
    return "\n".join(lines)


def _enqueue_notice(process, event: dict, *, audience: str) -> dict:
    mailbox = getattr(process, "mailbox", None)
    status = str(getattr(getattr(process, "status", None), "value", ""))
    if mailbox is None or status not in _ACTIVE_STATUSES:
        return {"process_id": process.process_id, "queued": False, "reason": "not_live"}
    try:
        message = mailbox.enqueue_coordination_update(
            _format_notice(event, audience=audience)
        )
    except Exception as exc:
        return {"process_id": process.process_id, "queued": False, "reason": str(exc)}
    return {"process_id": process.process_id, "queued": True, **message.to_dict()}


def publish_coordination_event(
    manager,
    source,
    *,
    kind: str,
    summary: str,
    details: dict | None = None,
    affected_process_ids: list[str] | None = None,
    affected_interfaces: list[str] | None = None,
    requires_supervisor_action: bool = True,
    block_affected: bool = False,
    offer_draft: bool = False,
    preserve_closed: bool = False,
) -> dict:
    """Record once and route through Host-owned owner/dependency relations."""
    owner = coordination_owner(manager, source)
    if owner is None:
        raise CoordinationOwnerUnavailable("coordination events require a worker owned by a supervisor")
    allowed_children = {item.process_id for item in manager.children_of(owner.process_id)}
    interface_refs = list(dict.fromkeys(
        str(item) for item in (affected_interfaces or []) if str(item)
    ))
    requested_affected = list(affected_process_ids or [])
    if offer_draft:
        requested_affected.extend(item.process_id for item in manager.downstream_of(source.process_id))
    if not requested_affected and (interface_refs or kind == "dependency_change"):
        for candidate in manager.downstream_of(source.process_id):
            if not interface_refs:
                requested_affected.append(candidate.process_id)
                continue
            candidate_context, _ = candidate.read_context_snapshot()
            candidate_contract = dict(candidate_context.get("task_contract") or {})
            if set(interface_refs) & set(candidate_contract.get("input_interfaces") or []):
                requested_affected.append(candidate.process_id)
    affected = list(dict.fromkeys(
        str(item) for item in requested_affected
        if str(item) in allowed_children and str(item) != source.process_id
    ))
    event_id = str(uuid.uuid4())
    event = {
        "schema_version": COORDINATION_EVENT_SCHEMA_VERSION,
        "coordination_event_id": event_id,
        "kind": str(kind),
        "status": "awaiting_supervisor" if requires_supervisor_action else "notified",
        "source_process_id": source.process_id,
        "source_task_id": source.active_task_id or source.process_id,
        "source_display_name": str(getattr(source.session, "display_name", "") or ""),
        "owner_process_id": owner.process_id,
        "affected_process_ids": affected,
        "affected_interfaces": interface_refs,
        "summary": str(summary).strip(),
        "details": dict(details or {}),
        "requires_supervisor_action": bool(requires_supervisor_action),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "route": "worker_to_host_to_supervisor_and_dependencies",
    }
    if offer_draft:
        from .assignment_handoff import (
            AssignmentHandoffError, assignment_basis, bind_pending_scope, capture_draft, checkpoint, inspect_assignment, latest_draft,
        )
        from .mechanism_lifecycle import fence
        if preserve_closed:
            event["route"] = "runtime_preserved_on_assignment_closeout"
        draft = capture_draft(manager, owner, event, preserve_closed=preserve_closed)
        with fence(owner), owner._context_lock, owner._coordination_lock:
            child, contract, _ = inspect_assignment(manager, owner, event, closeout=preserve_closed, versioned=True)
            if draft["basis"] != assignment_basis(owner, child, contract):
                raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_BASIS_CHANGED", "The assignment changed while capturing this draft.")
            previous = latest_draft(owner, source.process_id)
            draft["version"] = previous["version"] + 1 if previous else 1
            event["draft"] = draft
            original, _ = owner.read_context_snapshot()
            if change_id := bind_pending_scope(owner, child, contract, draft):
                event["scope_change_id"] = change_id
            _append_projection(owner, event)
            checkpoint(manager, owner, {"event": "agent_coordination_event", **event},
                       lambda: owner.update_context_snapshot(lambda _previous: original))
    else:
        with owner._coordination_lock:
            owner.session.host_ledger.append({"event": "agent_coordination_event", **event})
        _append_projection(owner, event)
    _append_projection(source, event)
    deliveries = [
        _enqueue_notice(source, event, audience="worker"),
        _enqueue_notice(owner, event, audience="supervisor"),
    ]
    for process_id in ([] if offer_draft else affected):
        target = manager.get(process_id)
        if target is None:
            continue
        _append_projection(target, event, block=block_affected)
        deliveries.append(_enqueue_notice(target, event, audience="worker"))
    manager.notify_state_changed(source.process_id)
    return {**event, "deliveries": deliveries}


def _contract_interface(contract: dict, ref: str) -> dict | None:
    for item in list(contract.get("interfaces") or []):
        if str(item.get("ref") or "") == ref:
            return item
    return None


def _inspect_interface(workspace_path: str, ref: str) -> dict:
    from backend.core.loop.interface_contract import (
        _inspect_declared_interface,
        normalise_interface_ref,
    )

    normalized = normalise_interface_ref(ref)
    file_name, symbol = normalized.rsplit(":", 1)
    workspace = Path(workspace_path).resolve()
    path = (workspace / file_name).resolve()
    path.relative_to(workspace)
    present, signature = _inspect_declared_interface(path, symbol)
    canonical = json.dumps(
        {"ref": normalized, "present": present, "signature": signature},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return {
        "ref": normalized,
        "file": file_name,
        "symbol": symbol,
        "present": bool(present),
        "signature": str(signature or ""),
        "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _pending_interface_event(owner, ref: str) -> dict | None:
    for item in reversed(pending_coordination_events(owner.session.host_ledger)):
        if (
            item.get("kind") == "interface_revision_proposed"
            and ref in list(item.get("affected_interfaces") or [])
        ):
            return item
    return None


def observe_interface_changes(
    manager,
    source,
    workspace_path: str,
    *,
    summary: str = "",
    compatibility: str = "unknown",
    requested_refs: list[str] | None = None,
) -> dict:
    """Detect output-interface drift and create a supervisor-owned revision proposal."""
    context, _version = source.read_context_snapshot()
    task_contract = dict(context.get("task_contract") or {})
    contract = dict(task_contract.get("interface_contract") or {})
    declared = list(task_contract.get("output_interfaces") or [])
    if requested_refs:
        from backend.core.loop.interface_contract import normalise_interface_ref
        requested = {normalise_interface_ref(str(item)) for item in requested_refs}
        declared = [
            item for item in declared
            if normalise_interface_ref(str(item)) in requested
        ]
    if not declared:
        return {"observed": [], "events": [], "reason": "no_declared_output_interfaces"}
    owner = coordination_owner(manager, source)
    if owner is None:
        return {"observed": [], "events": [], "reason": "no_supervisor_owner"}

    observed = [_inspect_interface(workspace_path, str(ref)) for ref in declared]
    events = []
    for document in observed:
        ref = document["ref"]
        frozen = _contract_interface(contract, ref)
        if frozen is None:
            continue
        expected_present = bool(frozen.get("initially_present"))
        expected_signature = str(frozen.get("frozen_signature") or "")
        changed = (
            document["present"] != expected_present
            or (expected_present and expected_signature != document["signature"])
        )
        existing = _pending_interface_event(owner, ref)
        if not changed:
            if existing is not None:
                resolve_coordination_event(
                    manager, owner, str(existing["coordination_event_id"]),
                    disposition="restored", note="Interface returned to the frozen contract.",
                )
            continue
        if existing is not None and dict(existing.get("details") or {}).get("proposed", {}).get("digest") == document["digest"]:
            continue
        if existing is not None:
            # One interface has one active proposal. A newer observation
            # supersedes the old candidate atomically in the Host ledger.
            resolve_coordination_event(
                manager, owner, str(existing["coordination_event_id"]),
                disposition="dismiss",
                note="Superseded by a newer observed interface revision.",
            )
        downstream = []
        for candidate in manager.downstream_of(source.process_id):
            candidate_context, _ = candidate.read_context_snapshot()
            inputs = list(dict(candidate_context.get("task_contract") or {}).get("input_interfaces") or [])
            if ref in inputs:
                downstream.append(candidate.process_id)
        # Capture the candidate through the existing read boundary and immutable
        # context store. A can inspect this exact proposal before B is sealed;
        # no outside-workspace read grant or mutable peer file access is issued.
        from .context_store import ContextObjectStore
        from backend.core.sandbox_read_plan import open_planned_read
        candidate = {"process_id": source.process_id, "path": document["file"],
                     "interface": document, "content": None, "file_sha256": None}
        try:
            with open_planned_read(Path(workspace_path).resolve(), document["file"]) as opened:
                raw = opened.read_bytes(max_bytes=256 * 1024)
            candidate.update(content=raw.decode("utf-8", errors="replace"),
                             file_sha256=hashlib.sha256(raw).hexdigest(), state="captured")
        except (OSError, ValueError):
            # Missing/oversized/unsafe candidates remain proposals with explicit
            # incomplete evidence. Never turn this into an outside-scope grant.
            candidate["state"] = "unavailable"
        snapshot = ContextObjectStore(owner.worktree_path or workspace_path).put(
            "coordination/interface-" + uuid.uuid4().hex, candidate,
            metadata={"authority": "observed_candidate", "producer": "interface_observation"})
        event = publish_coordination_event(
            manager,
            source,
            kind="interface_revision_proposed",
            summary=summary or f"Declared interface {ref} changed and needs supervisor resolution.",
            details={
                "expected": {
                    "present": expected_present,
                    "signature": expected_signature,
                },
                "proposed": document,
                "compatibility": compatibility,
                "artifact_ref": snapshot["pinned"],
            },
            affected_process_ids=downstream,
            affected_interfaces=[ref],
            requires_supervisor_action=True,
            block_affected=bool(downstream),
        )
        events.append(event)
    return {"observed": observed, "events": events}


def _replace_interface_baseline(process, ref: str, proposed: dict) -> None:
    def update(context: dict) -> dict:
        updated = dict(context)
        task_contract = dict(updated.get("task_contract") or {})
        contract = dict(task_contract.get("interface_contract") or {})
        interfaces = [dict(item) for item in list(contract.get("interfaces") or [])]
        for item in interfaces:
            if str(item.get("ref") or "") == ref:
                item["initially_present"] = bool(proposed.get("present"))
                item["frozen_signature"] = str(proposed.get("signature") or "")
                item["revision_digest"] = str(proposed.get("digest") or "")
        contract["interfaces"] = interfaces
        canonical = json.dumps(interfaces, ensure_ascii=False, sort_keys=True, default=str)
        contract["contract_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        task_contract["interface_contract"] = contract
        updated["task_contract"] = task_contract
        return updated

    process.update_context_snapshot(update)


def _update_projected_event(process, event_id: str, *, status: str, resolution: dict) -> None:
    def update(context: dict) -> dict:
        updated = dict(context)
        projected = []
        for raw in list(updated.get("coordination_events") or []):
            item = dict(raw)
            if str(item.get("coordination_event_id") or "") == event_id:
                item["status"] = status
                item["resolution"] = dict(resolution)
            projected.append(item)
        updated["coordination_events"] = projected[-MAX_PROJECTED_EVENTS:]
        if status in {"resolved", "accepted", "restored", "dismissed"}:
            updated["coordination_blocks"] = [
                str(item) for item in list(updated.get("coordination_blocks") or [])
                if str(item) != event_id
            ]
        return updated

    process.update_context_snapshot(update)


def resolve_coordination_event(
    manager,
    supervisor,
    event_id: str,
    *,
    disposition: str,
    note: str = "",
) -> dict:
    """Resolve or advance one event; only its governing supervisor may do so."""
    events = current_coordination_events(supervisor.session.host_ledger)
    event = next((
        item for item in events
        if str(item.get("coordination_event_id") or "") == str(event_id)
    ), None)
    if event is None or str(event.get("owner_process_id") or "") != supervisor.process_id:
        raise KeyError("COORDINATION_EVENT_NOT_FOUND")
    valid = {
        "accept_revision", "request_rework", "ask_user", "acknowledge",
        "dismiss", "restored", "take_remaining_work", "accept_draft", "complete_returned_work",
    }
    if disposition not in valid:
        raise ValueError("COORDINATION_RESOLUTION_INVALID")
    if event.get("kind") == "interface_revision_proposed" and disposition == "acknowledge":
        raise ValueError("COORDINATION_RESOLUTION_INVALID")
    if event.get("draft") and disposition in {"accept_revision", "restored"}:
        raise ValueError("COORDINATION_RESOLUTION_INVALID")

    if disposition in {"accept_revision", "take_remaining_work", "complete_returned_work"}:
        status = "accepted"
    elif disposition == "accept_draft":
        status = "draft_adopted"
    elif disposition == "request_rework":
        status = "rework_requested"
    elif disposition == "ask_user":
        status = "awaiting_user"
    elif disposition == "restored":
        status = "restored"
    elif disposition == "dismiss":
        status = "dismissed"
    else:
        status = "resolved"
    resolution = {
        "disposition": disposition,
        "note": str(note).strip(),
        "resolved_by_process_id": supervisor.process_id,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    entry = {"event": "agent_coordination_resolution", "coordination_event_id": event_id,
             "status": status, **resolution}
    returned = None
    accepted_result = None
    if disposition in {"take_remaining_work", "accept_draft", "complete_returned_work"}:
        from .assignment_handoff import (
            AssignmentHandoffError, checkpoint, inspect_return, latest_draft, retain_draft, validate_draft,
        )
        from .mechanism_lifecycle import fence
        child_id = event["source_process_id"]
        version = event.get("draft")
        existing = (supervisor.delegated_contracts.get(child_id) or {}).get("returned_assignment")
        if (disposition == "take_remaining_work" and existing
                and existing["coordination_event_id"] == event_id):
            if supervisor.session.mechanism_activation_blocked:
                raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_COMMIT_UNKNOWN", "Inspect the return checkpoint before retrying.")
            return {**event, "status": "accepted", "returned_assignment": existing, "deliveries": []}
        if not resolution["note"]:
            raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_STALE", "Record the decision and its reason.")
        if disposition == "accept_draft":
            if not version:
                raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DRAFT_NOT_FOUND", "Select an explicitly versioned draft.")
            with fence(supervisor), supervisor._context_lock, supervisor._coordination_lock:
                validate_draft(manager, supervisor, event, closeout=False)
                checkpoint(manager, supervisor, entry)
        elif disposition == "complete_returned_work":
            from .assignment_handoff import accept_returned_delivery
            accepted_result = accept_returned_delivery(manager, supervisor, event, entry)
        else:
            if existing:
                raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_STALE", "The assignment was already returned.")
            if not version and latest_draft(supervisor, child_id):
                raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_VERSION_CHANGED", "Select the current explicit draft ID.")
            inspect = (lambda: validate_draft(manager, supervisor, event, closeout=True)) if version else (
                lambda: inspect_return(manager, supervisor, event))
            child, initial_contract, assignment = inspect()
            draft = version or retain_draft(manager, child, assignment["target_files"])
            with fence(supervisor), supervisor._context_lock, supervisor._coordination_lock:
                current = next((item for item in current_coordination_events(supervisor.session.host_ledger)
                                if item["coordination_event_id"] == event_id), None)
                if current is None or current["status"] not in {"pending", "awaiting_supervisor", "draft_adopted"}:
                    raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_STALE", "The return request has already changed.")
                _, contract, returned = inspect()
                if contract is not initial_contract or returned["target_files"] != assignment["target_files"]:
                    raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_STALE", "The delegated assignment changed during retention.")
                returned["draft"] = draft
                supervisor.delegated_contracts[child_id] = {**contract, "returned_assignment": returned}
                entry["returned_assignment"] = returned
                checkpoint(manager, supervisor, entry,
                           lambda: supervisor.delegated_contracts.__setitem__(child_id, contract))
    else:
        with supervisor._coordination_lock:
            returned = (supervisor.delegated_contracts.get(event["source_process_id"]) or {}).get("returned_assignment")
            if returned and returned["coordination_event_id"] == event_id:
                raise ValueError("COORDINATION_RESOLUTION_INVALID")
            supervisor.session.host_ledger.append(entry)

    process_ids = list(dict.fromkeys([
        str(event.get("source_process_id") or ""),
        *[str(item) for item in list(event.get("affected_process_ids") or [])],
        supervisor.process_id,
    ]))
    if disposition == "accept_revision":
        proposed = dict(dict(event.get("details") or {}).get("proposed") or {})
        for ref in list(event.get("affected_interfaces") or []):
            for process_id in process_ids:
                process = manager.get(process_id)
                if process is not None:
                    _replace_interface_baseline(process, str(ref), proposed)

    notice = {
        **event,
        "status": status,
        "resolution": resolution,
        "summary": note or f"Coordination event {disposition}.",
    }
    if accepted_result:
        resolution["accepted_result"] = accepted_result
    deliveries = []
    for process_id in process_ids:
        process = manager.get(process_id)
        if process is None:
            continue
        _update_projected_event(process, event_id, status=status, resolution=resolution)
        if event.get("draft") and process_id in event.get("affected_process_ids", []):
            continue
        if process.process_id != supervisor.process_id:
            deliveries.append(_enqueue_notice(process, notice, audience="worker"))
    manager.notify_state_changed(supervisor.process_id)
    return {**notice, "deliveries": deliveries,
            **({"accepted_result": accepted_result} if accepted_result else {}),
            **({"returned_assignment": returned} if disposition == "take_remaining_work" else {})}


def coordination_block_reason(process) -> str:
    context, _version = process.read_context_snapshot()
    blocks = [str(item) for item in list(context.get("coordination_blocks") or []) if str(item)]
    return ",".join(blocks)

def pending_source_interface_decisions(process) -> list[str]:
    context, _ = process.read_context_snapshot()
    return [str(item["coordination_event_id"])
            for item in context.get("coordination_events", [])
            if item.get("kind") == "interface_revision_proposed"
            and item.get("source_process_id") == process.process_id
            and item.get("status") in {"pending", "awaiting_supervisor", "awaiting_user"}]
