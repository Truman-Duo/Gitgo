"""操作历史记录 — 记录全操作类型的审计日志"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

HISTORY_FILE = "gitgo_history.json"


@dataclass
class HistoryEntry:
    timestamp: str  # ISO format
    project_name: str
    operation: str = ""          # "scan" | "formalize" | "sync" | "push"
                                  # | "triage_accept" | "triage_promote" | "triage_discard"
                                  # | "delete_formal" | "dissolve_formal"
    status: str = "success"       # "success" | "failed" | "cancelled"
    detail: dict = field(default_factory=dict)  # 操作特定数据
    correlation_id: str = ""      # session 级关联 ID，同一次工作流的所有记录共享
    # 保留旧字段向后兼容（add_entry 委托到 add_operation 内部填充）
    file_count: int = 0
    commit_hash: str = ""
    commit_message: str = ""
    workspace: str = ""
    backup: str = ""


class HistoryManager:
    """管理操作历史日志的读写"""

    @staticmethod
    def _path() -> Path:
        """返回历史文件路径（exe/脚本同目录）"""
        import sys
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).parent
        else:
            base = Path.cwd()
        return base / HISTORY_FILE

    @staticmethod
    def load() -> list[HistoryEntry]:
        path = HistoryManager._path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [HistoryEntry(**e) for e in data]
        except (json.JSONDecodeError, OSError, TypeError):
            return []

    @staticmethod
    def save(entries: list[HistoryEntry]) -> None:
        path = HistoryManager._path()
        path.write_text(
            json.dumps([asdict(e) for e in entries], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def add_operation(cls, project_name: str, operation: str,
                      status: str = "success", detail: dict | None = None,
                      correlation_id: str = "") -> None:
        """记录一条操作历史。

        operation: "scan" | "formalize" | "sync" | "push"
                   | "triage_accept" | "triage_promote" | "triage_discard"
                   | "delete_formal" | "dissolve_formal"
        """
        entries = cls.load()
        entries.append(HistoryEntry(
            timestamp=datetime.now().isoformat(),
            project_name=project_name,
            operation=operation,
            status=status,
            detail=detail or {},
            correlation_id=correlation_id,
        ))
        if len(entries) > 200:
            entries = entries[-200:]
        cls.save(entries)

    @classmethod
    def add_suggestion(cls, project_name: str, suggest_type: str,
                       ai_proposal: dict, human_decision: dict,
                       correlation_id: str = "") -> None:
        """记录 AI 建议与人的最终决策差异，供 P4 质量度量使用。

        - ``suggest_type``: "formalize" | "triage" | "summary"
        - ``ai_proposal``: agent 返回的完整建议 JSON
        - ``human_decision``: 人最终执行时的参数
        """
        entries = cls.load()
        entries.append(HistoryEntry(
            timestamp=datetime.now().isoformat(),
            project_name=project_name,
            operation=f"suggest_{suggest_type}",
            status="recorded",
            detail={
                "ai_proposal": ai_proposal,
                "human_decision": human_decision,
            },
            correlation_id=correlation_id,
        ))
        if len(entries) > 200:
            entries = entries[-200:]
        cls.save(entries)

    @classmethod
    def add_entry(cls,
                  project_name: str,
                  file_count: int,
                  commit_hash: str,
                  commit_message: str,
                  workspace: str,
                  backup: str,
                  correlation_id: str = "",
                  ) -> None:
        """旧 API — 记录 sync 操作。委托到 add_operation 保持向后兼容。"""
        cls.add_operation(project_name, "sync", "success", {
            "file_count": file_count,
            "commit_hash": commit_hash,
            "commit_message": commit_message.split("\n")[0][:80],
            "workspace": workspace,
            "backup": backup,
        }, correlation_id=correlation_id)
