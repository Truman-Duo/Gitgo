"""Bind nested native producers to their actual Pipeline invocation owner."""
from contextlib import contextmanager
from contextvars import ContextVar

_SCOPE = ContextVar("native_invocation_scope", default=None)


def current_native_records(ctx):
    scope = _SCOPE.get()
    return scope["records"] if scope is not None and scope["ctx"] is ctx else []


def retain_native_recovery(ctx, findings):
    recovery = ctx.process.recovery or {}
    reasons = list(recovery.get("reasons") or [])
    for finding in findings:
        if finding not in reasons:
            reasons.append(finding)
    ctx.process.recovery = {**recovery, "resume_forbidden": True,
        "requires_manual_verification": True, "reasons": reasons}


def check_native_recovery(ctx):
    """Preflight known project debt before spending a native tool's grants.

    The runner rechecks at the actual OS boundary, including service-owned
    producers. Component callers need not open a new store merely to preflight.
    """
    from backend.core.storage import peek_storage
    from backend.core.storage.native_invocations import pending_native_invocations
    storage = ctx.storage or (peek_storage(ctx.workspace_path) if ctx.workspace_path else None)
    if storage is not None:
        findings = pending_native_invocations(storage)
        if findings:
            retain_native_recovery(ctx, findings)
            raise ValueError("SANDBOX_CLEANUP_PENDING: unsettled project native evidence requires inspection")


@contextmanager
def native_invocation_scope(ctx, execution_id, call_index):
    scope = {"ctx": ctx, "execution_id": execution_id, "call_index": call_index, "records": []}
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def prepare_native_record(invocation):
    from backend.core.storage import get_storage
    from backend.core.storage.native_invocations import NativeInvocationRecord
    scope = _SCOPE.get()
    ctx = scope["ctx"] if scope else None
    workspace = ctx.workspace_path if ctx else invocation.policy.workspace
    storage = (ctx.storage if ctx else None) or get_storage(workspace)
    record = NativeInvocationRecord(storage, workspace, invocation,
        execution_id=scope["execution_id"] if scope else "",
        call_index=scope["call_index"] if scope else 0,
        session_id=ctx.session.session_id if ctx else "")
    try:
        record.prepare()
    except Exception as exc:
        from backend.core.sandbox import SandboxDenied
        if ctx is not None and getattr(exc, "findings", None):
            retain_native_recovery(ctx, exc.findings)
        if isinstance(exc, SandboxDenied):
            raise
        raise SandboxDenied("INVOCATION_JOURNAL_UNAVAILABLE",
            "Native launch evidence could not be committed; no process was started.") from exc
    if scope is not None:
        scope["records"].append(record)
    return record
