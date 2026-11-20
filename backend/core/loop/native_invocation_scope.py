"""Bind nested native producers to their actual Pipeline invocation owner."""
from contextlib import contextmanager
from contextvars import ContextVar

_SCOPE = ContextVar("native_invocation_scope", default=None)


def current_native_records(ctx):
    scope = _SCOPE.get()
    return scope["records"] if scope is not None and scope["ctx"] is ctx else []


def current_native_cancellation(workspace):
    """Propagate the owning task's cancellation to nested service queries."""
    scope = _SCOPE.get()
    if scope is None:
        return None
    _check_project_scope(scope['ctx'], workspace)
    return scope['ctx'].cancellation


def _check_project_scope(ctx, project):
    """Permit a linked workspace only through its durable task-owned lease."""
    from pathlib import Path
    from backend.core.sandbox import SandboxDenied
    actual, expected = Path(ctx.workspace_path).absolute(), Path(project).absolute()
    if actual == expected:
        return
    if not getattr(getattr(ctx, 'process', None), 'process_id', None):
        raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Native query belongs to a different project; task worktree ownership is missing')
    from backend.core.macos_worktree import resolve_authority
    try:
        root, manager, lease = resolve_authority(actual)
        if root != expected or lease is None:
            raise ValueError('worktree does not belong to the expected project')
        storage = getattr(ctx, 'storage', None)
        if storage is not None and (storage.paths.project_id != manager.storage.paths.project_id or
                storage.paths.project_root != manager.storage.paths.project_root):
            raise ValueError('task journal differs from the worktree project')
    except (OSError, ValueError, RuntimeError) as exc:
        raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Native query belongs to a different task project') from exc


def current_native_owner(project):
    scope=_SCOPE.get()
    if scope is None:return ('','')
    ctx=scope['ctx'];_check_project_scope(ctx,project)
    process=getattr(ctx,'process',None)
    identifier=getattr(process,'process_id','')
    return (identifier,getattr(process,'active_task_id',None) or identifier)


def retain_native_recovery(ctx, findings):
    recovery = ctx.process.recovery or {}
    reasons = list(recovery.get("reasons") or [])
    reconciled={f.get('invocation_id') for f in findings if f.get('code')=='NATIVE_OUTCOME_UNCONFIRMED'}
    reasons=[r for r in reasons if not (r.get('invocation_id') in reconciled and
        r.get('code') in ('NATIVE_LAUNCH_UNCONFIRMED','NATIVE_CLEANUP_UNCONFIRMED'))]
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
    from backend.core.storage.native_invocations import pending_native_invocations, reconcile_native_invocations
    storage = ctx.storage or (peek_storage(ctx.workspace_path) if ctx.workspace_path else None)
    if storage is not None:
        reconcile_native_invocations(storage)
        findings = pending_native_invocations(storage)
        if findings:
            retain_native_recovery(ctx, findings)
            from backend.core.sandbox import SandboxDenied
            code='NATIVE_OUTCOME_UNCONFIRMED' if all(f['code']=='NATIVE_OUTCOME_UNCONFIRMED' for f in findings) else 'SANDBOX_CLEANUP_PENDING'
            raise SandboxDenied(code, "Retained invocation evidence requires inspection; no automatic replay")


@contextmanager
def native_invocation_scope(ctx, execution_id, call_index):
    scope = {"ctx": ctx, "execution_id": execution_id, "call_index": call_index, "records": []}
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def prepare_native_record(invocation, *, expected_workspace=None):
    from backend.core.storage import get_storage
    from backend.core.storage.native_invocations import NativeInvocationRecord, reconcile_native_invocations
    scope = _SCOPE.get()
    ctx = scope["ctx"] if scope else None
    workspace = ctx.workspace_path if ctx else invocation.policy.workspace
    if expected_workspace is not None:
        from pathlib import Path
        if Path(workspace).absolute() != Path(expected_workspace).absolute():
            if ctx is None or invocation.handler not in ('macos_git_read','macos_git_objects','macos_git_checkout'):
                from backend.core.sandbox import SandboxDenied
                raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Native journal belongs to a different project scope')
            _check_project_scope(ctx, expected_workspace)
            # The producer works on the parent project's private object view.
            # Keep the original project ledger and the task's execution/session IDs.
            workspace = expected_workspace
    storage = (ctx.storage if ctx else None) or get_storage(workspace)
    reconcile_native_invocations(storage)
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
