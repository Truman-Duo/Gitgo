"""gitgo 引擎层 — 全项目共用的纯逻辑。

子包:
    core/       — 业务引擎 (SyncSession, config, daemon, operations)
    adapters/   — 基础设施 (文件/Git 适配器)
    models/     — 共享数据模型
    remote/     — 外部 API 连接器 (GitHub/GitLab)
"""

from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.core.history import HistoryManager, HistoryEntry
from backend.core.i18n import _tr, load_language, available_languages
from backend.core.sync_session import SyncSession, SessionStage

__all__ = [
    "Config", "ConfigManager", "ProjectConfig",
    "HistoryManager", "HistoryEntry",
    "_tr", "load_language", "available_languages",
    "SyncSession", "SessionStage",
]
