"""Shared compilation, fencing and durable activation for task mechanisms.

Only developer registered definitions enter this boundary. Mechanism parameters
configure existing consumers; they never carry capability/permission grants.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable
import uuid


@dataclass(frozen=True)
class Definition:
    identifier: str
    version: int
    default_parameters: tuple[tuple[str, str], ...]
    consumers: tuple[str, ...]
    dependencies: tuple[tuple[str, int], ...]
    validate: Callable[[dict], None]
    activation_boundary: str


def owner(process):
    manager = getattr(process, "_manager", None)
    seen = set()
    while True:
        if process.process_id in seen:
            raise ValueError("MECHANISM_OWNER_CYCLE")
        seen.add(process.process_id)
        owner_id = process.runtime_preferences.get("mechanism_owner_id") or (process.parent_id if manager else None)
        if not owner_id or owner_id == process.process_id:
            return process
        target = manager.get(owner_id) if manager else None
        if target is None:
            raise ValueError("MECHANISM_OWNER_UNAVAILABLE")
        process = target


def snapshot(process, definition):
    root = owner(process)
    with root.session._mechanism_lock:
        if root.session.mechanism_activation_blocked:
            raise ValueError("MECHANISM_ACTIVATION_BLOCKED")
        record = root.session.mechanism_instances.get(definition.identifier)
        if record is None:
            return {"definition_id": definition.identifier, "definition_version": definition.version,
                    "scope_id": root.session.session_id, "revision": 0,
                    "parameters": dict(definition.default_parameters), "phase": "effective"}
        if (record.get("definition_id") != definition.identifier or record.get("definition_version") != definition.version
                or record.get("scope_id") != root.session.session_id
                or record.get("phase") != "effective" or type(record.get("revision")) is not int
                or record.get("activation_boundary") != definition.activation_boundary
                or record["revision"] < 1 or record.get("dependencies") != dict(definition.dependencies)
                or record.get("consumers") != list(definition.consumers)):
            raise ValueError("MECHANISM_DEFINITION_UNSUPPORTED")
        definition.validate(record.get("parameters"))
        return deepcopy(record)


def compile_revision(current, definition, parameters, expected_revision):
    definition.validate(parameters)
    if type(expected_revision) is not int or expected_revision != current["revision"]:
        raise ValueError("MECHANISM_REVISION_CONFLICT")
    return {**current, "parameters": deepcopy(parameters), "revision": current["revision"] + 1,
            "transaction_id": str(uuid.uuid4()), "phase": "effective",
            "consumers": list(definition.consumers), "dependencies": dict(definition.dependencies),
            "activation_boundary": definition.activation_boundary}


def next_authority_sequence(session):
    """Stable causal order retained with authority receipts across compaction."""
    with session._mechanism_lock:
        previous = max(
            session.authority_sequence,
            max((int(event.get("authority_sequence", 0)) for event in session.host_ledger), default=0),
            max((int(record.get("control_sequence", 0)) for record in session.mechanism_instances.values()), default=0),
        )
        session.authority_sequence = previous + 1
        return session.authority_sequence


def activate(process, definition, parameters, expected_revision, store):
    root = owner(process)
    with root.session._mechanism_lock:
        # Persist through the current root, so changing a setting cannot make
        # a historical terminal process become the latest conversation cursor.
        manager = getattr(root, "_manager", None)
        if manager is not None:
            roots = [p for p in manager.list_processes() if not p.parent_id and p.session is root.session]
            root = max(roots, key=lambda p: p.created_at or "") if roots else root
        current = snapshot(root, definition)
        candidate = compile_revision(current, definition, parameters, expected_revision)
        if current["parameters"] == parameters:
            return current
        if store is None:
            raise ValueError("MECHANISM_CHECKPOINT_UNAVAILABLE")
        candidate["control_sequence"] = next_authority_sequence(root.session)
        candidate["user_control"] = {"origin": "user_command", "authority_sequence": candidate["control_sequence"],
                                     "parameters": deepcopy(parameters)}
        before = deepcopy(root.session.mechanism_instances)
        root.session.mechanism_instances = {**before, definition.identifier: candidate}
        event = {"event": "mechanism_revision_activated", **candidate, "authority_sequence": candidate["control_sequence"]}
        commit_activation(root, event, store,
                          lambda: setattr(root.session, "mechanism_instances", before))
        return deepcopy(candidate)


def commit_activation(process, event, store, rollback):
    """Commit staged control state and its fact through the existing checkpoint.

    The caller owns the activation fence and stages no external effects. Known
    failures restore the prior state; an uncertain commit retains both evidence
    and staged state and blocks admission until ordinary recovery resolves it.
    """
    process.session.host_ledger.append(event)
    try:
        store.save_process_checkpoint(process)
    except Exception as exc:
        from backend.core.storage import StorageCommitUnknown
        if isinstance(exc, StorageCommitUnknown):
            process.session.mechanism_activation_blocked = True
            owner(process).session.mechanism_activation_blocked = True
        else:
            rollback()
            process.session.host_ledger.remove(event)
        raise


@contextmanager
def fence(process):
    """One fence for a mechanism instance, including old/new process owners."""
    root = owner(process)
    with root.session._mechanism_lock:
        yield root
