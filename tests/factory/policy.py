"""Policy Engine 子系统测试数据生成器。"""

from tests.factory import pools


class PolicyGenerator:
    def __init__(self, factory):
        self.f = factory

    def result(self, check_type: str | None = None) -> dict:
        """生成一条随机 PolicyEngine check 结果。"""
        ct = check_type or self.f._pick([
            "lesson_triggers", "contract_drift",
            "identity_integrity", "dependency_chain",
        ])
        if ct == "lesson_triggers":
            return {
                "lesson_id": self.f._next_id("lesson"),
                "severity": self.f._pick(pools.SEVERITIES),
                "rule": self.f._pick(pools.LESSON_RULES).format(
                    file=self.f._pick(pools.FILE_PATHS),
                    tool=self.f._pick(pools.TOOL_NAMES),
                    other=self.f._pick(pools.FILE_PATHS),
                    category=self.f._pick(pools.CATEGORIES),
                    action=self.f._pick(pools.ACTIONS),
                    action2=self.f._pick(pools.ACTIONS),
                ),
                "file": self.f._pick(pools.FILE_PATHS),
                "dangerous_tools": self.f._pick_n(pools.TOOL_NAMES, self.f._int(0, 2)),
                "prerequisite_tools": self.f._pick_n(pools.TOOL_NAMES, self.f._int(0, 2)),
            }
        elif ct == "contract_drift":
            return {
                "rule": "contract_drift",
                "level": self.f._pick(["warning", "error"]),
                "message": f"文件 {self.f._pick(pools.FILE_PATHS)} 存在合约漂移",
                "file": self.f._pick(pools.FILE_PATHS),
            }
        elif ct == "identity_integrity":
            return {
                "rule": self.f._pick(["mass_override", "identity_file_deletion", "structure_collapse"]),
                "level": self.f._pick(["warning", "error"]),
                "message": f"Identity integrity violation detected",
            }
        else:
            return {
                "message": f"Dependency chain affected for {self.f._pick(pools.FILE_PATHS)}",
                "affected_files": self.f._pick_n(pools.FILE_PATHS, self.f._int(1, 3)),
            }

    def results(self) -> dict:
        """生成完整的 PolicyEngine.run() 输出格式。"""
        return {
            "lesson_triggers": [self.result("lesson_triggers") for _ in range(self.f._int(0, 3))],
            "contract_drift": [self.result("contract_drift") for _ in range(self.f._int(0, 2))],
            "identity_integrity": [self.result("identity_integrity") for _ in range(self.f._int(0, 1))],
            "dependency_chain": [self.result("dependency_chain") for _ in range(self.f._int(0, 2))],
        }
