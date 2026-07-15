"""Agent Loop 子系统测试数据生成器。"""

from tests.factory import pools


class AgentGenerator:
    def __init__(self, factory):
        self.f = factory

    def process(self, **overrides):
        """生成一个随机 AgentProcess。"""
        from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel

        return AgentProcess(
            process_id=overrides.pop("process_id", self.f._next_id("proc")),
            role=overrides.pop("role", self.f._pick(pools.AGENT_ROLES)),
            ring_level=overrides.pop("ring_level",
                self.f._pick([RingLevel.RING_0, RingLevel.RING_3])),
            max_steps=overrides.pop("max_steps", self.f._int(10, 100)),
            steps_used=overrides.pop("steps_used", self.f._int(0, 20)),
            status=overrides.pop("status",
                self.f._pick([ProcessStatus.RUNNING, ProcessStatus.COMPLETED,
                              ProcessStatus.KILLED])),
            parent_id=overrides.pop("parent_id", None),
            created_at=overrides.pop("created_at", self.f._ts(self.f._int(1, 1440))),
            **overrides,
        )
