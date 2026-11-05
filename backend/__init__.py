"""gitgo 引擎层 — 全项目共用的纯逻辑。

子包:
    core/       — 业务引擎 (SyncSession, config, daemon, operations)
    adapters/   — 基础设施 (文件/Git 适配器)
    models/     — 共享数据模型
    remote/     — 外部 API 连接器 (GitHub/GitLab)
"""

from importlib import import_module

# Importing a tiny child-process transport must not initialize configuration,
# providers and the full application. Keep the public facade, resolve on use.
_EXPORTS = {
    **dict.fromkeys(("Config", "ConfigManager", "ProjectConfig"), "backend.core.config"),
    **dict.fromkeys(("HistoryManager", "HistoryEntry"), "backend.core.history"),
    **dict.fromkeys(("_tr", "load_language", "available_languages"), "backend.core.i18n"),
    **dict.fromkeys(("SyncSession", "SessionStage"), "backend.core.sync_session"),
    **dict.fromkeys(("RemoteConnector", "GitHubConnector", "GitLabConnector", "create_connector"), "backend.remote"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name]), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "Config", "ConfigManager", "ProjectConfig",
    "HistoryManager", "HistoryEntry",
    "_tr", "load_language", "available_languages",
    "SyncSession", "SessionStage",
    "RemoteConnector", "GitHubConnector", "GitLabConnector", "create_connector",
]
