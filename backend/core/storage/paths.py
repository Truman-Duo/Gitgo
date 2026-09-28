"""Stable project identity and external runtime-state paths."""

from __future__ import annotations

import os
import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from .models import StorageBlocked


PROJECT_ID_FILE = "project-id"


class StorageLocationConflict(StorageBlocked):
    code = "STORAGE_LOCATION_CONFLICT"


def validate_storage_location(project_root: Path) -> None:
    """Never combine a virtualized main DB with journals from another directory.

    Windows MSIX can merge AppData directories without showing symlinks. Resolve
    existing family members individually, not just the apparent parent folder.
    """
    canonical = project_root.resolve()
    for base in ("state.sqlite3", "observability.sqlite3"):
        for suffix in ("", "-wal", "-shm", "-journal"):
            member = project_root / (base + suffix)
            if member.exists() and member.resolve().parent != canonical:
                raise StorageLocationConflict(
                    f"SQLite file family is redirected or split: {member} resolves to "
                    f"{member.resolve()}. Stop all project runtimes and explicitly "
                    "recover/migrate the complete database family to a stable directory."
                )


def _select_default_home(project_id: str) -> Path:
    preferred = _default_state_home()
    if os.name != "nt" or os.getenv("GITGO_STATE_HOME") or not os.getenv("LOCALAPPDATA"):
        return preferred
    legacy = Path(os.environ["LOCALAPPDATA"]) / "Gitgo" / "state"
    old_root, new_root = legacy / "projects" / project_id, preferred / "projects" / project_id
    if old_root.exists():
        if not new_root.exists():
            # Do not silently abandon existing data after changing a default.
            return legacy
        receipt = new_root / "storage-location.json"
        try:
            record = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise StorageLocationConflict("Both legacy and canonical project stores exist without a migration receipt")
        if record.get("project_id") != project_id or record.get("source_home") != str(legacy):
            raise StorageLocationConflict("Storage migration receipt does not match this project/source")
    return preferred


@dataclass(frozen=True)
class StoragePaths:
    workspace: Path
    project_id: str
    state_home: Path
    project_root: Path
    state_db: Path
    observability_db: Path
    cas_dir: Path
    health_file: Path


