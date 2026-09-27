"""State Bundle — 项目治理状态的自包含导出格式。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from backend.core.governance.patterns import build_patterns_report
from backend.core.governance.quality import compute_quality_metrics, load_suggestion_pairs
from backend.core.history import HistoryManager
from backend.core.sync_session import SyncSession


def _collect_knowledge_snapshot(session: SyncSession, *, minimal: bool) -> dict:
    """Export lessons and lifecycle state, never raw reasoning/signal payloads."""
    from pathlib import Path
    from collections import Counter
    from backend.core.knowledge.applicability import assess_lesson
    from backend.core.knowledge.harvest import harvest_status
    from backend.core.knowledge.lesson import LessonManager

    workspace = Path(session.workspace_path)
    project_name = session.project.name
    # A configured project can legitimately be exported before its workspace
    # is created or while removable/offline storage is unavailable.  Knowledge
    # is an additive section of the bundle; it must not make the entire status
    # export fail.  Preserve lifecycle truth and mark the snapshot unavailable
    # instead of fabricating lessons or opening storage outside the workspace.
    workspace_available = workspace.is_dir()
    if workspace_available:
        lifecycle = harvest_status(project_name)
        abstract = LessonManager.load_abstract(workspace)
        instances = LessonManager.load_instance(workspace, project_name)
        pending = LessonManager.load_pending(workspace, project_name)
    else:
        lifecycle = {
            "total_signals": 0,
            "states": {},
            "signal_types": {},
            "awaiting_confirmation": [],
        }
        abstract, instances, pending = [], [], []
    lesson_groups = {
        "abstract": abstract,
        "instances": instances,
        "pending": pending,
    }
    applicability = {
        lesson.id: assess_lesson(lesson, workspace)
        for lessons in lesson_groups.values()
        for lesson in lessons
    } if workspace_available else {}
    applicability_counts = Counter(
        str(item.get("state") or "unknown") for item in applicability.values()
    )
    if minimal:
        return {
            "workspace_available": workspace_available,
            "counts": {
                "abstract": len(abstract),
                "instances": len(instances),
                "pending": len(pending),
            },
            "applicability_counts": dict(sorted(applicability_counts.items())),
            "harvest": lifecycle,
        }
    def export_records(items):
        return [
            {**item.to_dict(), "applicability": applicability.get(item.id, {
                "state": "unknown", "reason": "workspace_unavailable",
            })}
            for item in items
        ]
    return {
        "workspace_available": workspace_available,
        "applicability_counts": dict(sorted(applicability_counts.items())),
        "abstract": export_records(abstract),
        "instances": export_records(instances),
        "pending": export_records(pending),
        "harvest": lifecycle,
    }


def collect_state_bundle(session: SyncSession, minimal: bool = False,
                         include_identity: bool = False) -> dict:
    """收集项目的完整治理状态快照。

    - minimal=True: 不含 history/suggestions，仅 status + governance summary
    - include_identity=True: 包含项目身份快照（目录骨架、工具记忆摘要）
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
        "knowledge": _collect_knowledge_snapshot(session, minimal=minimal),
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

    if include_identity:
        bundle["identity"] = _collect_identity_snapshot(session)

    return bundle


def _collect_identity_snapshot(session: SyncSession) -> dict:
    """收集项目身份快照。"""
    from pathlib import Path
    ws = Path(session.workspace_path)

    # 目录骨架
    dirs, files = [], []
    try:
        for entry in sorted(ws.iterdir()):
            if entry.name.startswith(".") and entry.name not in (
                ".git", ".claude", ".codex", ".codebuddy", ".github",
            ):
                continue
            if entry.is_dir():
                dirs.append(entry.name)
            else:
                files.append(entry.name)
    except PermissionError:
        pass

    # 身份文件状态
    identity_files = {}
    for fname in ["CLAUDE.md", ".claude/", ".codex/", ".codebuddy/",
                  ".gitignore", "gitgo_config.json", "sync_config.json"]:
        p = ws / fname.strip("/")
        identity_files[fname] = "present" if p.exists() else "missing"

    # 工具记忆摘要
    tool_memories = {}
    for name in [".claude", ".codex", ".codebuddy"]:
        p = ws / name
        if p.is_dir():
            file_count = sum(1 for _ in p.rglob("*") if _.is_file())
            tool_memories[name] = {"file_count": file_count}
        elif p.exists():
            tool_memories[name] = {"size": p.stat().st_size}
        else:
            tool_memories[name] = None

    return {
        "project_structure": {"dirs": dirs, "files": files},
        "identity_files": identity_files,
        "tool_memories": tool_memories,
    }
