"""操作历史记录 — 记录全操作类型的审计日志。

v0.33 E1-fix: JSONL 追加写入 + threading.Lock 并发安全。
            不再每次全量覆写，改为逐行追加（O(1) per add）。
            超过 400 条时触发 compact 保留最近 200 条。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

HISTORY_FILE = "gitgo_history.json"

_MAX_ENTRIES = 200        # 常驻上限
_COMPACT_THRESHOLD = 400  # 超过此行数触发 compact


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

    # ── L0: StateLog 2.0 关联字段 ──
    fact_refs: list[str] = field(default_factory=list)
    # 本 event 触发了哪些 fact 的派生。例：["fact_frequent_mod_dashboard_py"]
    tags: list[str] = field(default_factory=list)
    # 语义标签。例：["exploration", "abandoned", "rejected", "lesson_applied"]
    parent_event_id: str = ""
    # 前驱 event 的 correlation_id。例：rejection 的 parent 是被拒绝的 formalize

    # 保留旧字段向后兼容（add_entry 委托到 add_operation 内部填充）
    file_count: int = 0
    commit_hash: str = ""
    commit_message: str = ""
    workspace: str = ""
    backup: str = ""


class HistoryManager:
    """管理操作历史日志的读写。可通过 set_workspace 切换到项目级路径。

    v0.33: JSONL 追加写入 + threading.Lock 并发安全。
           add_operation / add_suggestion 不再全量覆写，改为逐行追加。
           超过 _COMPACT_THRESHOLD 条时自动 compact 到 _MAX_ENTRIES 条。
    """

    _workspace_path: str | None = None
    _lock = threading.Lock()

    @classmethod
    def set_workspace(cls, path: str) -> None:
        """设置当前工作项目路径。后续读写使用 .gitgo/gitgo_history.json。"""
        cls._workspace_path = path

    @staticmethod
    def _path() -> Path:
        import sys
        ws = HistoryManager._workspace_path
        if ws:
            p = Path(ws) / ".gitgo" / HISTORY_FILE
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).parent
        else:
            base = Path.cwd()
        return base / HISTORY_FILE

    # ── 读写（JSONL 格式，向后兼容旧 JSON 数组格式）────────────

    @staticmethod
    def load() -> list[HistoryEntry]:
        """从 JSONL 文件加载历史。向后兼容旧 JSON 数组格式。"""
        path = HistoryManager._path()
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []

        if not raw.strip():
            return []

        stripped = raw.lstrip()
        if stripped.startswith("{"):
            return HistoryManager._load_jsonl(raw)
        elif stripped.startswith("["):
            return HistoryManager._load_json_array(raw)
        else:
            return []

    @staticmethod
    def _load_jsonl(raw: str) -> list[HistoryEntry]:
        entries = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(HistoryEntry(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return entries

    @staticmethod
    def _load_json_array(raw: str) -> list[HistoryEntry]:
        try:
            data = json.loads(raw)
            return [HistoryEntry(**e) for e in data]
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def save(entries: list[HistoryEntry]) -> None:
        """全量覆写（compact 时使用），用 tmp + rename 保证原子性。"""
        path = HistoryManager._path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([asdict(e) for e in entries], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)  # 同一文件系统上 rename 是原子的

    @staticmethod
    def _append_one(entry: HistoryEntry) -> None:
        """追加一行 JSON 到文件末尾。调用方需持有 _lock。"""
        path = HistoryManager._path()
        line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    @classmethod
    def _compact(cls) -> None:
        """重写文件，只保留最近 _MAX_ENTRIES 条。调用方需持有 _lock。"""
        path = HistoryManager._path()
        if not path.exists():
            return
        entries = cls.load()
        if len(entries) <= _MAX_ENTRIES:
            return
        cls.save(entries[-_MAX_ENTRIES:])

    @classmethod
    def _compact_if_needed(cls) -> None:
        """行数超过阈值时触发 compact。调用方需持有 _lock。"""
        path = HistoryManager._path()
        if not path.exists():
            return
        try:
            line_count = sum(1 for _ in open(path, "r", encoding="utf-8"))
            if line_count >= _COMPACT_THRESHOLD:
                cls._compact()
        except OSError:
            pass

    # ── 公开 API ─────────────────────────────────────────────

    @classmethod
    def add_operation(cls, project_name: str, operation: str,
                      status: str = "success", detail: dict | None = None,
                      correlation_id: str = "") -> None:
        """记录一条操作历史（线程安全，JSONL 追加写入）。

        operation: "scan" | "formalize" | "sync" | "push"
                   | "triage_accept" | "triage_promote" | "triage_discard"
                   | "delete_formal" | "dissolve_formal"
        """
        entry = HistoryEntry(
            timestamp=datetime.now().isoformat(),
            project_name=project_name,
            operation=operation,
            status=status,
            detail=detail or {},
            correlation_id=correlation_id,
        )

        with cls._lock:
            cls._append_one(entry)
            cls._compact_if_needed()

    @classmethod
    def add_suggestion(cls, project_name: str, suggest_type: str,
                       ai_proposal: dict, human_decision: dict,
                       correlation_id: str = "") -> None:
        """记录 AI 建议与人的最终决策差异，供 P4 质量度量使用。

        - ``suggest_type``: "formalize" | "triage" | "summary"
        - ``ai_proposal``: agent 返回的完整建议 JSON
        - ``human_decision``: 人最终执行时的参数
        """
        entry = HistoryEntry(
            timestamp=datetime.now().isoformat(),
            project_name=project_name,
            operation=f"suggest_{suggest_type}",
            status="recorded",
            detail={
                "ai_proposal": ai_proposal,
                "human_decision": human_decision,
            },
            correlation_id=correlation_id,
        )

        with cls._lock:
            cls._append_one(entry)
            cls._compact_if_needed()

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
