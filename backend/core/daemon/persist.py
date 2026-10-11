"""Session persistence helpers — checkpoint + incomplete-session recovery.

Extracted from daemon/__init__.py (pure structural refactor).
"""

from __future__ import annotations

from backend.core.storage import StorageReferenceMissing


def _save_session_checkpoint(daemon_ctx: dict, process) -> None:
    """Save session checkpoint after agent_step completes or errors."""
    store = daemon_ctx.get("session_store")
    if store is None:
        return
    if getattr(process, "session", None) is None:
        return
    from backend.core.loop.lifecycle import TERMINAL
    previous = getattr(process, "lifecycle_finalized", False)
    if process.status.value in TERMINAL:
        process.lifecycle_finalized = True
    try:
        # State, revision, immutable fact and terminal barrier commit together.
        store.save_process_checkpoint(process)
    except BaseException:
        process.lifecycle_finalized = previous
        raise


def _scan_incomplete_sessions(session_store, apm) -> list[str]:
    """Read durable incomplete processes from the authoritative session store."""
    incomplete = session_store.list_incomplete()
    recoverable = []
    for pid in incomplete:
        if apm.get(pid) is None:
            try:
                msgs = session_store.load_session(pid)
            except StorageReferenceMissing:
                # Recovery will expose a discard-only diagnostic candidate;
                # startup itself must remain available.
                msgs = ["storage-reference-missing"]
            if msgs:
                recoverable.append(pid)
    return recoverable
