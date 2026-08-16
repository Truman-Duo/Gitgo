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
from backend.core.loop.agent_tool import AgentTool


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

    def test_is_write_tool_recognizes_write_ops(self):
        from backend.core.loop.tool_execution import ToolExecution

        exc = ToolExecution(
            execution_id="test-4",
            ctx=MagicMock(),
            tool_calls=[],
        )
        assert exc._is_write_tool("formalize") is True
        assert exc._is_write_tool("write") is True
        assert exc._is_write_tool("edit") is True
        assert exc._is_write_tool("delete") is True
        assert exc._is_write_tool("scan") is False
        assert exc._is_write_tool("status") is False

    def test_extract_write_targets_finds_paths(self):
        from backend.core.loop.tool_execution import ToolExecution

        exc = ToolExecution(
            execution_id="test-5",
            ctx=MagicMock(),
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
        assert g._tool_error_counts.get(("write", "FILE_NOT_FOUND")) is None

    def test_empty_error_code_skips(self):
        g = LoopGuard()
        g.record_tool_error("write", "")
        nudge = g.check_storm_break("write", "")
        assert nudge == ""


# ═══════════════════════════════════════════════════════════════
# Session Persistence (P1.2)
# ═══════════════════════════════════════════════════════════════

class TestSessionPersistence:
    """P1.2: SessionStore —— JSONL + atomic checkpoint。"""

    def test_append_and_load_jsonl(self):
        from backend.core.loop.manager import SessionStore

        store = SessionStore(str(tempfile.mkdtemp()))
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

        store = SessionStore(str(tempfile.mkdtemp()))
        sess = AgentSession()
        sess.append_user("checkpoint hello")
        sess.append_assistant("checkpoint world")

        ck = store.save_checkpoint("pid-2", sess)
        assert ck is not None
        assert Path(ck).exists()

        msgs = store.load_session("pid-2")
        assert msgs is not None
        assert len(msgs) == 2
        assert msgs[0]["content"] == "checkpoint hello"

    def test_checkpoint_truncates_jsonl(self):
        from backend.core.loop.manager import SessionStore
        from backend.core.loop.session import AgentSession

        store = SessionStore(str(tempfile.mkdtemp()))

        # Append several events
        for i in range(5):
            store.append_event("pid-3", "message_append", {
                "message": {"role": "user", "content": f"msg {i}"},
            })

        # Save checkpoint — should truncate jsonl
        sess = AgentSession()
        sess.append_user("final")
        store.save_checkpoint("pid-3", sess)

        # Load — should get checkpoint data only (2 messages)
        msgs = store.load_session("pid-3")
        assert msgs is not None
        assert len(msgs) == 1  # checkpoint overwrites jsonl

    def test_list_incomplete_finds_active_sessions(self):
        from backend.core.loop.manager import SessionStore

        store = SessionStore(str(tempfile.mkdtemp()))
        store.append_event("incomplete-1", "step_start", {"step": 1})
        store.append_event("incomplete-2", "step_start", {"step": 1})

        incomplete = store.list_incomplete()
        assert "incomplete-1" in incomplete
        assert "incomplete-2" in incomplete

    def test_delete_session_cleans_up(self):
        from backend.core.loop.manager import SessionStore
        from backend.core.loop.session import AgentSession

        store = SessionStore(str(tempfile.mkdtemp()))
        sess = AgentSession()
        sess.append_user("data")
        store.save_checkpoint("pid-del", sess)
        store.append_event("pid-del", "step", {"n": 1})

        store.delete_session("pid-del")
        assert store.list_incomplete() == []

    def test_should_checkpoint_triggers_at_limit(self):
        from backend.core.loop.manager import SessionStore

        store = SessionStore(str(tempfile.mkdtemp()))
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
            input=input_data, capture_output=True, text=True, timeout=10,
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
            input=input_data, capture_output=True, text=True, timeout=10,
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
