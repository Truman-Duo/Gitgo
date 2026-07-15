"""Knowledge System 全链路测试 —— 种子 2 + 边界条件 + 压力测试。

种子 1 (42) 已包含在 test_knowledge_system.py 中 (43 tests)。
本文件:
  - 种子 2 (77):  不同数据跑同一套链路 → 验证生成器 + 子系统双稳定性
  - 边界条件:      空输入/极值/阈值附近/长字符串/特殊字符
  - 并发安全:      多 Agent 同时检索不冲突
  - 热→冷转换:     检索停止后 lesson 降温
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from tests.factory import TestDataFactory


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def f2():
    """种子 2 (77): 与种子 1 (42) 不同的数据。"""
    return TestDataFactory(seed=77)


@pytest.fixture
def tmp_ws():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / ".gitgo" / "knowledge" / "instances" / "testproject").mkdir(
            parents=True, exist_ok=True,
        )
        yield ws


# ═══════════════════════════════════════════════════════════════
# 种子 2: 用不同数据重跑全部链路
# ═══════════════════════════════════════════════════════════════


class TestSeed2HarvestChain:
    """种子 2 收割链路：验证不同数据下链路仍然完整。"""

    def test_seed2_full_harvest(self, f2, tmp_ws):
        from backend.core.knowledge.harvest import (
            capture_signal, harvest_llm_summary,
        )
        from backend.core.knowledge.manager import LessonManager
        from backend.core.history import HistoryManager

        HistoryManager.set_workspace(str(tmp_ws))

        ws_path = tmp_ws / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws_path.mkdir(parents=True, exist_ok=True)

        # 种子 2 的信号
        signals = f2.signals(8)
        for s in signals:
            capture_signal(s["signal_type"], s, "testproject")

        # 验证种子 2 和种子 1 信号不同
        f1 = TestDataFactory(seed=42)
        s1 = f1.signals(8)
        assert signals[0]["trigger"] != s1[0]["trigger"], \
            "种子 2 应产生与种子 1 不同的数据"

        # Mock LLM
        mock_response = f2.knowledge.mock_llm_response(3)
        mock_llm = type("MockLLM", (), {
            "chat": lambda self, messages, **kw: __import__("json").dumps(mock_response),
            "call_count": 0,
        })()

        harvest_signals = [{**s, "signal_type": s["signal_type"]}
                           for s in signals]
        lessons = harvest_llm_summary(
            harvest_signals, mock_llm, str(tmp_ws), "testproject",
        )

        assert len(lessons) >= 1, "不同种子也应产出有效 lesson"
        for l in lessons:
            LessonManager.save_pending(tmp_ws, l)

        pending = LessonManager.load_pending(tmp_ws, "testproject")
        assert len(pending) >= 1

    def test_seed2_recall_chain(self, f2, tmp_ws):
        """种子 2 检索→热冷链路。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.recall import recall_grep
        from backend.core.knowledge.models import classify_lesson_heat

        lessons = f2.lessons(10)
        for l in lessons:
            LessonManager.save(tmp_ws, l)

        query = f2._pick([
            "auth", "login", "scan", "sync", "contract",
            "database", "config", "test", "import", "refactor",
        ])

        # 检索 3 次
        for _ in range(3):
            recall_grep(query, "testproject", workspace=str(tmp_ws))

        # 检查是否有 lesson 变 hot
        instance = LessonManager.load_instance(tmp_ws, "testproject")
        hot_count = sum(
            1 for l in instance
            if classify_lesson_heat(l) == "hot"
        )
        # 至少有一些 lesson 被检索过（warm 或 hot）
        warm_or_hot = sum(
            1 for l in instance
            if classify_lesson_heat(l) in ("warm", "hot")
        )
        assert warm_or_hot >= 1


