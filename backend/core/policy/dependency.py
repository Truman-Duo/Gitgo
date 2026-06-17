"""Dependency chain — detect files that import changed files and may need updates."""

from pathlib import Path
from typing import TYPE_CHECKING
from backend.core.policy.base import PolicyCheck

if TYPE_CHECKING:
    from backend.core.sync_session import SyncSession
    from backend.core.config import ProjectConfig


class DependencyChainCheck(PolicyCheck):
    name = "dependency_chain"
    description = "Detect files importing changed files"

    def check(self, session: "SyncSession",
              _project: "ProjectConfig") -> list[dict]:
        from backend.core.contract import get_dependents
        alerts: list[dict] = []
        changed = [e.rel_path for e in session.entries if e.status != "same"]
        if not changed:
            return alerts
        seen = set()
        for f in changed:
            deps = get_dependents(Path(session.workspace_path), f)
            for dep in deps:
                if dep in seen or dep in changed:
                    continue
                seen.add(dep)
                dep_path = Path(session.workspace_path) / dep
                if dep_path.exists():
                    alerts.append({
                        "rule": "dependency_chain",
                        "level": "info",
                        "message": f"'{f}' changed → may affect '{dep}' (imports it)",
                        "changed_file": f,
                        "dependent": dep,
                    })
        return alerts
