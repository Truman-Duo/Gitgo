"""Admitted read resources in a unique native invocation, not a shared ACL grant.

The existing permission broker owns authority. This consumer prepares only its
private writable execution home and read-only resource scope, then retires both
after the existing native supervisor has confirmed process-tree cleanup.
"""
import os
from pathlib import Path
import stat

from backend.core.sandbox import SandboxDenied, SandboxPolicy
from .file_candidate_runtime import FileCandidateRuntime
from .read_approval import NativeReadBinding, native_cleanup_pending, runtime_cleanup_report

_RETAINED = []


class NativeReadRuntime(FileCandidateRuntime):
    def execute(self, handler, args, timeout, *, binding, diagnostics, runtime_metadata):
        if type(binding) is not NativeReadBinding:
            raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Read view requires canonical invocation admission')
        binding.payload(args)
        if not self.is_current() or self.ctx.is_cancelled():
            raise SandboxDenied('TOOL_CANCELLED', 'Read task changed before preparation')
        if native_cleanup_pending(self.ctx.process):
            raise SandboxDenied('SANDBOX_CLEANUP_PENDING', 'Earlier native cleanup remains unconfirmed')
        from backend.core.tools.workspace_tools import _confined_path, _read_failure
        workspace = Path(self.workspace)
        try:
            target = _confined_path(workspace, args.get('path', '.'), must_exist=True,
                allowed_roots=[Path(p) for p in args.get('_allowed_roots') or []])
        except FileNotFoundError:
            runtime_metadata.update(launch_state='not_started', sandbox_preparation={'phase': 'target_missing'})
            return _read_failure('FILE_NOT_FOUND', str(args.get('path', '.')))
        except PermissionError as exc:
            return _read_failure('READ_ACCESS_DENIED', str(args.get('path', '.')), detail=str(exc))
        if self.tool.execution_contract.file_action == 'read_existing_file':
            if stat.S_ISDIR(target.stat().st_mode):
                return _read_failure('IS_DIRECTORY', str(target))
            binding.pin_current_file(args)

        from .candidate_workspace import allocate_candidate_workspace
        from .execution_contract import NativeInvocation
        from .workspace_candidate_runtime import WorkspaceCandidateRuntime
        from backend.core.sandbox_access import WorkspaceAccess, native_runtime_access
        stages, stage = allocate_candidate_workspace(self.storage.paths, 'read', workspace=workspace)
        access = None
        # ReadRoot needs directory metadata, not data access to siblings. Linux
        # gets empty parent directories; only the selected target is ro-bound.
        metadata = {workspace, target.parent, stage.parent}
        if target.is_relative_to(workspace):
            metadata.update(p for p in target.parents if p == workspace or p.is_relative_to(workspace))
        metadata = {p for p in metadata if p != Path(p.anchor)
                    and not (p == target or p.is_relative_to(target))}
        preparation = {'phase': 'preparing', 'scope': 'native_read', 'prepared': False, 'retired': False}
        runtime_metadata['sandbox_preparation'] = preparation
        try:
            if os.name == 'nt':
                runtimes, shallow = native_runtime_access()
                journal = self.storage.paths.project_root / 'sandbox_access' / (stage.name + '.json')
                journal.parent.mkdir(parents=True, exist_ok=True)
                access = WorkspaceAccess(stage, runtimes, journal, shallow_runtimes=shallow,
                    read_roots=[target], read_workspace=workspace, metadata_roots=metadata)
                access.prepare()
            if not self.is_current() or self.ctx.is_cancelled():
                raise SandboxDenied('TOOL_CANCELLED', 'Read task changed during preparation')
            binding.payload(args)
            policy = SandboxPolicy(stage, cpu_seconds=max(1, min(int(timeout), 1800)),
                read_roots=(target,), metadata_roots=tuple(sorted(metadata, key=str)))
            invocation = NativeInvocation(handler, NativeInvocation.encode(args), timeout, policy,
                self.owner, self.registration)
            runtime_metadata['sandbox_preparation']['phase'] = 'prepared'
            preparation['prepared'] = True
            return self.pipeline._execute_isolated(handler, args, timeout, native_invocation=invocation,
                read_binding=binding, cancellation_event=self.ctx.cancellation,
                diagnostics=diagnostics, runtime_metadata=runtime_metadata)
        except OSError as exc:
            preparation['failure_stage'] = preparation['phase']
            preparation['os_error'] = {'type': type(exc).__name__, 'errno': exc.errno,
                                       'winerror': getattr(exc, 'winerror', None)}
            if not preparation['prepared']:
                raise SandboxDenied('SANDBOX_PREPARATION_FAILED', str(exc)) from exc
            raise
        finally:
            cleanup = runtime_cleanup_report(runtime_metadata.get('cleanup'))
            if runtime_metadata.get('launch_state') in {'started', 'unknown'} and (cleanup or {}).get('complete') is not True:
                _RETAINED.append((access, stage))
                raise SandboxDenied('SANDBOX_CLEANUP_PENDING', 'Retain the read view until native cleanup is confirmed')
            if access is not None:
                try:
                    access.close()
                except Exception as exc:
                    _RETAINED.append((access, stage))
                    runtime_metadata['sandbox_preparation']['phase'] = 'retirement_pending'
                    raise SandboxDenied('SANDBOX_CLEANUP_PENDING', str(exc)) from exc
            runtime_metadata['sandbox_preparation']['phase'] = 'retired'
            preparation['retired'] = True
            WorkspaceCandidateRuntime._discard_stage(stage, stages)