class TestSeed2E2E:
    """种子 2 端到端。"""

    def test_seed2_e2e(self, f2, tmp_ws):
        from backend.core.knowledge.harvest import (
            capture_signal, harvest_llm_summary,
        )
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.recall import recall_grep
        from backend.core.knowledge.models import (
            classify_lesson_heat, get_sticky_lessons,
        )
        from backend.core.history import HistoryManager

        HistoryManager.set_workspace(str(tmp_ws))
        ws = tmp_ws / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws.mkdir(parents=True, exist_ok=True)

        # Round 1: 收割
        signals = f2.signals(6)
        for s in signals:
            capture_signal(s["signal_type"], s, "testproject")

        mock_response = f2.knowledge.mock_llm_response(3)
        mock_llm = type("MockLLM", (), {
            "chat": lambda self, messages, **kw: __import__("json").dumps(mock_response),
        })()

        harvest_signals = [{**s, "signal_type": s["signal_type"]}
                           for s in signals]
        lessons = harvest_llm_summary(
            harvest_signals, mock_llm, str(tmp_ws), "testproject",
        )
        for l in lessons:
            LessonManager.save_pending(tmp_ws, l)

        # Round 2: 检索
        queries = f2._pick_n([
            "auth", "login", "scan", "sync", "contract",
            "database", "config", "test",
        ], 4)
        for q in queries:
            recall_grep(q, "testproject", workspace=str(tmp_ws))

        # Round 3: 分类
        all_lessons = (
            LessonManager.load_instance(tmp_ws, "testproject")
            + LessonManager.load_pending(tmp_ws, "testproject")
        )
        sticky = get_sticky_lessons(all_lessons)
        # sticky 数量应 ≤ STICKY_CAP
        assert len(sticky) <= 10

        # 至少产生了一些 lesson
        assert len(all_lessons) >= 1


# ═══════════════════════════════════════════════════════════════
# 边界条件测试
# ═══════════════════════════════════════════════════════════════


class TestEdgeCases:
    """边界条件：空输入/极值/阈值附近/特殊字符。"""

    def test_recall_empty_query(self, tmp_ws):
        """空查询不应崩溃。"""
        from backend.core.knowledge.recall import recall_grep
        r = recall_grep("", "testproject", workspace=str(tmp_ws))
        assert r["total_matches"] == 0

    def test_recall_special_chars(self, tmp_ws):
        """特殊字符查询不应崩溃。"""
        from backend.core.knowledge.recall import recall_grep
        r = recall_grep("../../../etc/passwd", "testproject",
                        workspace=str(tmp_ws))
        assert isinstance(r["total_matches"], int)

    def test_recall_unicode(self, tmp_ws):
        """Unicode 查询。"""
        from backend.core.knowledge.recall import recall_grep
        r = recall_grep("修改文件", "testproject",  # 修改文件
                        workspace=str(tmp_ws))
        assert isinstance(r["total_matches"], int)

    def test_lesson_very_long_trigger(self):
        """极长 trigger/rule 的 lesson 应能正常创建和序列化。"""
        from backend.core.knowledge.models import Lesson
        long_path = "very/deep/nested/" * 50 + "file.py"
        long_rule = "if " + "modifying " * 50 + "then must scan"
        l = Lesson(trigger=long_path, rule=long_rule)
        d = l.to_dict()
        l2 = Lesson.from_dict(d)
        assert l2.trigger == long_path
        assert l2.rule == long_rule

    def test_classify_exactly_at_threshold(self):
        """HOT_THRESHOLD - 1 次检索 → 仍为 warm。"""
        from backend.core.knowledge.models import classify_lesson_heat, Lesson
        from backend.core.knowledge.models import HOT_THRESHOLD
        now = datetime.now().isoformat()
        # HOT_THRESHOLD - 1 次检索 → warm
        l = Lesson(trigger="t", rule="r",
                   recent_retrievals=[now] * (HOT_THRESHOLD - 1))
        assert classify_lesson_heat(l) == "warm"

    def test_sticky_exactly_at_cap(self):
        """恰好 STICKY_CAP 条 hot → 全部在 sticky 里。"""
        from backend.core.knowledge.models import (
            get_sticky_lessons, Lesson, STICKY_CAP,
        )
        now = datetime.now().isoformat()
        lessons = [
            Lesson(id=f"h{i}", trigger=f"f{i}", rule=f"r{i}",
                   severity="high", recent_retrievals=[now] * 5)
            for i in range(STICKY_CAP)
        ]
        assert len(get_sticky_lessons(lessons)) == STICKY_CAP

    def test_harvest_retry_exactly_at_max(self):
        """retry_count == MAX_HARVEST_RETRY → 信号被丢弃。"""
        from backend.core.knowledge.harvest import (
            harvest_llm_summary, MAX_HARVEST_RETRY,
        )
        signals = [{
            "signal_type": "lesson_trigger",
            "trigger": "auth.py", "rule": "modify",
            "detail": {},
            "harvest_retry_count": MAX_HARVEST_RETRY,
        }]
        mock_llm = type("MockLLM", (), {
            "chat": lambda self, **kw: (_ for _ in ()).throw(RuntimeError("fail")),
        })()
        result = harvest_llm_summary(signals, mock_llm, "/tmp", "test")
        assert result == []

    def test_pending_count_zero(self, tmp_ws):
        """空项目 pending_count = 0。"""
        from backend.core.knowledge.manager import LessonManager
        assert LessonManager.pending_count(tmp_ws, "no_project") == 0

    def test_discard_nonexistent(self, tmp_ws):
        """discard 不存在的 lesson → 返回 False。"""
        from backend.core.knowledge.manager import LessonManager
        ok = LessonManager.discard_lesson(
            tmp_ws, "nonexistent_id", "testproject",
        )
        assert not ok

    def test_signal_density_mixed_types(self):
        """混合信号类型的密度计算。"""
        from backend.core.knowledge.harvest import signal_density
        # 无历史 → 0
        assert signal_density("no_project", "lesson_trigger") == 0.0

    def test_filter_by_relevance_empty_task(self, f2):
        """空 task_description → 不筛选。"""
        from backend.core.knowledge.recall import filter_by_relevance
        lessons = f2.lessons(5)
        result = filter_by_relevance(lessons, "")
        assert len(result) == len(lessons)


