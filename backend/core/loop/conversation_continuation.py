"""Resolve an existing conversation through the authoritative checkpoint store.

This restores identities, not execution or approval. Explicit recovery remains
required for interrupted work; a missing historical identity never starts anew.
"""
from .models import ProcessStatus


def resolve_conversation_process(manager, store, workspace, process_id, session_id=""):
    if not process_id:
        raise ValueError("CONTINUATION_STATE_UNAVAILABLE: process identity required")
    if store is not None and process_id in store.deleted_process_ids():
        raise ValueError("CONTINUATION_STATE_UNAVAILABLE: process was deleted")
    process = manager.get(process_id)
    if process is None and store is not None:
        from .recovery import restore_incomplete_processes
        restore_incomplete_processes(store, manager, workspace, include_process_ids=[process_id])
        process = manager.get(process_id)
    if process is None or (session_id and process.session.session_id != session_id):
        raise ValueError("CONTINUATION_STATE_UNAVAILABLE: conversation checkpoint missing or changed")
    if process.status in {ProcessStatus.RECOVERY_REVIEW_REQUIRED, ProcessStatus.RESUME_AVAILABLE,
                          ProcessStatus.RECOVERING}:
        raise ValueError("CONTINUATION_RECOVERY_REQUIRED: inspect and explicitly resume interrupted work")
    if process.parent_id and manager.get(process.parent_id) is None:
        raise ValueError("CONTINUATION_STATE_UNAVAILABLE: supervisor checkpoint missing")
    return process

def bounded_continuation_tools(profile_tools, *, contract=None, issued_tools=None):
    """Restore an issued subset; old dynamic grants never expand a profile."""
    scope = issued_tools
    if scope is None and isinstance(contract, dict):
        scope = contract.get("allowed_tools")
    if scope is None:
        return list(profile_tools)  # legacy profile is the only recorded bound
    if not isinstance(scope, list) or any(not isinstance(name, str) for name in scope):
        raise ValueError("CONTINUATION_STATE_UNAVAILABLE: issued tool scope is invalid")
    return [name for name in profile_tools if name in set(scope)]
