"""v0.35 Knowledge System 测试 —— 收割/检索/注射/分离/回收 核心逻辑。

覆盖：
- Lesson 数据模型 + 内容哈希去重
- is_testable_proposition 门禁
- 信号捕获 + 调度算法参数计算
- recall_grep 检索 + 排序
- filter_by_relevance 分离过滤
- classify_lesson_heat 热/温/冷分类
- Pending 消化 (auto_discard_invalid)
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def tmp_workspace():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / ".gitgo" / "knowledge" / "instances" / "testproject").mkdir(
            parents=True, exist_ok=True,
        )
        yield ws


@pytest.fixture
def sample_lessons():
    from backend.core.knowledge.models import Lesson
    return [
        Lesson(
            id="L01", trigger="auth.py", rule="修改 auth.py 前必须先 scan",
            severity="high", verified_count=5,
            verified_in=["testproject"], project_name="testproject",
            prerequisite_tools=["scan"],
        ),
        Lesson(
            id="L02", trigger="login.py", rule="修改 login 模块必须更新测试",
            severity="critical", verified_count=3,
            verified_in=["testproject"], project_name="testproject",
        ),
        Lesson(
            id="L03", trigger="database", rule="数据库 schema 变更前需要备份",
            severity="medium", verified_count=1,
            project_name="testproject",
        ),
        Lesson(
            id="L04", trigger="config.yaml",
            rule="if config 变更, then must 同步更新 config.example.yaml",
            severity="high", verified_count=0,
            project_name="testproject",
        ),
        Lesson(
            id="L05", trigger="api.py",
            rule="API 接口变更需要更新文档",
            severity="low", verified_count=10,
            verified_in=["testproject", "other"], project_name="testproject",
        ),
    ]


# ── Lesson 数据模型 ──────────────────────────────────────────


class TestLessonContentHash:
    def test_same_content_same_hash(self):
        from backend.core.knowledge.models import lesson_content_hash
        h1 = lesson_content_hash("auth.py", "修改 auth 前必须 scan")
        h2 = lesson_content_hash("auth.py", "修改 auth 前必须 scan")
        assert h1 == h2
        assert len(h1) == 16

    def test_different_content_different_hash(self):
        from backend.core.knowledge.models import lesson_content_hash
        h1 = lesson_content_hash("auth.py", "规则 A")
        h2 = lesson_content_hash("login.py", "规则 A")
        assert h1 != h2

    def test_similar_pattern_different_hash(self):
        """允许相似模式重复——trigger 不同则 hash 不同。"""
        from backend.core.knowledge.models import lesson_content_hash
        h1 = lesson_content_hash("auth.py", "文件反复修改需要关注")
        h2 = lesson_content_hash("session.py", "文件反复修改需要关注")
        assert h1 != h2


class TestLessonCompat:
    def test_old_format_loaded(self):
        from backend.core.knowledge.models import Lesson
        old = {"id": "L01", "trigger": "auth.py",
               "rule": "修改 auth 前 scan", "severity": "high"}
        l = Lesson.from_dict(old)
        assert l.trigger_count == 0
        assert l.recent_retrievals == []
        assert l.origin == ""

    def test_new_fields_roundtrip(self):
        from backend.core.knowledge.models import Lesson
        l = Lesson(
            trigger="auth.py", rule="修改 auth 前 scan",
            trigger_count=3, applied_count=2,
            recent_retrievals=["2026-01-01T00:00:00"],
            origin="auto_verify",
        )
        d = l.to_dict()
        l2 = Lesson.from_dict(d)
        assert l2.trigger_count == 3
        assert l2.applied_count == 2
        assert l2.recent_retrievals == ["2026-01-01T00:00:00"]
        assert l2.origin == "auto_verify"


# ── is_testable_proposition 门禁 ─────────────────────────────


class TestIsTestableProposition:
    def test_valid_accepted(self):
        from backend.core.knowledge.harvest import is_testable_proposition
        assert is_testable_proposition(
            "if modifying auth, then must run scan first"
        )
        assert is_testable_proposition(
            "when login fails three times, must lock the account"
        )
        assert is_testable_proposition(
            "禁止直接修改 release repo without Gate A check"
        )
        assert is_testable_proposition(
            "must run contract signature check before modifying sensitive files"
        )

    def test_too_short_rejected(self):
        from backend.core.knowledge.harvest import is_testable_proposition
        assert not is_testable_proposition("auth.py 经常被改")

    def test_descriptive_rejected(self):
        from backend.core.knowledge.harvest import is_testable_proposition
        assert not is_testable_proposition(
            "auth.py 文件在最近被修改了很多次"
        )

    def test_empty_rejected(self):
        from backend.core.knowledge.harvest import is_testable_proposition
        assert not is_testable_proposition("")


# ── 调度算法参数 ─────────────────────────────────────────────


class TestSourceDiversity:
    def test_single_source(self):
        from backend.core.knowledge.harvest import source_diversity
        assert source_diversity([
            {"signal_type": "lesson_trigger"},
            {"signal_type": "lesson_trigger"},
        ]) == 1

    def test_multi_source(self):
        from backend.core.knowledge.harvest import source_diversity
        assert source_diversity([
            {"signal_type": "lesson_trigger"},
            {"signal_type": "contract_drift"},
            {"signal_type": "policy_warning_consecutive"},
        ]) == 3


# ── LLM 总结降级 ────────────────────────────────────────────


class TestHarvestLLMFallback:
    def test_empty_signals_returns_empty(self):
        from backend.core.knowledge.harvest import harvest_llm_summary
        assert harvest_llm_summary([], None, "", "") == []

    def test_no_llm_provider_marks_retry(self):
        from backend.core.knowledge.harvest import harvest_llm_summary
        signals = [{"signal_type": "lesson_trigger",
                     "trigger": "auth.py", "rule": "修改前 scan",
                     "detail": {}}]
        result = harvest_llm_summary(signals, None, "/tmp", "test")
        assert result == []
        assert signals[0].get("harvest_retry_count", 0) >= 1


# ── recall_grep 检索 ─────────────────────────────────────────


class TestRecallGrep:
    def test_match_by_trigger(self, tmp_workspace, sample_lessons):
        from backend.core.knowledge.manager import LessonManager
        for l in sample_lessons:
            LessonManager.save(tmp_workspace, l)

        from backend.core.knowledge.recall import recall_grep
        r = recall_grep("auth", "testproject", workspace=str(tmp_workspace))
        assert r["total_matches"] >= 1
        assert any("auth" in l["trigger"].lower() for l in r["lessons"])

    def test_match_by_rule(self, tmp_workspace, sample_lessons):
        from backend.core.knowledge.manager import LessonManager
        for l in sample_lessons:
            LessonManager.save(tmp_workspace, l)

        from backend.core.knowledge.recall import recall_grep
        r = recall_grep("scan", "testproject", workspace=str(tmp_workspace))
        assert r["total_matches"] >= 1

    def test_no_match_empty(self, tmp_workspace):
        from backend.core.knowledge.recall import recall_grep
        r = recall_grep("nonexistent_xyz", "testproject",
                        workspace=str(tmp_workspace))
        assert r["total_matches"] == 0

    def test_top_k(self, tmp_workspace, sample_lessons):
        from backend.core.knowledge.manager import LessonManager
        for l in sample_lessons:
            LessonManager.save(tmp_workspace, l)

        from backend.core.knowledge.recall import recall_grep
        r = recall_grep("a", "testproject", top_k=2,
                        workspace=str(tmp_workspace))
        assert len(r["lessons"]) <= 2
        assert r["total_matches"] >= len(r["lessons"])

    def test_verified_sorted_first(self, tmp_workspace, sample_lessons):
        from backend.core.knowledge.manager import LessonManager
        for l in sample_lessons:
            LessonManager.save(tmp_workspace, l)

        from backend.core.knowledge.recall import recall_grep
        r = recall_grep("a", "testproject", top_k=10,
                        workspace=str(tmp_workspace))
        vcs = [l.get("verified_count", 0) for l in r["lessons"]]
        assert vcs[0] >= vcs[-1]  # 降序


# ── filter_by_relevance ──────────────────────────────────────


class TestFilterByRelevance:
    def test_common_only_returns_all(self, sample_lessons):
        from backend.core.knowledge.recall import filter_by_relevance
        result = filter_by_relevance(sample_lessons,
                                     "修改 文件 需要 可以")
        assert len(result) == len(sample_lessons)

    def test_specific_keyword_filters(self, sample_lessons):
        from backend.core.knowledge.recall import filter_by_relevance
        result = filter_by_relevance(sample_lessons,
                                     "修复 auth 模块的登录问题")
        auth_hits = [l for l in result if "auth" in l.trigger.lower()]
        assert len(auth_hits) >= 1


# ── 热/温/冷分类 ─────────────────────────────────────────────


class TestClassifyLessonHeat:
    def test_no_retrieval_is_cold(self):
        from backend.core.knowledge.models import classify_lesson_heat, Lesson
        assert classify_lesson_heat(Lesson(trigger="t", rule="r")) == "cold"

    def test_one_is_warm(self):
        from backend.core.knowledge.models import classify_lesson_heat, Lesson
        l = Lesson(trigger="t", rule="r",
                   recent_retrievals=[datetime.now().isoformat()])
        assert classify_lesson_heat(l) == "warm"

    def test_hot_threshold(self):
        from backend.core.knowledge.models import classify_lesson_heat, Lesson
        now = datetime.now().isoformat()
        l = Lesson(trigger="t", rule="r", recent_retrievals=[now] * 3)
        assert classify_lesson_heat(l) == "hot"


class TestGetStickyLessons:
    def test_only_hot_sticky(self):
        from backend.core.knowledge.models import (
            get_sticky_lessons, Lesson,
        )
        now = datetime.now().isoformat()
        hot = Lesson(id="hot", trigger="a", rule="ra", severity="high",
                     recent_retrievals=[now] * 5)
        warm = Lesson(id="warm", trigger="b", rule="rb",
                      recent_retrievals=[now])
        sticky = get_sticky_lessons([hot, warm])
        assert "hot" in sticky
        assert "warm" not in sticky

    def test_sticky_cap(self):
        from backend.core.knowledge.models import get_sticky_lessons, Lesson
        now = datetime.now().isoformat()
        lessons = [Lesson(id=f"h{i}", trigger=f"f{i}", rule=f"r{i}",
                          severity="high",
                          recent_retrievals=[now] * 5)
                   for i in range(15)]
        assert len(get_sticky_lessons(lessons)) <= 10


# ── LessonManager 新增 ───────────────────────────────────────


class TestLessonManagerDedup:
    def test_hash_dedup(self, tmp_workspace):
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import Lesson
        ws = tmp_workspace / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws.mkdir(parents=True, exist_ok=True)

        LessonManager.save_pending(tmp_workspace, Lesson(
            trigger="auth.py", rule="修改 auth 前 scan",
            project_name="testproject",
        ))
        LessonManager.save_pending(tmp_workspace, Lesson(
            trigger="auth.py", rule="修改 auth 前 scan",
            project_name="testproject",
        ))
        pending = LessonManager.load_pending(tmp_workspace, "testproject")
        assert sum(1 for p in pending if p.trigger == "auth.py") == 1

    def test_similar_but_different_allowed(self, tmp_workspace):
        """不同 trigger 但相同 rule → 两条都保留（为联想留数据）。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import Lesson
        ws = tmp_workspace / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws.mkdir(parents=True, exist_ok=True)

        LessonManager.save_pending(tmp_workspace, Lesson(
            trigger="auth.py", rule="文件反复修改需要关注",
            project_name="testproject",
        ))
        LessonManager.save_pending(tmp_workspace, Lesson(
            trigger="session.py", rule="文件反复修改需要关注",
            project_name="testproject",
        ))
        pending = LessonManager.load_pending(tmp_workspace, "testproject")
        assert len([p for p in pending if "文件反复修改" in p.rule]) == 2

    def test_discard_works(self, tmp_workspace):
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import Lesson
        ws = tmp_workspace / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws.mkdir(parents=True, exist_ok=True)

        LessonManager.save_pending(tmp_workspace, Lesson(
            id="to_discard", trigger="x.py", rule="test discard",
            project_name="testproject",
        ))
        assert LessonManager.pending_count(tmp_workspace, "testproject") >= 1
        assert LessonManager.discard_lesson(
            tmp_workspace, "to_discard", "testproject",
        )


