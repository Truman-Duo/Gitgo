"""Host-owned invocation evidence, never authoritative workspace JSON."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from backend.core.storage.paths import resolve_storage_paths


def invocation_journal_root(workspace: str | Path, *, paths=None, state_home=None) -> Path:
    """Use the bound project's external state; keep worktree evidence distinct."""
    workspace = Path(workspace).resolve(strict=True)
    paths = paths or resolve_storage_paths(workspace, state_home=state_home)
    project = paths.project_root.resolve(strict=True)
    if project.is_relative_to(workspace) or workspace.is_relative_to(project):
        raise OSError('Invocation evidence requires state outside the execution workspace')
    from backend.core.sandbox import trusted_runtime_roots
    if any(project.is_relative_to(root) or root.is_relative_to(project)
           for root in trusted_runtime_roots()):
        raise OSError('Invocation evidence cannot overlap exposed trusted runtime roots')
    scope = hashlib.sha256(str(workspace).encode('utf-8')).hexdigest()
    root = project / 'tool_invocations' / scope
    canonical = root.resolve(strict=False)
    if not canonical.is_relative_to(project):
        raise OSError('Invocation evidence directory is redirected outside its bound project')
    if os.name == 'nt':
        # Host journals can exceed MAX_PATH even when the native execution view
        # fits its stricter startup bound. Keep the same location and identity;
        # use extended-path I/O instead of depending on a machine registry flag.
        name = str(canonical)
        if not name.startswith('\\\\?\\'):
            canonical = Path('\\\\?\\UNC\\' + name[2:] if name.startswith('\\\\') else '\\\\?\\' + name)
    return canonical
