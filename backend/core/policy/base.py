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

    @abstractmethod
    def check(self, session: "SyncSession",
              project: "ProjectConfig") -> list[dict]:
        """执行检查，返回告警列表。空列表 = 通过。"""