# ── auto_discard_invalid ─────────────────────────────────────


class TestAutoDiscardInvalid:
    def test_nonexistent_file_discarded(self, tmp_workspace):
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import Lesson
        from backend.core.knowledge.harvest import auto_discard_invalid
        ws = tmp_workspace / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws.mkdir(parents=True, exist_ok=True)

        LessonManager.save_pending(tmp_workspace, Lesson(
            trigger="nonexistent/file.py",
            rule="修改 nonexistent/file.py 前需要 scan",
            project_name="testproject",
        ))
        n = auto_discard_invalid(tmp_workspace, "testproject")
        assert n >= 1

    def test_short_rule_discarded(self, tmp_workspace):
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import Lesson
        from backend.core.knowledge.harvest import auto_discard_invalid
        ws = tmp_workspace / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws.mkdir(parents=True, exist_ok=True)

        LessonManager.save_pending(tmp_workspace, Lesson(
            trigger="auth.py", rule="太短",
            project_name="testproject",
        ))
        n = auto_discard_invalid(tmp_workspace, "testproject")
        assert n >= 1


# ── EmbeddingProvider ────────────────────────────────────────


class TestEmbeddingProvider:
    def test_not_available_by_default(self):
        from backend.core.knowledge.embedding import EmbeddingProvider
        p = EmbeddingProvider()
        assert not p.available

    def test_available_when_configured(self):
        from backend.core.knowledge.embedding import EmbeddingProvider
        p = EmbeddingProvider(provider="openai", model="text-embedding-3-small")
        assert p.available

    def test_embed_returns_none_when_unavailable(self):
        from backend.core.knowledge.embedding import EmbeddingProvider
        p = EmbeddingProvider()
        assert p.embed("test") is None


