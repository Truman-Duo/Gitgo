"""Cheap repository-scope admission checks.

This guard deliberately blocks catastrophic roots before any broad file walk.
Deeper size/file-count inspection belongs to the deterministic scan subsystem.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepositoryScope:
    workspace: Path
    git_root: Path | None
    allowed: bool
    severity: str
    reasons: tuple[str, ...]


def _discover_git_root(workspace: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value).resolve() if value else None


def assess_repository_scope(
    workspace: str | Path,
    *,
    git_root: str | Path | None = None,
    home: str | Path | None = None,
) -> RepositoryScope:
    resolved = Path(workspace).expanduser().resolve()
    discovered = (
        Path(git_root).expanduser().resolve()
        if git_root is not None
        else _discover_git_root(resolved)
    )
    if discovered is None:
        return RepositoryScope(resolved, None, True, "warning", ("not_a_git_repository",))

    reasons: list[str] = []
    resolved_home = Path(home).expanduser().resolve() if home is not None else Path.home().resolve()
    if discovered == resolved_home:
        reasons.append("git_root_is_user_home")
    if discovered == Path(discovered.anchor):
        reasons.append("git_root_is_volume_root")
    if not resolved.is_relative_to(discovered):
        reasons.append("workspace_outside_git_root")
    return RepositoryScope(
        workspace=resolved,
        git_root=discovered,
        allowed=not reasons,
        severity="blocked" if reasons else "ok",
        reasons=tuple(reasons),
    )
