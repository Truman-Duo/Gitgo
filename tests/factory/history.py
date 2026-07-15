"""HistoryManager 子系统测试数据生成器。"""

from datetime import datetime

from backend.core.history import HistoryEntry
from tests.factory import pools


class HistoryGenerator:
    def __init__(self, factory):
        self.f = factory

    def entry(self, operation: str | None = None) -> HistoryEntry:
        """生成一条随机 HistoryEntry。"""
        op = operation or self.f._pick(pools.HISTORY_OPERATIONS)
        return HistoryEntry(
            timestamp=self.f._ts(self.f._int(1, 1440)),
            project_name="testproject",
            operation=op,
            status=self.f._pick(["success", "warning", "error"]),
            detail=self._detail_for(op),
            correlation_id=self.f._next_id("corr"),
        )

    def entries(self, n: int = 20,
                operations: list[str] | None = None) -> list[HistoryEntry]:
        """生成 N 条按时间排序的 HistoryEntry。"""
        ops = operations or pools.HISTORY_OPERATIONS
        entries = [self.entry(self.f._pick(ops)) for _ in range(n)]
        entries.sort(key=lambda e: e.timestamp)
        return entries

    def _detail_for(self, operation: str) -> dict:
        rng = self.f.rng
        if operation == "unprocessed_signal":
            return {
                "signal_type": self.f._pick(pools.SIGNAL_TYPES),
                "trigger": self.f._pick(pools.FILE_PATHS),
                "rule": self.f._pick(pools.LESSON_RULES).format(
                    file=self.f._pick(pools.FILE_PATHS),
                    tool=self.f._pick(pools.TOOL_NAMES),
                    other=self.f._pick(pools.FILE_PATHS),
                    category=self.f._pick(pools.CATEGORIES),
                    action=self.f._pick(pools.ACTIONS),
                    action2=self.f._pick(pools.ACTIONS),
                ),
                "severity": self.f._pick(pools.SEVERITIES),
            }
        elif operation in ("scan", "sync", "push"):
            return {
                "file_count": rng.randint(1, 30),
                "files_changed": self.f._pick_n(pools.FILE_PATHS, rng.randint(1, 5)),
            }
        elif operation == "governance_drift":
            return {
                "rule": rng.choice(["contract_drift", "identity_integrity"]),
                "message": f"Drift detected in {self.f._pick(pools.FILE_PATHS)}",
            }
        elif operation == "policy_check_result":
            return {
                "gov_warnings": rng.randint(0, 5),
                "checks_triggered": rng.randint(1, 4),
            }
        else:
            return {"source": "test_factory"}