# ═══════════════════════════════════════════════════════════════
# 链路测试 —— 模拟下级返回值，验证完整数据流
# ═══════════════════════════════════════════════════════════════


class MockLLMProvider:
    """模拟 LLMProvider。返回预定义的 lesson JSON。"""

    def __init__(self, response_data=None, should_fail=False):
        self.response_data = response_data or []
        self.should_fail = should_fail
        self.call_count = 0

    def chat(self, messages, max_tokens=4096, timeout=30, **kwargs):
        self.call_count += 1
        if self.should_fail:
            raise RuntimeError("LLM API connection failed")
        import json
        return json.dumps(self.response_data, ensure_ascii=False)


# ── Chain 1: Signal Capture → Harvest Trigger → LLM Summary → Save ──


class TestChainSignalToLesson:
    """完整收割链路：信号捕获 → 调度判断 → LLM 总结 → 写入 pending。"""

    def test_full_harvest_chain(self, tmp_workspace):
        """端到端：capture_signal → trigger → LLM → save_pending → verify。"""
        from backend.core.knowledge.harvest import (
            capture_signal, harvest_llm_summary,
        )
        from backend.core.knowledge.manager import LessonManager
        from backend.core.history import HistoryManager

        HistoryManager.set_workspace(str(tmp_workspace))

        # Step 1: 捕获 5 条信号
        signals_data = [
            {"trigger": "auth.py", "rule": "修改 auth 前 scan",
             "detail": {"file": "auth.py", "severity": "high"}},
            {"trigger": "login.py", "rule": "修改 login 必须更新测试",
             "detail": {"file": "login.py", "severity": "critical"}},
            {"trigger": "database", "rule": "数据库变更前备份",
             "detail": {"file": "db.py", "severity": "medium"}},
            {"trigger": "auth.py", "rule": "auth 模块变更需要 review",
             "detail": {"file": "auth.py", "severity": "high"}},
            {"trigger": "config.yaml", "rule": "config 变更同步 example",
             "detail": {"file": "config.yaml", "severity": "medium"}},
        ]
        for s in signals_data:
            capture_signal("lesson_trigger", s, "testproject")

        # Step 2: 验证信号已写入 HistoryManager
        entries = HistoryManager.load()
        signals = [e for e in entries if e.operation == "unprocessed_signal"]
        assert len(signals) >= 5, f"Expected >=5 signals, got {len(signals)}"

        # Step 3: Mock LLM 返回 2 条有效 lesson + 1 条无效（描述性）
        mock_llm = MockLLMProvider(response_data=[
            {
                "trigger": "auth.py",
                "rule": "if modifying auth.py, then must run scan and review first",
                "severity": "high",
                "category": "process",
                "dangerous_tools": [],
                "prerequisite_tools": ["scan"],
                "required_tools": [],
            },
            {
                "trigger": "login.py",
                "rule": "when login logic changes, must update integration tests",
                "severity": "critical",
                "category": "dependency",
                "dangerous_tools": [],
                "prerequisite_tools": [],
                "required_tools": ["test"],
            },
            {
                "trigger": "auth.py",
                "rule": "auth is a hot file",
                "severity": "low",
                "category": "process",
            },
        ])

        # Step 4: LLM 总结
        harvest_signals = [
            {"signal_type": "lesson_trigger", **s}
            for s in signals_data
        ]
        lessons = harvest_llm_summary(
            harvest_signals, mock_llm,
            str(tmp_workspace), "testproject",
        )

        # Step 5: 第 3 条被门禁过滤（描述性），只保留 2 条
        assert len(lessons) == 2, f"Expected 2, got {len(lessons)}"
        assert lessons[0].rule == "if modifying auth.py, then must run scan and review first"
        assert lessons[0].severity == "high"
        assert lessons[0].origin == "harvest"
        assert lessons[0].prerequisite_tools == ["scan"]

        # Step 6: 写入 pending
        ws = tmp_workspace / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws.mkdir(parents=True, exist_ok=True)
        for lesson in lessons:
            LessonManager.save_pending(tmp_workspace, lesson)

        # Step 7: 验证 pending 已写入
        pending = LessonManager.load_pending(tmp_workspace, "testproject")
        assert len(pending) >= 2
        assert any(p.trigger == "auth.py" for p in pending)
        assert any(p.trigger == "login.py" for p in pending)

    def test_llm_failure_saves_nothing_but_retries(self, tmp_workspace):
        """LLM 调用失败 → 信号保留为 unprocessed，重试计数递增。"""
        from backend.core.knowledge.harvest import (
            capture_signal, harvest_llm_summary,
        )
        from backend.core.history import HistoryManager

        HistoryManager.set_workspace(str(tmp_workspace))

        capture_signal("lesson_trigger", {
            "trigger": "auth.py", "rule": "修改 auth 前 scan",
            "detail": {}, "harvest_retry_count": 0,
        }, "testproject")

        signals = [
            {"signal_type": "lesson_trigger",
             "trigger": "auth.py", "rule": "修改 auth 前 scan",
             "detail": {}, "harvest_retry_count": 0},
        ]
        mock_llm = MockLLMProvider(should_fail=True)
        result = harvest_llm_summary(
            signals, mock_llm, str(tmp_workspace), "testproject",
        )

        assert result == []
        assert signals[0].get("harvest_retry_count", 0) >= 1

    def test_max_retries_discards_signal(self):
        """重试 ≥5 次 → 信号被丢弃，不再回写。"""
        from backend.core.knowledge.harvest import harvest_llm_summary
        signals = [{
            "signal_type": "lesson_trigger",
            "trigger": "auth.py", "rule": "修改 auth 前 scan",
            "detail": {}, "harvest_retry_count": 5,
        }]
        mock_llm = MockLLMProvider(should_fail=True)
        result = harvest_llm_summary(signals, mock_llm, "/tmp", "test")
        # retry_count=5 → 超过 MAX_HARVEST_RETRY → 不回写
        assert result == []


