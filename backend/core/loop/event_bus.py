"""EventBus —— 进程内 pub/sub 事件骨干。

ToolPipeline 只发射事件，不知道谁在监听。SignalBus、TranscriptBuilder、
Telemetry、Dashboard 都可以独立订阅。

事件类型分为两类：
- ToolEvent: 单次工具调用的生命周期事件
- ExecutionEvent: 工具批次事务的生命周期事件
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
import logging

LOG = logging.getLogger(__name__)


# ── 事件类型 ────────────────────────────────────────────────

@dataclass
class ToolEvent:
    """单次工具调用的生命周期事件。"""
    event_type: str      # ToolPrepareStarted / ToolResultReady / ...
    execution_id: str
    tool_name: str = ""
    call_index: int = 0
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ExecutionEvent:
    """工具批次事务的生命周期事件。"""
    event_type: str      # ExecutionStarted / ExecutionCompleted / ...
    execution_id: str
    reason: str = ""
    results: list = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ── EventBus ────────────────────────────────────────────────

class EventBus:
    """进程内 pub/sub。

    轻量实现，不做跨进程——gitgo 是单进程 runtime，Dashboard 通过 daemon client
    拉数据。EventBus 只服务同进程内的解耦（治理、转录、遥测等）。

    用法:
        bus = EventBus()
        bus.subscribe("ToolResultReady", handler)
        bus.emit(ToolEvent("ToolResultReady", execution_id="..."))
    """

    def __init__(self, on_error: Callable | None = None):
        self._subscribers: dict[str, list[Callable]] = {}
        self._wildcard_subscribers: list[Callable] = []
        self._on_error = on_error
        self.delivery_failures: list[dict] = []

    def subscribe(self, event_type: str, handler: Callable) -> None:
        """订阅特定事件类型。handler 接收 ToolEvent | ExecutionEvent。"""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)

    def subscribe_all(self, handler: Callable) -> None:
        """订阅所有事件（用于调试/审计/replay）。"""
        self._wildcard_subscribers.append(handler)

    def emit(self, event: ToolEvent | ExecutionEvent) -> None:
        """发射事件。异常不传播——单个订阅者出错不影响其他。"""
        handlers = self._subscribers.get(event.event_type, [])
        for handler in handlers + self._wildcard_subscribers:
            try:
                handler(event)
            except Exception as exc:
                failure = {"event_type": event.event_type,
                           "execution_id": event.execution_id,
                           "handler": getattr(handler, "__name__", type(handler).__name__),
                           "error_type": type(exc).__name__}
                self.delivery_failures.append(failure)
                del self.delivery_failures[:-64]
                LOG.error("event delivery failed: %s", failure)
                if self._on_error is not None:
                    try:
                        self._on_error(failure)
                    except Exception:
                        LOG.exception("event failure notification failed; retained in delivery_failures")

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        """取消订阅。"""
        if event_type in self._subscribers:
            self._subscribers[event_type] = [
                h for h in self._subscribers[event_type] if h is not handler
            ]
