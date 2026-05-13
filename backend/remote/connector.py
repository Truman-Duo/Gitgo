"""RemoteConnector ABC — GitHub/GitLab API 抽象基类"""

from abc import ABC, abstractmethod

from backend.models import RemoteTarget


class RemoteConnector(ABC):
    """远程仓库连接器基类"""

    def __init__(self, target: RemoteTarget, token: str):
        self.target = target
        self.token = token

    @abstractmethod
    def is_configured(self) -> bool:
        """检查是否已配置（有 token 和有效 URL）"""
        ...

    @abstractmethod
    def create_release(self, tag: str, name: str, body: str) -> tuple[bool, str]:
        """创建 Release，返回 (success, url_or_error)"""
        ...

    @abstractmethod
    def get_repo_info(self) -> dict:
        """获取仓库基本信息"""
        ...

    # ── 远期预留 ─────────────────────────────────────────

    def list_issues(self, state: str = "open") -> list:
        """获取 Issue 列表（Phase 5.2）"""
        raise NotImplementedError

    def create_pr(self, title: str, body: str, head: str, base: str = "main") -> tuple[bool, str]:
        """创建 Pull Request（Phase 5.2）"""
        raise NotImplementedError