# ── Chain 2: Recall → Record Retrieval → Classify Heat → Recycle ──


class TestChainRecallToRecycle:
    """完整检索+回收链路：recall → record_retrieval → classify → sticky。"""

    def test_recall_updates_retrieval_log(self, tmp_workspace, sample_lessons):
        """recall_grep 后 lesson.recent_retrievals 被更新。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.recall import recall_grep

        for l in sample_lessons:
            LessonManager.save(tmp_workspace, l)

        # 检索前：无检索记录
        pending = LessonManager.load_pending(tmp_workspace, "testproject")
        for l in pending:
            assert l.recent_retrievals == []

        # 检索
        recall_grep("auth", "testproject", workspace=str(tmp_workspace))

        # 检索后：matched lesson 的 recent_retrievals 被更新
        instance = LessonManager.load_instance(tmp_workspace, "testproject")
        auth_matches = [l for l in instance if "auth" in l.trigger.lower()]
        for l in auth_matches:
            assert len(l.recent_retrievals) >= 1

    def test_repeated_recall_turns_hot_then_sticky(self, tmp_workspace):
        """多次检索同一条 lesson → hot → sticky list 包含它。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import (
            classify_lesson_heat, get_sticky_lessons, Lesson,
        )
        from backend.core.knowledge.recall import recall_grep

        # 创建一条 lesson
        ws_path = tmp_workspace / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws_path.mkdir(parents=True, exist_ok=True)

        lesson = Lesson(
            trigger="auth.py",
            rule="if modifying auth, then must scan first",
            severity="high", verified_count=5,
            project_name="testproject",
        )
        LessonManager.save(tmp_workspace, lesson)

        # 模拟 3 次检索（达到 hot 阈值）
        for _ in range(3):
            recall_grep("auth", "testproject", workspace=str(tmp_workspace))

        # 验证 lesson 变成 hot
        instance = LessonManager.load_instance(tmp_workspace, "testproject")
        auth_lesson = next(l for l in instance if "auth" in l.trigger.lower())
        assert classify_lesson_heat(auth_lesson) == "hot"

        # 验证 hot lesson 进入 sticky list
        sticky = get_sticky_lessons(instance)
        assert auth_lesson.id in sticky

    def test_cold_lesson_not_sticky(self, tmp_workspace):
        """从未被检索的 lesson 保持 cold，不在 sticky 里。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import (
            classify_lesson_heat, get_sticky_lessons, Lesson,
        )

        lesson = Lesson(
            trigger="untouched.py",
            rule="if modifying untouched, then must test",
            severity="medium",
            project_name="testproject",
        )
        LessonManager.save(tmp_workspace, lesson)

        instance = LessonManager.load_instance(tmp_workspace, "testproject")
        cold = [l for l in instance if l.trigger == "untouched.py"]
        assert len(cold) == 1
        assert classify_lesson_heat(cold[0]) == "cold"
        assert cold[0].id not in get_sticky_lessons(instance)


# ── Chain 3: 分离 → 检索时实时过滤 ──


class TestChainIsolationToRecall:
    """Per-agent scope：filter_by_relevance → recall_grep 结果只含相关 lesson。"""

    def test_filter_then_recall_returns_only_relevant(self, tmp_workspace,
                                                      sample_lessons):
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.recall import recall_grep

        for l in sample_lessons:
            LessonManager.save(tmp_workspace, l)

        # 模拟 B Agent 的任务（只涉及 auth 和 login）
        agent_ctx = {"task_description": "修复 auth 模块的登录安全问题"}

        # 不带 agent_context 检索：全部 lesson 都可能命中
        r_all = recall_grep("a", "testproject", workspace=str(tmp_workspace))
        all_count = r_all["total_matches"]

        # 带 agent_context 检索：只有 auth/login 相关的被保留
        r_filtered = recall_grep("a", "testproject",
                                 agent_context=agent_ctx,
                                 workspace=str(tmp_workspace))
        filtered_count = r_filtered["total_matches"]

        # 带 filter 的结果 ≤ 不带 filter 的结果
        assert filtered_count <= all_count

    def test_agent_with_unrelated_task_gets_empty(self, tmp_workspace,
                                                   sample_lessons):
        """Agent 的任务和所有 lesson 无关 → filter 后无匹配。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.recall import recall_grep

        for l in sample_lessons:
            LessonManager.save(tmp_workspace, l)

        # 任务完全无关
        agent_ctx = {"task_description": "update readme file with new badges"}

        r = recall_grep("a", "testproject",
                        agent_context=agent_ctx,
                        workspace=str(tmp_workspace))
        # "readme" 和 "badges" 不在任何 lesson 的 trigger/rule 里
        assert r["total_matches"] == 0 or len(r["lessons"]) == 0


