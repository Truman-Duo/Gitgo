"""TestDataFactory —— 种子可复现的通用测试数据生成器。

用法:
    factory = TestDataFactory(seed=42)
    lesson = factory.lesson()               # 随机 lesson
    lessons = factory.lessons(10)           # 10 条
    chain = factory.harvest_chain()         # 完整收割链路
    chain2 = factory.e2e_knowledge_chain()  # 端到端知识链路

种子策略:
    - 固定种子 (seed=42): CI 确定性测试
    - 随机种子 (seed=0): 探索性测试，每次不同
    - 失败时打印种子 → 可直接复现: TestDataFactory(seed=12345)

子模块:
    - pools: 内置数据池 (文件路径/规则模板/信号类型等)
    - knowledge: Knowledge 子系统生成器
    - policy: Policy Engine 子系统生成器
    - agent: Agent Loop 子系统生成器
    - history: HistoryManager 子系统生成器
    - chains: 链路生成器 (跨子系统连贯数据)
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta

from tests.factory import pools
from tests.factory.knowledge import KnowledgeGenerator
from tests.factory.policy import PolicyGenerator
from tests.factory.agent import AgentGenerator
from tests.factory.history import HistoryGenerator
from tests.factory.sync import SyncGenerator
from tests.factory.chains import ChainGenerator


class TestDataFactory:
    """种子可复现的测试数据生成器。"""

    __test__ = False  # pytest 不把它当测试类

    def __init__(self, seed: int = 42):
        self.seed = seed
        self.rng = random.Random(seed)
        self._seq = 0

        # 子系统生成器
        self.knowledge = KnowledgeGenerator(self)
        self.policy = PolicyGenerator(self)
        self.agent = AgentGenerator(self)
        self.history = HistoryGenerator(self)
        self.sync = SyncGenerator(self)
        self.chains = ChainGenerator(self)

    # ── 基础工具 ──────────────────────────────────────────

    def _next_id(self, prefix: str = "") -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:04d}"

    def _pick(self, items: list, weights: list | None = None) -> object:
        if weights:
            return self.rng.choices(items, weights=weights, k=1)[0]
        return self.rng.choice(items)

    def _pick_n(self, items: list, n: int, unique: bool = False) -> list:
        if unique and n <= len(items):
            return self.rng.sample(items, n)
        return self.rng.choices(items, k=n)

    def _ts(self, minutes_ago: int = 0) -> str:
        return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()

    def _bool(self, p: float = 0.5) -> bool:
        return self.rng.random() < p

    def _int(self, lo: int, hi: int) -> int:
        return self.rng.randint(lo, hi)

    # ── Knowledge 子系统快捷方法 ──────────────────────────

    def lesson(self, **overrides):
        return self.knowledge.lesson(**overrides)

    def lessons(self, n: int = 5, **overrides):
        return self.knowledge.lessons(n, **overrides)

    def signal(self, signal_type: str | None = None):
        return self.knowledge.signal(signal_type)

    def signals(self, n: int = 5, signal_type: str | None = None):
        return self.knowledge.signals(n, signal_type)

    # ── History 子系统快捷方法 ────────────────────────────

    def history_entry(self, operation: str | None = None):
        return self.history.entry(operation)

    def history_entries(self, n: int = 20, operations: list[str] | None = None):
        return self.history.entries(n, operations)

    # ── Agent 子系统快捷方法 ──────────────────────────────

    def agent_process(self, **overrides):
        return self.agent.process(**overrides)

    # ── Policy 子系统快捷方法 ─────────────────────────────

    def policy_result(self, check_type=None):
        return self.policy.result(check_type)

    def policy_results(self):
        return self.policy.results()

    # ── Sync 子系统快捷方法 ──────────────────────────────

    def file_entry(self, **overrides):
        return self.sync.file_entry(**overrides)

    def file_entries(self, n=10):
        return self.sync.file_entries(n)

    def commit_info(self, **overrides):
        return self.sync.commit_info(**overrides)

    def commit_infos(self, n=5):
        return self.sync.commit_infos(n)

    def scan_result(self, file_count=10):
        return self.sync.scan_result(file_count)

    # ── 链路快捷方法 ──────────────────────────────────────

    def harvest_chain(self, signal_count=5, lesson_count=2):
        return self.chains.harvest_chain(signal_count, lesson_count)

    def recall_chain(self, lesson_count=10, query_count=3):
        return self.chains.recall_chain(lesson_count, query_count)

    def e2e_knowledge_chain(self, rounds=3):
        return self.chains.e2e_knowledge_chain(rounds)

    def sync_chain(self, file_count=10):
        return self.chains.sync_chain(file_count)
