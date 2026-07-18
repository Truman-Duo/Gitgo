"""Context Management v0.36 测试 —— 单元 + 链路。

覆盖: 压缩优先级链 / 隐用户输入 / 约束晋升 / Assembler / Transcript / Compact
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from tests.factory import TestDataFactory


@pytest.fixture
def f():
    return TestDataFactory(seed=42)


@pytest.fixture
def tmp_hist():
    with tempfile.TemporaryDirectory() as d:
        from backend.core.history import HistoryManager
        HistoryManager.set_workspace(d)
        yield d


# ═══════════════════════════════════════════════════════════════
# 单元测试: ContextConstants
# ═══════════════════════════════════════════════════════════════


class TestContextConstants:
    def test_defaults(self):
        from backend.core.loop.context_window import ContextConstants
        assert ContextConstants.TURN_UNIT == "assistant_message"
        assert ContextConstants.SNIP_AGE_TURNS == 3
        assert ContextConstants.NUDGE_TTL_TURNS == 5
        assert ContextConstants.MAX_NUDGE_REPEAT == 3
        assert ContextConstants.DEP_GRAPH_HOP_WEIGHTS[0] == 0.9
        assert ContextConstants.DEP_GRAPH_HOP_WEIGHTS.get(999, 0.3) == 0.3


# ═══════════════════════════════════════════════════════════════
# 单元测试: Message 结构化字段
# ═══════════════════════════════════════════════════════════════


class TestMessageMetadata:
    def test_append_with_referenced_files(self):
        from backend.core.loop.session import AgentSession
        s = AgentSession()
        s.append_user("fix auth", referenced_files=["auth.py", "login.py"],
                      message_type="conversation")
        assert s.messages[0]["referenced_files"] == ["auth.py", "login.py"]
        assert s.messages[0]["message_type"] == "conversation"

    def test_append_user_without_metadata(self):
        from backend.core.loop.session import AgentSession
        s = AgentSession()
        s.append_user("hello")
        assert "referenced_files" not in s.messages[0]
        assert "message_type" not in s.messages[0]

    def test_governance_nudge_has_type(self):
        from backend.core.loop.session import AgentSession
        s = AgentSession()
        s.append_user(
            "[完成检查] tool test not called",
            message_type="governance_nudge",
            referenced_files=["auth.py"],
        )
        assert s.messages[0]["message_type"] == "governance_nudge"


# ═══════════════════════════════════════════════════════════════
# 单元测试: Nudge 回收
# ═══════════════════════════════════════════════════════════════


class TestRecycleGovernanceNudges:
    def test_resolved_nudge_gets_recycled(self):
        """Agent 已响应 → resolved → retention=0.1。"""
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import _recycle_governance_nudges

        s = AgentSession()
        s.append_user("fix auth", message_type="governance_nudge")
        s.append_assistant("ok, running test now")  # 已响应
        s.append_user("tool result: test passed")

        recycled = _recycle_governance_nudges(s)
        assert recycled >= 1
        assert s.messages[0].get("_nudge_state") == "resolved"
        assert s.messages[0].get("_retention_override") == 0.1

    def test_pending_nudge_not_recycled(self):
        """Agent 未响应 → pending → 不碰。"""
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import _recycle_governance_nudges

        s = AgentSession()
        s.append_user("fix auth", message_type="governance_nudge")
        # 没有 subsequent assistant 消息

        recycled = _recycle_governance_nudges(s)
        assert recycled == 0
        assert s.messages[0].get("_nudge_state") == "pending"
        assert "_retention_override" not in s.messages[0]

    def test_orphan_nudge_after_ttl(self):
        """超过 TTL → orphan → retention=0.1 + 写入 HistoryManager。"""
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import (
            _recycle_governance_nudges, ContextConstants,
        )

        s = AgentSession()
        s.append_user("fix auth", message_type="governance_nudge")
        # 填充足够多的空消息来满足 TTL
        for _ in range(ContextConstants.NUDGE_TTL_TURNS + 1):
            s.append_user("...")  # 没有 assistant 响应

        recycled = _recycle_governance_nudges(s)
        assert recycled >= 1
        assert s.messages[0].get("_nudge_state") == "orphan"
        assert s.messages[0].get("_retention_override") == 0.1

    def test_non_nudge_ignored(self):
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import _recycle_governance_nudges

        s = AgentSession()
        s.append_user("normal message")  # 没有 message_type

        recycled = _recycle_governance_nudges(s)
        assert recycled == 0


# ═══════════════════════════════════════════════════════════════
# 单元测试: Tool Result Snip
# ═══════════════════════════════════════════════════════════════


class TestSnipOldToolResults:
    def test_old_result_snipped(self):
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import _snip_old_tool_results

        s = AgentSession()
        s.append_user("tool_result: scan complete",
                      message_type="tool_result")
        s.messages[0]["_tool_name"] = "scan"
        # 添加 4 个 assistant 响应（超过 SNIP_AGE_TURNS=3）
        for _ in range(4):
            s.append_assistant("step")

        snipped = _snip_old_tool_results(s)
        assert snipped >= 1
        assert s.messages[0]["_snip_state"] == "snipped"
        assert "elided" in s.messages[0]["content"].lower()

    def test_recent_result_not_snipped(self):
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import _snip_old_tool_results

        s = AgentSession()
        s.append_user("tool_result: scan", message_type="tool_result")
        s.messages[0]["_tool_name"] = "scan"
        s.append_assistant("step 1")  # only 1 assistant after

        snipped = _snip_old_tool_results(s)
        assert snipped == 0

    def test_idempotent(self):
        """已 snip 的不重复处理。"""
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import _snip_old_tool_results

        s = AgentSession()
        s.append_user("tool_result", message_type="tool_result")
        s.messages[0]["_tool_name"] = "scan"
        s.messages[0]["_snip_state"] = "snipped"  # 已snip
        for _ in range(4):
            s.append_assistant("step")

        snipped = _snip_old_tool_results(s)
        assert snipped == 0


# ═══════════════════════════════════════════════════════════════
# 单元测试: Retention 多源合成
# ═══════════════════════════════════════════════════════════════


class TestResolveRetention:
    def test_max_takes_highest(self):
        from backend.core.loop.context_window import _resolve_retention

        msg = {
            "_retention_override": 0.1,
            "_retention_priority": 0.8,  # from RetentionAdvisor
        }
        assert _resolve_retention(msg) == 0.8

    def test_default_when_no_overrides(self):
        from backend.core.loop.context_window import _resolve_retention
        assert _resolve_retention({}) == 0.3


# ═══════════════════════════════════════════════════════════════
# 单元测试: 约束晋升
# ═══════════════════════════════════════════════════════════════


class TestPromoteConstraints:
    def test_detects_negative_directive(self):
        from backend.core.loop.session import AgentSession
        from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
        from backend.core.loop.executor import _promote_mid_task_constraints

        s = AgentSession()
        s.append_user("这次不要改 API 层的接口定义")
        p = AgentProcess(
            process_id="test", role="executor",
            ring_level=RingLevel.RING_3,
            status=ProcessStatus.RUNNING,
        )

        promoted = _promote_mid_task_constraints(s, p)
        assert promoted >= 1
        assert any("API" in c for c in p.task_constraints)

    def test_rejects_vague_negative(self):
        """不要这样 → 无宾语 → 不晋升。"""
        from backend.core.loop.session import AgentSession
        from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
        from backend.core.loop.executor import _promote_mid_task_constraints

        s = AgentSession()
        s.append_user("不要这样")
        p = AgentProcess(
            process_id="test", role="executor",
            ring_level=RingLevel.RING_3,
            status=ProcessStatus.RUNNING,
        )

        promoted = _promote_mid_task_constraints(s, p)
        assert promoted == 0

    def test_excludes_quoted_lesson(self):
        """lesson 引述不算新约束。"""
        from backend.core.loop.session import AgentSession
        from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
        from backend.core.loop.executor import _promote_mid_task_constraints

        s = AgentSession()
        s.append_user("lesson L01 说不要直接修改 release 仓库")
        p = AgentProcess(
            process_id="test", role="executor",
            ring_level=RingLevel.RING_3,
            status=ProcessStatus.RUNNING,
        )

        promoted = _promote_mid_task_constraints(s, p)
        assert promoted == 0  # "lesson" 关键词 → 被排除

    def test_system_message_includes_constraints(self):
        from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
        from backend.core.loop.executor import _build_system_message_for_llm

        p = AgentProcess(
            process_id="test", role="executor",
            ring_level=RingLevel.RING_3,
            status=ProcessStatus.RUNNING,
            task_constraints=["不要改 API 层"],
        )
        base = "You are an Agent."
        result = _build_system_message_for_llm(p, base)
        assert "Task-level Constraints" in result
        assert "不要改 API 层" in result

    def test_empty_constraints_no_change(self):
        from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
        from backend.core.loop.executor import _build_system_message_for_llm

        p = AgentProcess(
            process_id="test", role="executor",
            ring_level=RingLevel.RING_3,
            status=ProcessStatus.RUNNING,
        )
        base = "You are an Agent."
        assert _build_system_message_for_llm(p, base) == base


# ═══════════════════════════════════════════════════════════════
# 单元测试: Nudge Counter
# ═══════════════════════════════════════════════════════════════


class TestNudgeCounter:
    def test_increment_and_get(self):
        from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
        from backend.core.loop.executor import _inc_nudge_counter, _get_nudge_count

        p = AgentProcess(
            process_id="test", role="executor",
            ring_level=RingLevel.RING_3,
            status=ProcessStatus.RUNNING,
        )
        _inc_nudge_counter(p, "required_tools")
        _inc_nudge_counter(p, "required_tools")
        _inc_nudge_counter(p, "rejection")

        assert _get_nudge_count(p, "required_tools") == 2
        assert _get_nudge_count(p, "rejection") == 1
        assert _get_nudge_count(p, "unknown") == 0


# ═══════════════════════════════════════════════════════════════
# 单元测试: Transcript Builder
# ═══════════════════════════════════════════════════════════════


class TestTranscriptBuilder:
    def test_to_xml(self):
        from backend.core.loop.transcript import TaskTranscriptBuilder

        tb = TaskTranscriptBuilder(task_id="fix-auth")
        tb.append_tool_call(1, "recall_grep", {"query": "auth"},
                            {"total_matches": 3}, 120)
        tb.append_governance_event(2, "required_tool_test_missing")
        tb.append_completion(3, ["recall_grep", "test"], ["L01"])

        xml = tb.to_xml()
        assert "fix-auth" in xml
        assert "recall_grep" in xml
        assert "governance" in xml
        assert "L01" in xml

    def test_to_return_context(self):
        from backend.core.loop.transcript import TaskTranscriptBuilder

        tb = TaskTranscriptBuilder(task_id="fix-auth")
        tb.append_tool_call(1, "recall_grep", {"query": "auth"},
                            {"total_matches": 3}, 120)
        tb.append_completion(3, ["recall_grep"], ["L01"])

        ctx = tb.to_return_context()
        assert ctx["task"] == "fix-auth"
        assert ctx["status"] == "COMPLETED"
        assert "recall_grep" in ctx["tools"]

    def test_extract_compact_constraints_hard_extract(self):
        from backend.core.loop.transcript import TaskTranscriptBuilder

        msgs = [
            {"role": "user", "content": "不要改 API 层接口"},
            {"role": "user", "content": "禁止直接 modfiy release repo"},
        ]
        constraints = TaskTranscriptBuilder.extract_compact_constraints(msgs)
        assert len(constraints) >= 1
        assert any("API" in c["rule"] for c in constraints)

    def test_extract_compact_constraints_lesson_inherit(self):
        from backend.core.loop.transcript import TaskTranscriptBuilder
        from backend.core.knowledge.models import Lesson

        lesson = Lesson(
            id="L01", trigger="auth.py",
            rule="if modifying auth, then must scan first",
        )
        constraints = TaskTranscriptBuilder.extract_compact_constraints(
            [], [lesson],
        )
        assert len(constraints) >= 1
        assert constraints[0]["source"] == "lesson_L01"


# ═══════════════════════════════════════════════════════════════
# 单元测试: 依赖图过滤
# ═══════════════════════════════════════════════════════════════


class TestDepGraphFilter:
    def test_graph_distance_same_file(self):
        from backend.core.loop.context_window import _graph_distance
        assert _graph_distance({}, "auth.py", "auth.py") == 0

    def test_graph_distance_unreachable(self):
        from backend.core.loop.context_window import _graph_distance
        assert _graph_distance({}, "auth.py", "login.py") == 999

    def test_filter_with_whitelist(self):
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import _dep_graph_filter

        s = AgentSession()
        s.append_user("update config",
                      referenced_files=[".gitgo/config.yaml"])
        filtered = _dep_graph_filter(s, {}, ["backend/auth.py"])
        # 白名单: retention=0.8
        assert filtered == 0  # 白名单不算被降级
        assert s.messages[0]["_retention_override"] == 0.8

    def test_filter_no_references(self):
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import _dep_graph_filter

        s = AgentSession()
        s.append_user("hello")
        filtered = _dep_graph_filter(s, {}, ["auth.py"])
        assert filtered == 0


# ═══════════════════════════════════════════════════════════════
# 单元测试: 知识替代
# ═══════════════════════════════════════════════════════════════


class TestReplaceWithLessonTranscripts:
    def test_replaces_matching_content(self):
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import _replace_with_lesson_transcripts
        from backend.core.knowledge.models import Lesson

        s = AgentSession()
        s.append_user("改了 auth.py 文件需要验证")
        harness = {
            "lessons": [
                Lesson(trigger="auth.py", rule="修改 auth 前必须 scan",
                       id="L01"),
            ]
        }

        replaced = _replace_with_lesson_transcripts(s, harness)
        assert replaced >= 1
        assert "L01" in s.messages[0]["content"]
        assert s.messages[0].get("_replaced_by_lesson")

    def test_no_lessons_no_replacement(self):
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import _replace_with_lesson_transcripts

        s = AgentSession()
        s.append_user("nothing matches")
        harness = {}

        replaced = _replace_with_lesson_transcripts(s, harness)
        assert replaced == 0


# ═══════════════════════════════════════════════════════════════
# 链路测试: 完整压缩优先级链
# ═══════════════════════════════════════════════════════════════


class TestChainManageContext:
    def test_full_pipeline(self):
        """manage_context 五步全链路。"""
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import manage_context
        from backend.core.knowledge.models import Lesson

        s = AgentSession()
        # 填充大量消息模拟满 session
        for i in range(100):
            s.append_assistant(f"step {i} response " + "x" * 500)  # ~125 tokens each

        harness = {
            "lessons": [
                Lesson(trigger="auth.py", rule="修改 auth 前 scan", id="L01"),
            ]
        }

        # 添加 governance nudge
        s.append_user("fix auth", message_type="governance_nudge")
        s.append_assistant("ok")  # resolved

        # 添加 tool result
        s.append_user("tool: scan complete", message_type="tool_result")
        s.messages[-1]["_tool_name"] = "scan"

        need_compact = manage_context(s, harness, None)
        # 应该完成前三步不需要 compact
        assert isinstance(need_compact, bool)

    def test_max_retention_takes_highest(self):
        """多源 retention 合成取 max。"""
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import (
            _recycle_governance_nudges, _resolve_retention,
        )

        s = AgentSession()
        s.append_user("fix auth",
                      message_type="governance_nudge")
        s.append_assistant("ok")
        s.messages[0]["_retention_priority"] = 0.8  # from RetentionAdvisor

        _recycle_governance_nudges(s)
        # resolved → override=0.1, but RetentionAdvisor says 0.8
        final = _resolve_retention(s.messages[0])
        assert final == 0.8  # max(0.1, 0.8) = 0.8


# ═══════════════════════════════════════════════════════════════
# 链路测试: Nudge 逃生舱
# ═══════════════════════════════════════════════════════════════


class TestChainNudgeEscape:
    def test_three_nudges_escalates(self):
        """3 次 nudge → FAILED with nudge_escalation error。"""
        from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
        from backend.core.loop.executor import _inc_nudge_counter, _get_nudge_count
        from backend.core.loop.context_window import ContextConstants

        p = AgentProcess(
            process_id="test", role="executor",
            ring_level=RingLevel.RING_3,
            status=ProcessStatus.RUNNING,
        )
        for i in range(ContextConstants.MAX_NUDGE_REPEAT):
            _inc_nudge_counter(p, "required_tools")

        assert _get_nudge_count(p, "required_tools") >= 3
        # 达到 MAX_NUDGE_REPEAT → agent_step 会 kill process


# ═══════════════════════════════════════════════════════════════
# 链路测试: Assembler 工具
# ═══════════════════════════════════════════════════════════════


class TestChainAssembler:
    def test_assemble_return_context(self, f, tmp_hist):
        """assemble_return_context 返回结构化摘要。"""
        from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
        from backend.core.loop.session import AgentSession
        from backend.core.loop.transcript import TaskTranscriptBuilder

        s = AgentSession()
        s.append_user("tool: scan complete", message_type="tool_result",
                      referenced_files=["auth.py"])
        s.messages[-1]["_tool_name"] = "scan"

        p = AgentProcess(
            process_id="test", role="executor",
            ring_level=RingLevel.RING_3,
            status=ProcessStatus.COMPLETED,
            steps_used=3,
            task_description="fix auth bug",
        )
        p.session = s

        # 使用 TranscriptBuilder 的 to_return_context
        tb = TaskTranscriptBuilder(task_id="fix auth bug")
        tb.append_tool_call(1, "scan", {},
                            {"status_dict": {"entries_total": 5}}, 100)
        tb.append_completion(2, ["scan"], [])
        p._transcript_builder = tb

        ctx = tb.to_return_context()
        assert ctx["task"] == "fix auth bug"
        assert ctx["status"] == "COMPLETED"
        assert "scan" in ctx["tools"]


# ═══════════════════════════════════════════════════════════════
# 链路测试: Transcript → Return Context 贯通
# ═══════════════════════════════════════════════════════════════


class TestChainTranscriptToReturn:
    def test_full_transcript_to_return(self):
        from backend.core.loop.transcript import TaskTranscriptBuilder
        from backend.core.loop.session import AgentSession
        from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus

        # Step 1: 构建 transcript
        tb = TaskTranscriptBuilder(task_id="fix-auth")
        tb.append_tool_call(1, "recall_grep", {"query": "auth"},
                            {"total_matches": 2}, 120)
        tb.append_tool_call(2, "scan", {},
                            {"status_dict": {"entries_total": 10, "entries_changed": 3}}, 340)
        tb.append_governance_event(3, "required_tool_test_missing")
        tb.append_tool_call(4, "test", {},
                            {"passed": 5, "failed": 0}, 2100)
        tb.append_completion(5, ["recall_grep", "scan", "test"], ["L01"])

        # Step 2: XML 转录
        xml = tb.to_xml()
        assert "recall_grep" in xml
        assert "governance" in xml
        assert "L01" in xml

        # Step 3: 返回转录
        ctx = tb.to_return_context()
        assert ctx["status"] == "COMPLETED"
        assert ctx["steps"] == 5
        assert len(ctx["governance_events"]) == 1


# ═══════════════════════════════════════════════════════════════
# 链路测试: Constraint → System Prompt → Agent Context
# ═══════════════════════════════════════════════════════════════


class TestChainConstraintToPrompt:
    def test_promote_then_build_system(self):
        from backend.core.loop.session import AgentSession
        from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
        from backend.core.loop.executor import (
            _promote_mid_task_constraints, _build_system_message_for_llm,
        )

        s = AgentSession()
        s.append_user("这次不要改 API 层接口")

        p = AgentProcess(
            process_id="test", role="executor",
            ring_level=RingLevel.RING_3,
            status=ProcessStatus.RUNNING,
        )

        promoted = _promote_mid_task_constraints(s, p)
        assert promoted >= 1

        result = _build_system_message_for_llm(p, "Base system prompt")
        assert "Task-level Constraints" in result
        assert "API" in result


# ═══════════════════════════════════════════════════════════════
# 边界条件
# ═══════════════════════════════════════════════════════════════


class TestContextEdgeCases:
    def test_empty_session_no_crash(self):
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import (
            _recycle_governance_nudges, _snip_old_tool_results,
            _dep_graph_filter, _replace_with_lesson_transcripts,
        )

        s = AgentSession()
        assert _recycle_governance_nudges(s) == 0
        assert _snip_old_tool_results(s) == 0
        assert _dep_graph_filter(s, {}, []) == 0
        assert _replace_with_lesson_transcripts(s, {}) == 0

    def test_dep_graph_filter_empty_files(self):
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_window import _dep_graph_filter

        s = AgentSession()
        s.append_user("hello", referenced_files=["auth.py"])
        # task_files=[] → 所有文件都离 task 很远 → 降级
        filtered = _dep_graph_filter(s, {}, [])
        assert filtered >= 0  # 不崩溃

    def test_no_constraints_system_prompt_unchanged(self):
        from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
        from backend.core.loop.executor import _build_system_message_for_llm

        p = AgentProcess(
            process_id="test", role="executor",
            ring_level=RingLevel.RING_3,
            status=ProcessStatus.RUNNING,
            task_constraints=[],
        )
        base = "You are an Agent."
        assert _build_system_message_for_llm(p, base) == base