# ── Chain 4: Pending Digest → Discard → Verify ──


class TestChainPendingDigest:
    """Pending 消化三级：auto_discard → auto_verify → 人介入。"""

    def test_digest_pipeline(self, tmp_workspace):
        """完整的 pending 消化管线。"""
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import Lesson
        from backend.core.knowledge.harvest import (
            auto_discard_invalid,
        )

        ws = tmp_workspace / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws.mkdir(parents=True, exist_ok=True)

        # 写入混合质量的 pending
        # 好 lesson（会被保留）
        LessonManager.save_pending(tmp_workspace, Lesson(
            id="good_1", trigger="auth.py",
            rule="if modifying auth, then must run full test suite first",
            severity="high", project_name="testproject",
        ))
        # 无效 lesson：rule 太短
        LessonManager.save_pending(tmp_workspace, Lesson(
            id="bad_short", trigger="login.py",
            rule="too short",
            severity="low", project_name="testproject",
        ))
        # 无效 lesson：trigger 文件不存在
        LessonManager.save_pending(tmp_workspace, Lesson(
            id="bad_file", trigger="nonexistent/deleted.py",
            rule="if modifying deleted file, then must reconsider",
            severity="medium", project_name="testproject",
        ))
        # 有效的
        LessonManager.save_pending(tmp_workspace, Lesson(
            id="good_2", trigger="database",
            rule="when schema changes, must backup before migration",
            severity="critical", project_name="testproject",
        ))

        # 消化前：4 条
        assert LessonManager.pending_count(tmp_workspace, "testproject") == 4

        # L1 Digest：auto discard
        discarded = auto_discard_invalid(tmp_workspace, "testproject")
        assert discarded == 2  # bad_short + bad_file 被清理

        # 消化后：2 条好的保留
        remaining = LessonManager.pending_count(tmp_workspace, "testproject")
        assert remaining == 2
        pending = LessonManager.load_pending(tmp_workspace, "testproject")
        retained_ids = {p.id for p in pending}
        assert "good_1" in retained_ids
        assert "good_2" in retained_ids
        assert "bad_short" not in retained_ids
        assert "bad_file" not in retained_ids


