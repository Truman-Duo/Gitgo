"""SyncSession 装配 — 组合基座与各阶段 mixin 为最终状态机类。"""

from __future__ import annotations

from backend.core.sync_session.base import SyncSessionBase
from backend.core.sync_session.triage import TrialMixin
from backend.core.sync_session.scan import ScanMixin
from backend.core.sync_session.commit import CommitMixin
from backend.core.sync_session.syncpush import SyncPushMixin
from backend.core.sync_session.finalize import FinalizeMixin
from backend.core.sync_session.persist import PersistenceMixin


class SyncSession(
    FinalizeMixin,
    SyncPushMixin,
    CommitMixin,
    ScanMixin,
    TrialMixin,
    PersistenceMixin,
    SyncSessionBase,
):
    """工作流状态机 — 编排 scan → commit → sync → push 全流程。

    交互模式（GUI/CUI）：覆盖决策钩子，然后逐个调用 step_*() 方法。
    Daemon 模式：调用 run_full_workflow() 自动走完所有步骤。
    """
