"""Shared private producer directories under the existing state owner.

Access journals retain absolute identities in project state, including older
locations. Allocation does not migrate or delete any existing candidate.
"""
from pathlib import Path
import uuid
from backend.core.sandbox import SandboxDenied, child_environment_paths


def candidate_workspace_root(paths, *, workspace=None):
    project = paths.project_root.resolve()
    root = (project / "c").resolve()
    workspace = Path(workspace if workspace is not None else paths.workspace).resolve()
    if not root.is_relative_to(project) or root.is_relative_to(workspace) or workspace.is_relative_to(root):
        raise SandboxDenied("SANDBOX_POLICY_INVALID", "Private producer directory overlaps workspace or escapes state")
    return root


def allocate_candidate_workspace(paths, kind, *, workspace=None):
    if kind not in {"document", "workspace", "read"}:
        raise ValueError("Unknown private producer kind")
    root = candidate_workspace_root(paths, workspace=workspace)
    stage = root / (kind[0] + "-" + uuid.uuid4().hex)
    # Check before materialization/access preparation, not after starting a
    # wrapper and waiting for a nested CreateProcess timeout.
    child_environment_paths(stage)
    root.mkdir(parents=True, exist_ok=True)
    stage.mkdir()
    return root, stage


def prepare_candidate_home(stage):
    """After input projection, before ACL pinning; only on an owned producer.

    WorkspaceImage excludes protected metadata. Refuse an existing .gitgo
    rather than following anything from an execution input or an old launch.
    """
    stage = Path(stage)
    metadata = stage / '.gitgo'
    if metadata.exists() or metadata.is_symlink():
        raise SandboxDenied('SANDBOX_PRIVATE_HOME_COLLISION', 'A fresh producer already contains reserved Gitgo metadata')
    metadata.mkdir()
    folders = child_environment_paths(stage)
    for folder in set(folders.values()):
        folder.mkdir(parents=True, exist_ok=True)