def _default_state_home() -> Path:
    override = os.getenv("GITGO_STATE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        # AppData is a merged view under MSIX launchers. A launcher-independent
        # profile directory avoids pairing private main files with public WALs.
        return (Path.home() / ".gitgo" / "state").resolve()
    xdg = os.getenv("XDG_STATE_HOME")
    if xdg:
        return (Path(xdg) / "gitgo").expanduser().resolve()
    if sys_platform() == "darwin":
        return (Path.home() / "Library" / "Application Support" / "Gitgo" / "state").resolve()
    return (Path.home() / ".local" / "state" / "gitgo").resolve()


def sys_platform() -> str:
    # Isolated for straightforward platform tests without mutating sys.platform.
    import sys

    return sys.platform


def _read_project_id(path: Path) -> str:
    raw = path.read_text(encoding="ascii").strip()
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(
            f"Invalid Gitgo project identity at {path}; refusing to silently replace it"
        ) from exc


def _git_common_dir(workspace: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=workspace,
            # NativeHost is a long-lived JSON-lines process.  Letting Git
            # inherit that open request pipe can keep Windows' subprocess
            # reader threads alive after git itself has exited, which blocks
            # every history/trace read until the Dashboard times out.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return None
    common = Path(value)
    if not common.is_absolute():
        common = workspace / common
    return common.resolve()


def _existing_project_identity(workspace: Path) -> Path | None:
    """Locate an existing identity lexically without spawning Git.

    Read paths are hot (history expansion, status, project overview) and must
    not launch a subprocess merely to rediscover an identity Gitgo already
    wrote.  The linked-worktree form is resolved from Git's documented
    ``gitdir``/``commondir`` files; malformed metadata simply falls through to
    the normal creation-time probe.
    """
    git_entry = workspace / ".git"
    candidates: list[Path] = []
    if git_entry.is_dir():
        candidates.append(git_entry / "gitgo" / PROJECT_ID_FILE)
    elif git_entry.is_file():
        try:
            raw = git_entry.read_text(encoding="utf-8").strip()
            if raw.lower().startswith("gitdir:"):
                git_dir = Path(raw.split(":", 1)[1].strip())
                if not git_dir.is_absolute():
                    git_dir = (workspace / git_dir).resolve()
                common_dir = git_dir
                common_file = git_dir / "commondir"
                if common_file.is_file():
                    relative = Path(common_file.read_text(encoding="utf-8").strip())
                    common_dir = (
                        relative if relative.is_absolute()
                        else (git_dir / relative).resolve()
                    )
                candidates.append(common_dir / "gitgo" / PROJECT_ID_FILE)
        except OSError:
            pass
    candidates.append(workspace / ".gitgo" / PROJECT_ID_FILE)
    return next((path for path in candidates if path.is_file()), None)


def _ensure_project_id(workspace: Path) -> str:
    existing = _existing_project_identity(workspace)
    if existing is not None:
        return _read_project_id(existing)
    common = _git_common_dir(workspace)
    # Git's common directory is shared by every linked worktree and is never a
    # commit candidate.  Non-git workspaces retain the local metadata fallback.
    metadata = (common / "gitgo") if common is not None else (workspace / ".gitgo")
    metadata.mkdir(parents=True, exist_ok=True)
    path = metadata / PROJECT_ID_FILE
    if path.exists():
        return _read_project_id(path)

    project_id = str(uuid.uuid4())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_project_id(path)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            handle.write(project_id + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # A partial identity is never regenerated automatically.  It is better
        # to fail explicitly than split one project across two state stores.
        raise
    return project_id


def resolve_storage_paths(
    workspace: str | Path,
    *,
    state_home: str | Path | None = None,
) -> StoragePaths:
    resolved_workspace = Path(workspace).expanduser().resolve()
    if not resolved_workspace.is_dir():
        raise FileNotFoundError(f"Workspace does not exist: {resolved_workspace}")
    project_id = _ensure_project_id(resolved_workspace)
    resolved_home = (
        Path(state_home).expanduser().resolve() if state_home is not None else _select_default_home(project_id)
    )
    project_root = resolved_home / "projects" / project_id
    validate_storage_location(project_root)
    project_root.mkdir(parents=True, exist_ok=True)
    if (project_root / "project-deleted.json").exists():
        raise StorageBlocked("PROJECT_DELETED: this project store was explicitly retired")
    cas_dir = project_root / "cas"
    cas_dir.mkdir(parents=True, exist_ok=True)
    return StoragePaths(
        workspace=resolved_workspace,
        project_id=project_id,
        state_home=resolved_home,
        project_root=project_root,
        state_db=project_root / "state.sqlite3",
        observability_db=project_root / "observability.sqlite3",
        cas_dir=cas_dir,
        health_file=project_root / "storage-health.json",
    )


def resolve_existing_storage_paths(workspace: str | Path) -> StoragePaths | None:
    """Resolve an existing project's store without creating or probing it.

    Project-list rendering must not run Git, create identity files/directories,
    or construct the full writer runtime.  Main repositories keep identity in
    ``.git/gitgo``; non-Git workspaces use ``.gitgo``. Linked worktrees follow
    their ``gitdir``/``commondir`` files lexically, without invoking Git.
    """
    resolved_workspace = Path(workspace).expanduser().resolve()
    if not resolved_workspace.is_dir():
        raise FileNotFoundError(f"Workspace does not exist: {resolved_workspace}")
    identity = _existing_project_identity(resolved_workspace)
    if identity is None:
        return None
    project_id = _read_project_id(identity)
    resolved_home = _select_default_home(project_id)
    project_root = resolved_home / "projects" / project_id
    validate_storage_location(project_root)
    if (project_root / "project-deleted.json").exists():
        raise StorageBlocked("PROJECT_DELETED: this project store was explicitly retired")
    return StoragePaths(
        workspace=resolved_workspace,
        project_id=project_id,
        state_home=resolved_home,
        project_root=project_root,
        state_db=project_root / "state.sqlite3",
        observability_db=project_root / "observability.sqlite3",
        cas_dir=project_root / "cas",
        health_file=project_root / "storage-health.json",
    )
