"""remote 包 — RemoteConnector 抽象 + GitHub/GitLab 实现"""

from .connector import RemoteConnector
from .github import GitHubConnector
from .gitlab import GitLabConnector

__all__ = [
    "RemoteConnector",
    "GitHubConnector",
    "GitLabConnector",
    "create_connector",
]


def create_connector(target, token: str = ""):
    """工厂：根据 RemoteTarget.kind 创建对应的连接器。
    返回 None 表示未配置或不支持的类型。
    """
    import os
    if not target or not target.kind or target.kind == "bare":
        return None

    kind = target.kind
    if kind == "github":
        resolved_token = token or os.environ.get("GITHUB_TOKEN", "")
        return GitHubConnector(target, resolved_token)
    elif kind == "gitlab":
        resolved_token = token or os.environ.get("GITLAB_TOKEN", "")
        return GitLabConnector(target, resolved_token)

    return None
