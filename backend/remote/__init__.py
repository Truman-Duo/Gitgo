"""remote 包 — RemoteConnector 抽象 + GitHub/GitLab 实现"""

from .connector import RemoteConnector
from .github import GitHubConnector


def create_connector(target, token: str = ""):
    """工厂：根据 RemoteTarget.kind 创建对应的连接器。
    返回 None 表示未配置或不支持的类型。
    """
    import os
    if not target or not target.kind or target.kind == "bare":
        return None
    resolved_token = token or os.environ.get("GITHUB_TOKEN", "")
    if target.kind == "github":
        return GitHubConnector(target, resolved_token)
    return None
