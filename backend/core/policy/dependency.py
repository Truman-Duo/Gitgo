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
    applicable_task_kinds = frozenset({"action", "supervisor", "review"})

    def check(self, session: "SyncSession",
              _project: "ProjectConfig") -> list[dict]:
        from backend.core.dependency_graph import load_dependency_graph
        alerts: list[dict] = []
        changed = [e.rel_path for e in session.entries if e.status != "same"]
        if not changed:
            return alerts
        # One policy admission owns one coherent graph snapshot.  Loading via
        # ``get_dependents`` for every changed path reparsed and fingerprinted
        # the whole repository N times (hundreds of times in a dirty tree),
        # blocking the native Host before a process could even be admitted.
        # Admission is a read path, not the graph-maintenance authority.  Use
        # the latest validated snapshot here; source-changing work refreshes
        # the graph in the atomic worktree-promotion path, and an Agent can
        # explicitly call rebuild_dependency_graph when it needs a newer view
        # before then.  Synchronously rebuilding a whole dirty repository on
        # every prompt made even greetings wait minutes.
        graph = load_dependency_graph(Path(session.workspace_path), allow_stale=True)
        seen = set()
        for f in changed:
            for edge in graph.get_dependents(f):
                dep = str(edge.get("dependent") or "")
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
        entries_by_path = {entry.rel_path: entry for entry in session.entries}
        stale_inputs = sorted({
            path for path in changed
            if graph.file_fingerprints.get(path, "")
            != str(getattr(entries_by_path.get(path), "workspace_hash", ""))[:20]
        })
        if stale_inputs:
            alerts.append({
                "rule": "dependency_snapshot_stale",
                "level": "info",
                "message": (
                    "Dependency impact uses the last validated snapshot; "
                    "changed or new paths will be refreshed at source promotion."
                ),
                "affected_files": stale_inputs,
                "snapshot_state": "stale_candidate",
            })
        return alerts
