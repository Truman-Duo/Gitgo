"""Bind a daemon project to its local operator's protected worktree settings."""
from contextlib import contextmanager
from pathlib import Path
import re

from backend.core.macos_broker_client import resident_project_session
from backend.core.storage.paths import resolve_existing_storage_paths


@contextmanager
def bind_project(project):
    selection = project.macos_worktree
    if (type(selection) is not dict
            or set(selection) != {"service", "snapshot_commit"}):
        raise ValueError("Protected worktree settings require a service and sealed snapshot")
    service = selection["service"]
    snapshot_commit = selection["snapshot_commit"]
    if type(service) is not str or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", service):
        raise ValueError("Select an administrator-provisioned project service")
    if type(snapshot_commit) is not str or not re.fullmatch(r"[0-9a-f]{40}", snapshot_commit):
        raise ValueError("Select a sealed project snapshot using a full commit ID")

    if not project.workspace_path:
        raise ValueError("Select a registered project workspace")
    workspace = Path(project.workspace_path).resolve(strict=True)
    if resolve_existing_storage_paths(workspace) is None:
        raise ValueError("Register the project before opening a protected worktree")
    with resident_project_session(service, workspace):
        yield snapshot_commit