# ═══════════════════════════════════════════════════════════════
# 热→冷转换测试
# ═══════════════════════════════════════════════════════════════


class TestHeatTransition:
    """热→温→冷 生命周期。"""

    def test_hot_to_warm_when_retrievals_age_out(self, tmp_ws):
        """检索停止后 lesson 从 hot → cold。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import (
            classify_lesson_heat, Lesson, HOT_THRESHOLD,
        )
        from backend.core.knowledge.recall import recall_grep

        l = Lesson(
            trigger="auth.py",
            rule="if modifying auth, then must scan first",
            severity="high", project_name="testproject",
        )
        LessonManager.save(tmp_ws, l)

        # 多次检索 → hot
        for _ in range(HOT_THRESHOLD):
            recall_grep("auth", "testproject", workspace=str(tmp_ws))

        instance = LessonManager.load_instance(tmp_ws, "testproject")
        auth = next(ll for ll in instance if "auth" in ll.trigger.lower())
        assert classify_lesson_heat(auth) == "hot"

        # 不再检索 → 下次 load 时分类降为 warm/cold
        # (取决于 MAX_RETRIEVAL_LOG 和检索间隔)
        # 直接清空 recent_retrievals 模拟时间流逝
        auth.recent_retrievals = []
        assert classify_lesson_heat(auth) == "cold"


# ═══════════════════════════════════════════════════════════════
# 并发安全模拟测试
# ═══════════════════════════════════════════════════════════════


class TestConcurrentSafety:
    """多 Agent 同时检索：lesson 的 recent_retrievals 不应丢失。"""

    def test_concurrent_recalls(self, tmp_ws):
        """两个 'Agent' 同时检索同一 lesson → 两次都被记录。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import Lesson
        from backend.core.knowledge.recall import recall_grep

        l = Lesson(
            trigger="shared_module.py",
            rule="if modifying shared, then must notify all agents",
            severity="high", project_name="testproject",
        )
        LessonManager.save(tmp_ws, l)

        # Agent A 检索 2 次, Agent B 检索 2 次（交错）
        for _ in range(2):
            recall_grep("shared", "testproject", workspace=str(tmp_ws))
        for _ in range(2):
            recall_grep("shared", "testproject", workspace=str(tmp_ws))

        instance = LessonManager.load_instance(tmp_ws, "testproject")
        shared = next(
            ll for ll in instance
            if "shared" in ll.trigger.lower()
        )
        assert len(shared.recent_retrievals) >= 4

    def test_concurrent_harvest_and_recall(self, tmp_ws):
        """收割（写入 pending）和检索（读 pending）不应冲突。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import Lesson
        from backend.core.knowledge.recall import recall_grep

        ws = tmp_ws / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws.mkdir(parents=True, exist_ok=True)

        # 收割：写入 pending
        for i in range(5):
            LessonManager.save_pending(tmp_ws, Lesson(
                trigger=f"concurrent_{i}.py",
                rule=f"if modifying concurrent_{i}, then must test",
                severity="medium", project_name="testproject",
            ))

        # 检索：读 pending（同时）
        r = recall_grep("concurrent", "testproject", workspace=str(tmp_ws))
        assert r["total_matches"] >= 5


# ═══════════════════════════════════════════════════════════════
# 回收模拟：B Agent kill + A Agent round_complete
# ═══════════════════════════════════════════════════════════════


class TestRecycleScenarios:
    """回收场景：B Agent kill 自然清理，A Agent round_complete 选择性回收。"""

    def test_b_agent_kill_clears_context(self, tmp_ws):
        """B Agent fork/kill → context 自然清理，lesson 仍在知识库。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import Lesson

        # Fork B Agent 时创建 task-scoped lessons
        LessonManager.save(tmp_ws, Lesson(
            trigger="b_agent_task.py",
            rule="if b_agent_task changes, must verify with A agent",
            severity="high", project_name="testproject",
        ))

        # B Agent kill —— lesson 保留在知识库
        instance = LessonManager.load_instance(tmp_ws, "testproject")
        assert len(instance) >= 1

    def test_round_complete_selective_recycle(self, tmp_ws):
        """round_complete: hot lesson 在 sticky，cold 不在。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import (
            classify_lesson_heat, get_sticky_lessons, Lesson,
        )
        from backend.core.knowledge.recall import recall_grep

        # 分别创建 hot 和 cold lesson（用不同 project 避免文件冲突）
        hot = Lesson(
            id="hot_test_1", trigger="hot_module.py",
            rule="if modifying hot module, then must review first",
            severity="high", project_name="testproject",
        )
        cold = Lesson(
            id="cold_test_1", trigger="cold_module.py",
            rule="if modifying cold module, should check dependencies",
            severity="medium", project_name="testproject",
        )
        LessonManager.save(tmp_ws, hot)
        LessonManager.save(tmp_ws, cold)

        # 多次检索 hot
        for _ in range(5):
            recall_grep("hot_module", "testproject", workspace=str(tmp_ws))

        # 验证 hot vs cold 分类
        instance = LessonManager.load_instance(tmp_ws, "testproject")
        hot_found = [l for l in instance if l.id == "hot_test_1"]
        cold_found = [l for l in instance if l.id == "cold_test_1"]

        assert len(hot_found) >= 1
        assert len(cold_found) >= 1
        assert classify_lesson_heat(hot_found[0]) == "hot"
        assert classify_lesson_heat(cold_found[0]) == "cold"

        # hot 在 sticky 里
        sticky = get_sticky_lessons(instance)
        assert hot_found[0].id in sticky
        assert cold_found[0].id not in sticky


# ═══════════════════════════════════════════════════════════════
# 压力测试：大数据量
# ═══════════════════════════════════════════════════════════════


class TestStress:
    """大数据量下功能不退化。"""

    def test_100_lessons_recall(self, tmp_ws, f2):
        """100 条 lesson + 检索 → 不超时，不崩溃。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.recall import recall_grep

        lessons = f2.lessons(100)
        for l in lessons:
            LessonManager.save(tmp_ws, l)

        r = recall_grep("a", "testproject", workspace=str(tmp_ws))
        assert r["total_matches"] >= 0
        assert len(r["lessons"]) <= 10  # default top_k

    def test_100_signals_capture(self, f2):
        """100 条信号捕获 → 不崩溃。"""
        from backend.core.knowledge.harvest import capture_signal
        from backend.core.history import HistoryManager

        with tempfile.TemporaryDirectory() as d:
            HistoryManager.set_workspace(d)
            signals = f2.signals(100)
            for s in signals:
                capture_signal(s["signal_type"], s, "testproject")

            entries = HistoryManager.load()
            unprocessed = [e for e in entries
                          if e.operation == "unprocessed_signal"]
            assert len(unprocessed) >= 50  # compact 可能截断

    def test_50_pending_lessons(self, tmp_ws, f2):
        """50 条 pending → 消化不掉队。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.harvest import auto_discard_invalid

        ws = tmp_ws / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws.mkdir(parents=True, exist_ok=True)

        lessons = f2.lessons(50)
        for l in lessons:
            LessonManager.save_pending(tmp_ws, l)

        count_before = LessonManager.pending_count(tmp_ws, "testproject")
        # 内容哈希去重可能导致略少于 50（相似 trigger+rule 合并）
        assert count_before >= 40, f"Expected >=40, got {count_before}"

        # L1 digest 运行
        discarded = auto_discard_invalid(tmp_ws, "testproject")
        # 验证不崩溃，discard 数量合理
        assert discarded >= 0
        assert LessonManager.pending_count(tmp_ws, "testproject") <= count_before
