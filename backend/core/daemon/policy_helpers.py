"""Policy/snapshot helpers — workspace snapshot + rejection-chain harvest + LLM config.

Extracted from daemon/__init__.py (pure structural refactor).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from backend.core.config import ProjectConfig
from backend.core.sync_session import SyncSession
from backend.core.daemon.emit import _emit


def _snapshot_workspace(session: SyncSession, project: ProjectConfig) -> list[str] | None:
    """每轮结束时在 workspace 做 git commit 快照。"""
    import subprocess

    ws = str(session.workspace_path)
    changed = [e.rel_path for e in session.entries if e.status != "same"]

    try:
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        subprocess.run(["git", "add", "-A"], cwd=ws,
                       capture_output=True, text=True,
                       creationflags=creationflags, timeout=30)

        msg = f"gitgo: round snapshot [{datetime.now().strftime('%H:%M:%S')}]\n\n"
        msg += f"变更文件: {len(changed)}\n"
        if changed:
            msg += "\n".join(f"  {f}" for f in changed[:20])
            if len(changed) > 20:
                msg += f"\n  ... 还有 {len(changed) - 20} 个文件"

        result = subprocess.run(["git", "commit", "-m", msg], cwd=ws,
                                capture_output=True, text=True,
                                creationflags=creationflags, timeout=30)

        if result.returncode == 0:
            from backend.core.history import HistoryManager
            HistoryManager.add_operation(
                project.name, "workspace_state_snapshot", "success",
                {"files_changed": changed,
                 "round_time": datetime.now().isoformat()},
                correlation_id=session._correlation_id,
            )
            _emit({"event": "workspace_snapshot", "files": len(changed)})
            return changed
        return []  # no changes to commit
    except (subprocess.SubprocessError, OSError) as e:
        _emit({"event": "snapshot_error", "error": str(e)})
        return None


def _harvest_from_rejection_chain(
    project_name: str, rejections: list, session: SyncSession
) -> None:
    """从连续 rejection 中提取 pending lesson。"""
    from backend.core.knowledge.lesson import LessonManager
    from backend.core.knowledge.models import Lesson
    from backend.core.history import HistoryManager

    recent_3 = rejections[-3:]
    reasons = []
    for r in recent_3:
        d = r.detail if isinstance(r.detail, dict) else {}
        reasons.append(d.get("reason", ""))

    last_detail = recent_3[-1].detail if isinstance(recent_3[-1].detail, dict) else {}
    final_rule = last_detail.get("instruction", "")

    lesson = Lesson(
        tech_stack="",
        category="process",
        severity="high",
        trigger=f"连续 3 次被人否定: {'; '.join(reasons[-2:])}",
        rule=final_rule or "人连续纠正了多次方向性错误，最终方案需要被记录。",
    )
    lesson.id = f"rejection_{project_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    ws = Path(session.workspace_path)
    LessonManager.save_pending(ws, lesson)

    HistoryManager.add_operation(
        project_name, "governance_lesson", "success",
        {"harvested_count": 1, "lesson_id": lesson.id, "trigger": "rejection_chain"},
        correlation_id=session._correlation_id,
    )
    _emit({"event": "lesson_harvested", "lesson_id": lesson.id,
           "trigger": "rejection_chain"})


def _resolve_llm_config(workspace: str) -> tuple | None:
    """解析 LLM 配置。优先级：环境变量 > llm_config.json active_provider。"""
    base_url = os.environ.get("GITGO_LLM_BASE_URL", "")
    api_key = os.environ.get("GITGO_LLM_API_KEY", "")
    model_id = os.environ.get("GITGO_LLM_MODEL", "")

    if base_url and api_key and model_id:
        return (base_url, api_key, model_id)

    if workspace:
        try:
            from backend.core.llm_config import LLMConfigManager
            active = LLMConfigManager.get_active()
            if active:
                return (active.base_url, active.api_key, active.model_id)
        except Exception:
            pass

    return None