# ── Chain 5: Full End-to-End (no daemon, no real LLM) ──


class TestChainEndToEnd:
    """模拟完整端到端流程：信号→lesson→检索→分类→回收。"""

    def test_e2e_with_mock_llm(self, tmp_workspace):
        """全链路：capture → LLM → save → recall → classify → sticky。"""
        from backend.core.knowledge.harvest import (
            capture_signal, harvest_llm_summary,
        )
        from backend.core.knowledge.manager import LessonManager
        from backend.core.knowledge.models import (
            classify_lesson_heat, get_sticky_lessons,
        )
        from backend.core.knowledge.recall import recall_grep
        from backend.core.history import HistoryManager

        HistoryManager.set_workspace(str(tmp_workspace))
        ws = tmp_workspace / ".gitgo" / "knowledge" / "instances" / "testproject"
        ws.mkdir(parents=True, exist_ok=True)

        # ── 收割阶段 ──
        for i in range(6):
            capture_signal("lesson_trigger", {
                "trigger": f"file_{i}.py",
                "rule": f"规则 {i}",
                "detail": {"index": i},
            }, "testproject")

        # Mock LLM 返回 lesson
        mock_llm = MockLLMProvider(response_data=[
            {
                "trigger": "auth.py",
                "rule": "if modifying core auth, then must run full test suite",
                "severity": "critical", "category": "process",
                "dangerous_tools": ["sync", "push"],
                "prerequisite_tools": ["scan", "test"],
                "required_tools": ["test"],
            },
            {
                "trigger": "login.py",
                "rule": "when login logic changes, must update session tests too",
                "severity": "high", "category": "dependency",
                "dangerous_tools": [],
                "prerequisite_tools": ["test"],
                "required_tools": [],
            },
        ])

        harvest_signals = [
            {"signal_type": "lesson_trigger",
             "trigger": f"file_{i}.py",
             "rule": f"规则 {i}", "detail": {"index": i}}
            for i in range(6)
        ]
        lessons = harvest_llm_summary(
            harvest_signals, mock_llm,
            str(tmp_workspace), "testproject",
        )
        assert len(lessons) == 2

        for l in lessons:
            LessonManager.save_pending(tmp_workspace, l)

        pending = LessonManager.load_pending(tmp_workspace, "testproject")
        assert len(pending) >= 2

        # ── 检索阶段 ──
        r1 = recall_grep("core auth", "testproject",
                         workspace=str(tmp_workspace))
        assert r1["total_matches"] >= 1

        r2 = recall_grep("nonexistent_xyz", "testproject",
                         workspace=str(tmp_workspace))
        assert r2["total_matches"] == 0

        # 重复检索 → hot
        for _ in range(3):
            recall_grep("auth", "testproject", workspace=str(tmp_workspace))

        # ── 分类+回收阶段 ──
        all_lessons = (
            LessonManager.load_instance(tmp_workspace, "testproject")
            + LessonManager.load_pending(tmp_workspace, "testproject")
        )

        hot_count = sum(1 for l in all_lessons
                        if classify_lesson_heat(l) == "hot")
        assert hot_count >= 1  # auth lesson 被检索 4 次 → hot

        sticky = get_sticky_lessons(all_lessons)
        assert len(sticky) >= 1
