"""Integration tests for error recovery infrastructure (P0-P2).

Covers: error taxonomy, transaction rollback, storm break,
session persistence, ProcessToolRunner, LLM retry classification.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.core.loop.error_taxonomy import (
    ErrorSource, ErrorSeverity, Retryability, ErrorNature,
    ClassifiedError,
    classify_http_error, classify_network_error, classify_timeout_error,
    classify_context_overflow, classify_tool_error, classify_business_failure,
)
from backend.core.loop.loop_guard import LoopGuard
from backend.core.loop.agent_tool import AgentTool, ToolEffect


# ═══════════════════════════════════════════════════════════════
# Error Taxonomy
# ═══════════════════════════════════════════════════════════════

class TestErrorTaxonomy:
    """P0.1: 四维错误分类体系。"""

    def test_http_5xx_is_retryable(self):
        err = classify_http_error(502, "Bad Gateway")
        assert err.source == ErrorSource.LLM
        assert err.retryability == Retryability.RETRYABLE
        assert err.is_retryable is True

    def test_http_429_is_limited(self):
        err = classify_http_error(429, "Too Many Requests")
        assert err.code == "RATE_LIMITED"
        assert err.retryability == Retryability.LIMITED
        assert err.is_retryable is True

    def test_http_401_is_non_retryable(self):
        err = classify_http_error(401, "Unauthorized")
        assert err.retryability == Retryability.NON_RETRYABLE
        assert err.is_retryable is False
        assert err.severity == ErrorSeverity.FATAL

    def test_http_403_is_non_retryable(self):
        err = classify_http_error(403, "Forbidden")
        assert err.retryability == Retryability.NON_RETRYABLE

    def test_network_error_is_retryable(self):
        err = classify_network_error("Connection refused")
        assert err.retryability == Retryability.RETRYABLE
        assert err.code == "NETWORK_ERROR"

    def test_timeout_error_is_retryable(self):
        err = classify_timeout_error("Request timed out")
        assert err.retryability == Retryability.RETRYABLE
        assert err.code == "TIMEOUT"

    def test_context_overflow_is_limited(self):
        err = classify_context_overflow("Context window exceeded")
        assert err.code == "CONTEXT_OVERFLOW"
        assert err.retryability == Retryability.LIMITED

    def test_tool_error_is_crash_by_default(self):
        err = classify_tool_error(RuntimeError("boom"), tool_name="test")
        assert err.nature == ErrorNature.CRASH
        assert err.is_crash is True
        assert err.is_business is False
        assert err.source == ErrorSource.TOOL

    def test_tool_timeout_is_crash(self):
        err = classify_tool_error(TimeoutError(), tool_name="slow", timeout=True)
        assert err.code == "TOOL_TIMEOUT"
        assert err.nature == ErrorNature.CRASH

    def test_business_failure_is_not_crash(self):
        err = classify_business_failure("TEST_FAIL", "3 tests failed")
        assert err.nature == ErrorNature.BUSINESS
        assert err.is_business is True
        assert err.is_crash is False
        assert err.retryability == Retryability.NON_RETRYABLE

    def test_format_for_llm(self):
        err = classify_tool_error(RuntimeError("division by zero"), tool_name="calc")
        label = err.format_for_llm()
        assert "[TOOL/CRASH/TOOL_CRASH]" in label

    def test_to_dict(self):
        err = classify_network_error("DNS failure")
        d = err.to_dict()
        assert d["source"] == "llm"
        assert d["code"] == "NETWORK_ERROR"
        assert d["nature"] == "crash"


# ═══════════════════════════════════════════════════════════════
# Transaction Rollback (P0.2)
# ═══════════════════════════════════════════════════════════════

class TestTransactionRollback:
    """P0.2: 事务回滚 —— CRASH vs BUSINESS 区分，快照恢复。"""

    def test_is_crash_error_detects_crash(self):
        from backend.core.loop.tool_execution import ToolExecution
        from backend.core.loop.tool_pipeline import ToolResult

        # Simulate a ToolExecution with minimal fields
        exc = ToolExecution(
            execution_id="test-1",
            ctx=MagicMock(),
            tool_calls=[],
        )
        r = ToolResult(is_error=True, diagnostics={"nature": "crash"})
        assert exc._is_crash_error(r) is True

    def test_is_crash_error_passes_business(self):
        from backend.core.loop.tool_execution import ToolExecution
        from backend.core.loop.tool_pipeline import ToolResult

        exc = ToolExecution(
            execution_id="test-2",
            ctx=MagicMock(),
            tool_calls=[],
        )
        r = ToolResult(is_error=True, diagnostics={"nature": "business"})
        assert exc._is_crash_error(r) is False

    def test_is_crash_error_no_error_returns_false(self):
        from backend.core.loop.tool_execution import ToolExecution
        from backend.core.loop.tool_pipeline import ToolResult

        exc = ToolExecution(
            execution_id="test-3",
            ctx=MagicMock(),
            tool_calls=[],
        )
        r = ToolResult(is_error=False, diagnostics={})
        assert exc._is_crash_error(r) is False

    def test_schema_error_is_model_correctable_and_does_not_rollback(self, tmp_path_factory):
        from backend.core.loop.tool_execution import ToolExecution

        called = False

        def execute(_args):
            nonlocal called
            called = True
            return {}

        tool = AgentTool(
            name="write_file", description="write",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            execute=execute, read_only=False, effect=ToolEffect.WORKSPACE_WRITE,
        )
        ctx = MagicMock()
        ctx.workspace_path = str(tmp_path_factory)
        ctx.session = None
        ctx.is_cancelled.return_value = False
        ctx.process.cancel_requested = False
        ctx.artifacts = {"tool_catalog": {tool.name: tool}}
        execution = ToolExecution(
            execution_id="schema-error", ctx=ctx,
            tool_calls=[{"name": tool.name, "args": {}}],
        )
        execution.begin()
        results = execution.execute_batch({tool.name: tool})

        assert called is False
        assert execution._rolled_back is False
        assert results[0].diagnostics == {
            "nature": "business",
            "code": "INVALID_TOOL_ARGUMENTS",
            "source": "llm",
        }

    def test_write_detection_uses_effect_contract(self):
        from backend.core.loop.tool_execution import ToolExecution

        write_tool = AgentTool(
            name="custom_mutation", description="write", parameters={"type": "object"},
            execute=lambda args: {}, read_only=False, effect=ToolEffect.WORKSPACE_WRITE,
        )
        read_tool = AgentTool(
            name="misleading_write_name", description="read", parameters={"type": "object"},
            execute=lambda args: {}, effect=ToolEffect.READ,
        )
        ctx = MagicMock()
        ctx.artifacts = {"tool_catalog": {
            write_tool.name: write_tool, read_tool.name: read_tool,
        }}
        exc = ToolExecution(
            execution_id="test-4",
            ctx=ctx,
            tool_calls=[
                {"name": write_tool.name, "args": {"path": "real.txt"}},
                {"name": read_tool.name, "args": {"path": "ignored.txt"}},
            ],
        )
        assert exc._extract_write_targets() == ["real.txt"]

    def test_external_reads_do_not_turn_audit_resources_into_mutexes(self):
        from backend.core.loop.tool_execution import _resolve_tool_resources

        search = AgentTool(
            name="web_search", description="search", parameters={"type": "object"},
            execute=lambda args: {}, read_only=True,
            effect=ToolEffect.EXTERNAL_READ,
            resources=["network:public-search"],
        )
        assert _resolve_tool_resources(search, {"query": "one"}) == set()

    def test_extract_write_targets_finds_paths(self):
        from backend.core.loop.tool_execution import ToolExecution

        write_tool = AgentTool(
            name="write", description="write", parameters={"type": "object"},
            execute=lambda args: {}, read_only=False, effect=ToolEffect.WORKSPACE_WRITE,
        )
        edit_tool = AgentTool(
            name="edit", description="edit", parameters={"type": "object"},
            execute=lambda args: {}, read_only=False, effect=ToolEffect.WORKSPACE_WRITE,
        )
        scan_tool = AgentTool(
            name="scan", description="scan", parameters={"type": "object"},
            execute=lambda args: {}, effect=ToolEffect.READ,
        )
        ctx = MagicMock()
        ctx.artifacts = {"tool_catalog": {
            "write": write_tool, "edit": edit_tool, "scan": scan_tool,
        }}
        exc = ToolExecution(
            execution_id="test-5",
            ctx=ctx,
            tool_calls=[
                {"name": "write", "args": {"file": "a.txt"}},
                {"name": "edit", "args": {"path": "b.py"}},
                {"name": "scan", "args": {"files": ["c.md"]}},  # read tool, skip
            ],
        )
        targets = exc._extract_write_targets()
        assert "a.txt" in targets
        assert "b.py" in targets
        assert "c.md" not in targets  # scan is read-only

    def test_snapshot_take_and_restore(self, tmp_path_factory):
        from backend.core.loop.tool_execution import ToolExecution

        # Create a test file
        test_file = tmp_path_factory / "test.txt"
        test_file.write_text("original content")

        ctx = MagicMock()
        ctx.workspace_path = str(tmp_path_factory)
        ctx.session = None
        ctx.artifacts = {"tool_catalog": {
            "write": AgentTool(
                name="write", description="write", parameters={"type": "object"},
                execute=lambda args: {}, read_only=False,
                effect=ToolEffect.WORKSPACE_WRITE,
            ),
        }}

        exc = ToolExecution(
            execution_id="test-snap",
            ctx=ctx,
            tool_calls=[
                {"name": "write", "args": {"file": str(test_file)}},
            ],
        )
        exc.begin()

        # Verify snapshot captured
        assert exc.snapshot is not None
        assert "files" in exc.snapshot
        assert str(test_file) in exc.snapshot["files"]

        # Modify the file
        test_file.write_text("corrupted content")

        # Restore
        exc._restore_snapshot(exc.snapshot)
        assert test_file.read_text() == "original content"

        # Cleanup
        exc._cleanup_snapshot()

    def test_idempotency_key_generation(self):
        from backend.core.loop.tool_execution import ToolExecution

        exc1 = ToolExecution(
            execution_id="id-1",
            ctx=MagicMock(),
            tool_calls=[
                {"name": "write", "args": {"file": "a.txt"}},
                {"name": "edit", "args": {"file": "b.txt"}},
            ],
        )
        exc2 = ToolExecution(
            execution_id="id-2",
            ctx=MagicMock(),
            tool_calls=[
                {"name": "write", "args": {"file": "a.txt"}},
                {"name": "edit", "args": {"file": "b.txt"}},
            ],
        )
        # Same tool names → same idempotency key (assuming same step/pid)
        assert len(exc1.idempotency_key) == 16
        assert len(exc2.idempotency_key) == 16


# ═══════════════════════════════════════════════════════════════
# Storm Break (P0.3)
# ═══════════════════════════════════════════════════════════════

class TestStormBreak:
    """P0.3: 工具错误螺旋检测。"""

    def test_records_and_detects_repeated_errors(self):
        g = LoopGuard()

        # Record 2 errors — still below threshold
        g.record_tool_error("write", "FILE_NOT_FOUND")
        g.record_tool_error("write", "FILE_NOT_FOUND")
        nudge = g.check_storm_break("write", "FILE_NOT_FOUND")
        assert nudge == ""

        # 3rd error — triggers storm break
        g.record_tool_error("write", "FILE_NOT_FOUND")
        nudge = g.check_storm_break("write", "FILE_NOT_FOUND")
        assert "连续" in nudge
        assert "3" in nudge
        assert "write" in nudge
        assert "FILE_NOT_FOUND" in nudge

    def test_different_tool_resets_counter(self):
        g = LoopGuard()

        g.record_tool_error("write", "FILE_NOT_FOUND")
        g.record_tool_error("write", "FILE_NOT_FOUND")
        g.record_tool_error("write", "FILE_NOT_FOUND")

        # Now call a different tool
        nudge = g.check_storm_break("edit", "PERMISSION_DENIED")
        # Should have cleared old (write, FILE_NOT_FOUND) entries
        assert not any(
            key[:2] == ("write", "FILE_NOT_FOUND")
            for key in g._tool_error_counts
        )

    def test_empty_error_code_skips(self):
        g = LoopGuard()
        g.record_tool_error("write", "")
        nudge = g.check_storm_break("write", "")
        assert nudge == ""

    def test_capability_transition_resets_only_newly_authorized_tools(self):
        g = LoopGuard()
        for _ in range(3):
            g.record_tool_error("write_file", "SELF_EXECUTION_LEASE_REQUIRED")
            g.record_tool_error("web_fetch", "NETWORK_ERROR")

        g.reset_tool_errors({"write_file"})

        assert next(
            count for key, count in g._tool_error_counts.items()
            if key[:2] == ("web_fetch", "NETWORK_ERROR")
        ) == 3
        assert g.check_storm_break(
            "write_file", "SELF_EXECUTION_LEASE_REQUIRED",
        ) == ""

    def test_committed_mutation_invalidates_stale_cross_tool_storm_evidence(self):
        g = LoopGuard()
        for _ in range(3):
            g.record_tool_error("write_file", "FILE_EXISTS")

        # A successful mutation changes the workspace state.  The next write
        # must be evaluated against that new state rather than rejected using
        # the old FILE_EXISTS observations.
        g.record_tool_result(
            "delete_file", {"path": "sample.txt"}, False,
            effect="workspace_write",
        )

        assert g.check_storm_break("write_file", "FILE_EXISTS") == ""

    def test_distinct_command_arguments_are_not_a_retry_storm(self):
        g = LoopGuard()
        failed_commands = [
            {"argv": ["python", "-m", "unittest", "tests.test_one"]},
            {"argv": ["python", "-m", "unittest", "tests.test_two"]},
            {"argv": ["python", "-m", "unittest", "tests.test_three"]},
        ]
        for args in failed_commands:
            g.record_tool_error("exec_command", "COMMAND_EXIT_NONZERO", args)

        candidate = {
            "argv": ["python", "-m", "unittest", "tests.test_four"],
        }
        assert g.check_storm_break(
            "exec_command", "COMMAND_EXIT_NONZERO", candidate,
        ) == ""

    def test_identical_command_arguments_still_trigger_retry_storm(self):
        g = LoopGuard()
        args = {"argv": ["python", "-m", "unittest", "tests.test_one"]}
        for _ in range(3):
            g.record_tool_error("exec_command", "COMMAND_EXIT_NONZERO", args)

        nudge = g.check_storm_break(
            "exec_command", "COMMAND_EXIT_NONZERO", args,
        )
        assert "3" in nudge
        assert "exec_command" in nudge


class TestDoomLoopAccounting:
    """Only consecutive failed tool results may trigger the doom-loop gate."""

    def test_repeated_model_turns_are_not_tool_failures(self, tmp_path_factory):
        from backend.core.loop.manager import AgentProcessManager
        from backend.core.loop.tools import ToolRegistry
        from backend.core.loop.models import RingLevel

        process = AgentProcessManager(max_concurrency=1).fork(
            parent_id=None,
            role="worker",
            tool_registry=ToolRegistry([]),
            max_steps=5,
            ring_level=RingLevel.RING_3,
            workspace_path=str(tmp_path_factory),
            task_id="plain-text-is-not-doom-loop",
            actor_kind="worker",
            task_kind="action",
        )
        process._step_history.extend([
            {"tool_name": "llm_call", "args": "same final"},
            {"tool_name": "llm_call", "args": "same final"},
            {"tool_name": "llm_call", "args": "same final"},
        ])
        result = LoopGuard().check(process, "a distinct response", process.session)
        assert result.reason_code != "doom_loop"

    def test_three_consecutive_matching_tool_failures_trigger(self, tmp_path_factory):
        from backend.core.loop.manager import AgentProcessManager
        from backend.core.loop.tools import ToolRegistry
        from backend.core.loop.models import RingLevel

        process = AgentProcessManager(max_concurrency=1).fork(
            parent_id=None,
            role="worker",
            tool_registry=ToolRegistry([]),
            max_steps=5,
            ring_level=RingLevel.RING_3,
            workspace_path=str(tmp_path_factory),
            task_id="real-doom-loop",
            actor_kind="worker",
            task_kind="action",
        )
        guard = LoopGuard()
        for _ in range(3):
            guard.record_tool_result("edit_file", {"path": "a.py"}, True)
        result = guard.check(process, "still working", process.session)
        assert result.blocked is True
        assert result.reason_code == "doom_loop"

    def test_success_breaks_consecutive_failure_run(self, tmp_path_factory):
        from backend.core.loop.manager import AgentProcessManager
        from backend.core.loop.tools import ToolRegistry
        from backend.core.loop.models import RingLevel

        process = AgentProcessManager(max_concurrency=1).fork(
            parent_id=None,
            role="worker",
            tool_registry=ToolRegistry([]),
            max_steps=5,
            ring_level=RingLevel.RING_3,
            workspace_path=str(tmp_path_factory),
            task_id="broken-doom-loop",
            actor_kind="worker",
            task_kind="action",
        )
        guard = LoopGuard()
        for _ in range(2):
            guard.record_tool_result("edit_file", {"path": "a.py"}, True)
        guard.record_tool_result("read_file", {"path": "a.py"}, False, effect="read")
        guard.record_tool_result("edit_file", {"path": "a.py"}, True)
        result = guard.check(process, "still working", process.session)
        assert result.reason_code != "doom_loop"


# ═══════════════════════════════════════════════════════════════
# Session Persistence (P1.2)
# ═══════════════════════════════════════════════════════════════

class TestSessionPersistence:
    """P1.2: SessionStore —— SQLite/CAS authoritative checkpoints。"""

    def test_append_event_and_load_from_sqlite(self):
        from backend.core.loop.manager import SessionStore

        root = Path(tempfile.mkdtemp())
        store = SessionStore(str(root), state_home=root / "state")
        store.append_event("pid-1", "message_append", {
            "message": {"role": "user", "content": "hello"},
        })
        store.append_event("pid-1", "message_append", {
            "message": {"role": "assistant", "content": "world"},
        })
        msgs = store.load_session("pid-1")
        assert msgs is not None
        assert len(msgs) == 2
        assert msgs[0]["content"] == "hello"
        assert msgs[1]["content"] == "world"

    def test_save_and_load_checkpoint(self):
        from backend.core.loop.manager import SessionStore
        from backend.core.loop.session import AgentSession

        root = Path(tempfile.mkdtemp())
        store = SessionStore(str(root), state_home=root / "state")
        sess = AgentSession()
        sess.append_user("checkpoint hello")
        sess.append_assistant("checkpoint world")

        ck = store.save_checkpoint("pid-2", sess)
        assert ck is not None
        assert ck.startswith("sqlite:session/")

        msgs = store.load_session("pid-2")
        assert msgs is not None
        assert len(msgs) == 2
        assert msgs[0]["content"] == "checkpoint hello"

    def test_checkpoint_supersedes_pre_checkpoint_events(self):
        from backend.core.loop.manager import SessionStore
        from backend.core.loop.session import AgentSession

        root = Path(tempfile.mkdtemp())
        store = SessionStore(str(root), state_home=root / "state")

        # Append several events
        for i in range(5):
            store.append_event("pid-3", "message_append", {
                "message": {"role": "user", "content": f"msg {i}"},
            })

        # The checkpoint advances the durable event cursor. Earlier events stay
        # auditable but are no longer replayed over the canonical snapshot.
        sess = AgentSession()
        sess.append_user("final")
        store.save_checkpoint("pid-3", sess)

        # Load — should get checkpoint data only (2 messages)
        msgs = store.load_session("pid-3")
        assert msgs is not None
        assert len(msgs) == 1

    def test_list_incomplete_finds_active_sessions(self):
        from backend.core.loop.manager import SessionStore

        root = Path(tempfile.mkdtemp())
        store = SessionStore(str(root), state_home=root / "state")
        store.append_event("incomplete-1", "step_start", {"step": 1})
        store.append_event("incomplete-2", "step_start", {"step": 1})

        incomplete = store.list_incomplete()
        assert "incomplete-1" in incomplete
        assert "incomplete-2" in incomplete

    def test_list_incomplete_excludes_retained_terminal_sessions(self):
        from backend.core.loop.manager import SessionStore
        from backend.core.loop.session import AgentSession

        root = Path(tempfile.mkdtemp())
        store = SessionStore(str(root), state_home=root / "state")
        session = AgentSession()
        session.append_user("durable completed session")
        store.save_checkpoint("completed-1", session)
        store.append_event("completed-1", "agent_complete", {
            "status": "completed", "steps_used": 2,
        })

        assert "completed-1" not in store.list_incomplete()

    def test_delete_session_cleans_up(self):
        from backend.core.loop.manager import SessionStore
        from backend.core.loop.session import AgentSession

        root = Path(tempfile.mkdtemp())
        store = SessionStore(str(root), state_home=root / "state")
        sess = AgentSession()
        sess.append_user("data")
        store.save_checkpoint("pid-del", sess)
        store.append_event("pid-del", "step", {"n": 1})

        store.delete_session("pid-del")
        assert store.list_incomplete() == []

    def test_should_checkpoint_triggers_at_limit(self):
        from backend.core.loop.manager import SessionStore

        root = Path(tempfile.mkdtemp())
        store = SessionStore(str(root), state_home=root / "state")
        # Append many events
        for i in range(store.MAX_JSONL_LINES + 10):
            store.append_event("pid-chk", "step", {"i": i})
        assert store.should_checkpoint("pid-chk") is True


# ═══════════════════════════════════════════════════════════════
# ProcessToolRunner (P2.1)
# ═══════════════════════════════════════════════════════════════

class TestProcessToolRunner:
    """P2.1: 子进程工具执行。"""

    def test_runner_executes_registered_tool(self):
        import subprocess as sp

        input_data = json.dumps({
            "tool_name": "file_read",
            "args": {"path": __file__},
        })
        proc = sp.run(
            [sys.executable, "-m", "backend.core.tools.runner"],
            input=input_data, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        result = json.loads(proc.stdout)
        assert result["success"] is True
        assert "error" not in result["data"]
        assert "content" in result["data"]

    def test_runner_rejects_unknown_tool(self):
        import subprocess as sp

        input_data = json.dumps({
            "tool_name": "nonexistent_tool_xyz",
            "args": {},
        })
        proc = sp.run(
            [sys.executable, "-m", "backend.core.tools.runner"],
            input=input_data, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        result = json.loads(proc.stdout)
        assert result["success"] is False

    def test_subprocess_result_timed_out_flag(self):
        from backend.core.loop.process_tool_runner import SubprocessResult

        r = SubprocessResult(success=False, timed_out=True,
                            error="timeout", duration_ms=60000)
        assert r.timed_out is True
        assert r.success is False

    def test_subprocess_result_success_flag(self):
        from backend.core.loop.process_tool_runner import SubprocessResult

        r = SubprocessResult(success=True, data={"ok": True},
                            duration_ms=100, exit_code=0)
        assert r.success is True
        assert r.data == {"ok": True}


# ═══════════════════════════════════════════════════════════════
# LLM Retry Classification (P1.1)
# ═══════════════════════════════════════════════════════════════

class TestLLMRetryClassification:
    """P1.1: LLM 分类重试引擎错误分类。"""

    def test_classify_context_overflow_pattern(self):
        from backend.core.loop.llm import LLMProvider

        # We only test the classification helper, not actual API calls
        provider = LLMProvider("http://localhost", "key", "model")

        # Simulate error message matching
        err = RuntimeError("context length overflow: 15000 tokens exceeds 8192 limit")
        classified = provider._classify_chat_error(err)
        assert classified.code == "CONTEXT_OVERFLOW"

    def test_classify_network_error_pattern(self):
        from backend.core.loop.llm import LLMProvider

        provider = LLMProvider("http://localhost", "key", "model")
        err = RuntimeError("LLM API connection failed: Connection refused")
        classified = provider._classify_chat_error(err)
        assert classified.code == "NETWORK_ERROR"

    def test_classify_http_502_from_message(self):
        from backend.core.loop.llm import LLMProvider

        provider = LLMProvider("http://localhost", "key", "model")
        err = RuntimeError("LLM API error 502: Bad Gateway")
        classified = provider._classify_chat_error(err)
        assert classified.code == "HTTP_502"
        assert classified.is_retryable is True

    def test_classify_http_401_from_message(self):
        from backend.core.loop.llm import LLMProvider

        provider = LLMProvider("http://localhost", "key", "model")
        err = RuntimeError("LLM API error 401: Unauthorized")
        classified = provider._classify_chat_error(err)
        assert classified.is_retryable is False

    def test_parse_retry_after_from_http_error(self):
        from backend.core.loop.llm import LLMProvider
        import urllib.error

        http_err = urllib.error.HTTPError(
            "http://test", 429, "Too Many Requests",
            {"Retry-After": "30"}, None,
        )
        wrapper = RuntimeError("rate limited")
        wrapper.__cause__ = http_err
        retry = LLMProvider._parse_retry_after(wrapper)
        assert retry == 30

    def test_parse_retry_after_returns_none_for_non_http(self):
        from backend.core.loop.llm import LLMProvider

        err = RuntimeError("some error")
        retry = LLMProvider._parse_retry_after(err)
        assert retry is None


# ═══════════════════════════════════════════════════════════════
# AgentTool Isolation Flag (P2.1)
# ═══════════════════════════════════════════════════════════════

class TestAgentToolIsolation:
    """P2.1: AgentTool isolated 标志。"""

    def test_default_is_not_isolated(self):
        tool = AgentTool(
            name="test",
            description="test",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=lambda args: {"ok": True},
        )
        assert tool.isolated is False
        assert tool.timeout == 60.0

    def test_isolated_tool_has_flag(self):
        tool = AgentTool(
            name="isolated_tool",
            description="runs in subprocess",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=lambda args: {"ok": True},
            isolated=True,
            timeout=30.0,
        )
        assert tool.isolated is True
        assert tool.timeout == 30.0
def test_tool_execution_missing_tool_returns_structured_result_not_name_error(tmp_path_factory):
    """The runtime missing-tool branch must import ToolResult at execution time."""
    from backend.core.loop.event_bus import EventBus
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.models import RingLevel
    from backend.core.loop.runtime import AgentRuntimeFactory, RuntimeSpec
    from backend.core.loop.tool_execution import ToolExecution
    from backend.core.loop.tools import ToolRegistry

    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", ring_level=RingLevel.RING_3,
        tool_registry=ToolRegistry([]), max_steps=2,
    ))
    ctx = ExecutionContext(
        process=process, session=process.session,
        workspace_path=str(tmp_path_factory), event_bus=EventBus(),
        cancellation=process.cancellation_event,
    )
    execution = ToolExecution(
        execution_id="missing-tool-execution", ctx=ctx,
        tool_calls=[{"name": "not_registered", "args": {}}],
    )
    results = execution.execute_batch({})
    assert len(results) == 1
    assert results[0].is_error is True
    assert results[0].error == "TOOL_NOT_FOUND"
    assert results[0].formatted
    assert "available_tools" in results[0].formatted
    assert results[0].diagnostics["code"] == "TOOL_NOT_FOUND"
    assert results[0].diagnostics["execution_state"] == "not_started"
    assert results[0].receipt["succeeded"] is False
    assert results[0].receipt["error_code"] == "TOOL_NOT_FOUND"
