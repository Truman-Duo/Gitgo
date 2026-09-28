"""Policy Engine 策略抽象基类。"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.sync_session import SyncSession
    from backend.core.config import ProjectConfig


class PolicyCheck(ABC):
    """一个治理检查策略。

    子类必须设置 name/description 并实现 check()。
    """

    name: str = ""
    description: str = ""
    # ``None`` means the check is universally applicable.  Workspace-mutation
    # checks opt into the task kinds for which their evidence is relevant, so
    # a conversational answer cannot synchronously trigger an action audit.
    applicable_task_kinds: frozenset[str] | None = None

    def applies_to(self, task_kind: str) -> bool:
        if not task_kind or self.applicable_task_kinds is None:
            return True
        return task_kind in self.applicable_task_kinds

    @abstractmethod
    def check(self, session: "SyncSession",
              project: "ProjectConfig") -> list[dict]:
        """执行检查，返回告警列表。空列表 = 通过。"""
