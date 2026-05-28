"""StateReader — 统一的治理状态查询接口

不引入新文件格式，不替代 HistoryManager 或 ConfigManager。
所有方法从已有持久化文件读取，封装路径逻辑。
Agent 可直接调用，不需要知道文件路径和格式细节。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from backend.core.history import HistoryManager


class StateReader:
    """统一的治理状态查询接口。"""

    @staticmethod
    def get_formal_commits(project_name: str, workspace_path: str = "") -> list[dict]:
        """从 session.json + history.json 重建 formal commits 当前状态。

        优先从 session.json 读取当前快照；若不存在则从 governance_synced/
        governance_pushed 等 event 重建。
        """
        import json
        ws = Path(workspace_path) if workspace_path else None
        if ws and (ws / ".gitgo" / "session.json").exists():
            try:
                data = json.loads((ws / ".gitgo" / "session.json").read_text(encoding="utf-8"))
                fcs = data.get("formal_commits", [])
                if fcs:
                    return fcs
            except (json.JSONDecodeError, OSError):
                pass

        # fallback: 从 history events 重建
        entries = HistoryManager.load()
        pushed = set()
        synced = set()
        for e in entries:
            if e.project_name != project_name:
                continue
            if e.operation == "governance_pushed" and e.detail:
                for c in (e.detail.get("commits") or []):
                    pushed.add(c)
            elif e.operation == "governance_synced" and e.detail:
                c = e.detail.get("commit", "")
                if c:
                    synced.add(c)
        return [{"commit": c, "synced": c in synced, "pushed": c in pushed}
                for c in synced | pushed]

    @staticmethod
    def get_contract(workspace_path: str) -> dict | None:
        """从 .gitgo/contract.yaml 读取项目合约。"""
        from backend.core.contract import ContractManager
        c = ContractManager.load(Path(workspace_path))
        return c.to_dict() if c else None

    @staticmethod
    def get_lessons(workspace_path: str, project_name: str = "",
                    layer: str = "instance") -> list[dict]:
        """从 .gitgo/knowledge/ 读取 lesson。

        layer: "abstract" | "instance" | "pending"
        """
        from backend.core.knowledge.lesson import LessonManager
        ws = Path(workspace_path)
        if layer == "abstract":
            lessons = LessonManager.load_abstract(ws)
        elif layer == "pending" and project_name:
            lessons = LessonManager.load_pending(ws, project_name)
        else:
            lessons = LessonManager.load_instance(ws, project_name) if project_name else []
        return [l.to_dict() for l in lessons]

    @staticmethod
    def get_integrity_warnings(project_name: str, limit: int = 20) -> list[dict]:
        """从 HistoryManager 查询 integrity_warning 记录。"""
        entries = HistoryManager.load()
        warnings = [
            {"timestamp": e.timestamp, "detail": e.detail}
            for e in entries
            if e.project_name == project_name
            and e.operation == "integrity_warning"
        ]
        return warnings[-limit:]

    @staticmethod
    def get_memory_snapshots(backup_path: str) -> list[dict]:
        """列出 .gitgo/memories/ 下的快照。"""
        from backend.core.identity.snapshot import list_memory_snapshots
        return list_memory_snapshots(Path(backup_path))

    @staticmethod
    def get_governance_events(project_name: str, event_type: str = "",
                              limit: int = 50) -> list[dict]:
        """查询 governance_* event 记录。"""
        entries = HistoryManager.load()
        result = []
        for e in entries:
            if e.project_name != project_name:
                continue
            if not e.operation.startswith("governance_"):
                continue
            if event_type and e.operation != event_type:
                continue
            result.append({
                "timestamp": e.timestamp,
                "operation": e.operation,
                "status": e.status,
                "detail": e.detail,
                "correlation_id": e.correlation_id,
            })
        return result[-limit:]
