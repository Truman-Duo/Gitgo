"""Narrow consumer bridge to the operator-provisioned macOS Git broker.

This never supplies a command to subprocess or interprets revision expressions.
Fixed object queries and blob/tree/commit producers enter the low-UID view.
The Host only appends validated objects after cleanup and journal completion.
Full worktree/publish operations remain unavailable until their own protocol
exists; an active grant does not enable refs/worktree writes or arbitrary repository access.
"""
import math
from pathlib import Path
import subprocess

from .git_execution import GitBoundaryError


def run_object_query(cwd, args, *, capture_output=True, timeout=None, text=True,
                     encoding='utf-8', input_bytes=None, transport_identity=None,
                     author_identity=None):
    from backend.core import macos_broker_client as broker
    from backend.core.macos_git_read import read_command
    from backend.core.sandbox_read import ReadRoot
    from backend.core.sandbox import SandboxDenied
    from .native_invocation_scope import current_native_cancellation
    if not broker.active():
        raise GitBoundaryError('GIT_BOUNDARY_UNAVAILABLE', 'An operator-authorized macOS project broker is required')
    producer=bool(args) and args[0] in ('hash-object','mktree','commit-tree')
    if (capture_output is not True or type(text) is not bool or encoding not in ('utf-8','utf8')
            or (not producer and (input_bytes is not None or author_identity is not None)) or transport_identity is not None):
        raise GitBoundaryError('GIT_COMMAND_UNSUPPORTED', 'Only captured fixed object queries without stdin or overrides are supported')
    seconds=15 if timeout is None else timeout
    if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds<1:
        raise GitBoundaryError('GIT_COMMAND_UNSUPPORTED', 'Invalid Git query timeout')
    # A consumer's larger ceiling does not increase the broker's hard maximum.
    seconds=min(seconds,60)
    if producer:
        from backend.core.macos_git_write import validate
        if input_bytes is not None and text:raise GitBoundaryError('GIT_COMMAND_UNSUPPORTED','Git stdin requires binary mode')
        validate(args,b'' if input_bytes is None else input_bytes,author_identity)
    metadata_query=list(args) in (['rev-parse','HEAD'],['rev-parse','--git-common-dir'],
                                  ['rev-parse','--path-format=absolute','--git-common-dir'])
    from backend.core import macos_git_compare as compare
    comparison=compare.parse(args)
    if not producer and not metadata_query and comparison is None:read_command('/private/tmp/gitgo-query-validation',args)
    try:
        from backend.core import macos_worktree as worktrees
        project,manager,lease=worktrees.resolve_authority(cwd)
        broker.project_binding(project)
        cancellation=current_native_cancellation(project)
        if metadata_query:
            if cancellation is not None and cancellation.is_set():raise RuntimeError('Git metadata read cancelled')
            if list(args)==['rev-parse','HEAD']:
                from types import SimpleNamespace
                value=lease.macos_binding['commit'] if lease else worktrees.root_head(SimpleNamespace(repo_root=project))
            else:value=str(project/'.git')
            output=value+'\n'
            return subprocess.CompletedProcess(['git',*args],0,output if text else output.encode('utf8'),'' if text else b'')
        with ReadRoot(project/'.git/objects') as root:
            if comparison is not None:
                def guard():
                    broker.project_binding(project)
                    if lease:worktrees.verify(manager,lease)
                result=compare.query(project,root,comparison,timeout=seconds,cancellation=cancellation,guard=guard)
            elif producer:
                result=broker.produce_git_objects(project,root,args,b'' if input_bytes is None else input_bytes,author_identity,timeout=seconds,cancellation=cancellation)
            else:result=broker.query_git_objects(project,root,args,timeout=seconds,cancellation=cancellation)
        if lease:worktrees.verify(manager,lease)
    except (GitBoundaryError,SandboxDenied,OSError,ValueError,RuntimeError,EOFError) as exc:
        failure=GitBoundaryError(getattr(exc,'code','GIT_READ_UNCONFIRMED'),str(exc))
        # The original journal, not this adapter, decides whether launch occurred.
        failure.command_started=None
        if hasattr(exc,'cleanup'):failure.cleanup=exc.cleanup
        raise failure from exc
    output=result.stdout if text else result.stdout.encode('utf8')
    return subprocess.CompletedProcess(['git',*args],result.returncode,output,'' if text else b'')
