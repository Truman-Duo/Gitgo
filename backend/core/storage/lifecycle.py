"""Lifecycle transaction helpers owned by StorageRuntime, never a second writer."""
from __future__ import annotations

from .models import StorageBlocked


class RevisionConflict(StorageBlocked):
    code = "STATE_REVISION_CHANGED"


class CheckpointRef(str):
    def __new__(cls, value, revision):
        ref = super().__new__(cls, value)
        ref.revision = revision
        return ref


def check_revision(connection, process_id, expected):
    row = connection.execute("SELECT state_revision FROM session_processes WHERE process_id=?", (process_id,)).fetchone()
    actual = int(row[0]) if row is not None else 0
    if expected is not None and (type(expected) is not int or expected != actual):
        raise RevisionConflict("STATE_REVISION_CHANGED: refresh the process before committing its checkpoint")
    return actual


def check_transition(connection, process_id, snapshot, read_blob):
    from backend.core.loop.lifecycle import validate_transition, ACTIVE, RECOVERY
    row = connection.execute("SELECT status,runtime_state_ref FROM session_processes WHERE process_id=?", (process_id,)).fetchone()
    if row is None:
        return
    previous, proposed = str(row[0]), str(snapshot.get("status") or "running")
    runtime = snapshot.get("runtime_state") or {}
    if previous in ACTIVE and proposed in RECOVERY and runtime.get("recovery"):
        return  # Host recovery publishes a diagnostic state, never a running lease.
    if (previous in ACTIVE and proposed == "recovering"
            and (runtime.get("recovery") or {}).get("state") == "recovering"
            and snapshot.get("expected_revision") is not None):
        # Restore does not overwrite the last live checkpoint. Explicit Host
        # recovery claims that exact revision before acquiring its next lease.
        return
    if previous == "completed" and proposed != previous:
        prior = read_blob(row[1]) if row[1] else {}
        if prior.get("lifecycle_finalized") is False:
            if proposed == "failed" and runtime.get("lifecycle_finalized") is False:
                return  # Provisional success failed the Host finalization boundary.
            if proposed in RECOVERY and (runtime.get("recovery") or {}).get("resume_forbidden"):
                return  # Diagnostic only; never reopen the old execution.
    validate_transition(previous, proposed)
