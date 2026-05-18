"""State Bundle — 项目治理状态的自包含导出格式。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from backend.core.governance.patterns import build_patterns_report
from backend.core.governance.quality import compute_quality_metrics, load_suggestion_pairs
from backend.core.history import HistoryManager
from backend.core.sync_session import SyncSession


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
