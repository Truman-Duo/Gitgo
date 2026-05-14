"""State Bundle — 项目治理状态的自包含导出格式。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from backend.core.governance.patterns import build_patterns_report
from backend.core.governance.quality import compute_quality_metrics, load_suggestion_pairs
from backend.core.history import HistoryManager
from backend.core.sync_session import SyncSession


def collect_state_bundle(session: SyncSession, minimal: bool = False) -> dict:
    """收集项目的完整治理状态快照。

    - minimal=True: 不含 history/suggestions，仅 status + governance summary
    """
    project = session.project
    bundle = {
        "gitgo_protocol_version": "1.0",
        "exported_at": datetime.now().isoformat(),
        "project": {
            "name": project.name,
            "workspace_path": project.workspace_path,
            "backup_path": project.backup_path if project.backup_path else None,
            "commit_prefix": project.commit_format.get("prefix", ""),
        },
        "current_state": session.status_dict(semantic=True),
        "governance_summary": {
            "quality": compute_quality_metrics(load_suggestion_pairs(project.name)),
            "patterns": build_patterns_report(project.name),
        },
    }

    if not minimal:
        entries = HistoryManager.load()
        project_entries = [e for e in entries if e.project_name == project.name]

        bundle["recent_history"] = [
            asdict(e) for e in project_entries[-50:]
        ]
        bundle["recent_suggestions"] = [
            asdict(e) for e in project_entries
            if e.operation.startswith("suggest_")
        ][-20:]

    return bundle
