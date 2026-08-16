"""SyncSession — Runtime Kernel (Layer 1: Operational State Machine)

Gitgo 的运行时核心。18 个 step_*() 方法驱动状态转移。
GUI / CUI / CLI / Daemon 四种前端共用此状态机。

Operational State Machine:
  IDLE → SCANNING → SELECTING → COMMITTING → SYNCING → PUSHING → IDLE
            ↘ TRIAL_CHECKING → TRIAL_REVIEWING → INCOMING_CONFIRMING

规则:
  - 所有状态转移必须通过 step_*() 方法，禁止直接修改 self.stage
  - 每个 step_*() 方法在成功时写入对应的 governance event
  - 硬编码调用序列（非 event-driven）——参见 RuntimeConstitution §4 Observer Constraint

纯 Python 实现，无 Qt 依赖。
"""

from backend.core.sync_session.models import SessionStage, FormalCommit
from backend.core.sync_session.session import SyncSession

__all__ = ["SyncSession", "SessionStage", "FormalCommit"]
