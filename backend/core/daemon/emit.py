"""Emit — line-delimited JSON protocol I/O with micro-batch flush.

Extracted from daemon/__init__.py (pure structural refactor).
"""

from __future__ import annotations

import json
import sys
import time

from backend.core.protocol_io import write_utf8_line


def _emit(event: dict) -> None:
    """Write a line-delimited JSON event to stdout (backward-compat wrapper)."""
    _emit_v2(event)


# ── v0.44: 微批 flush ──
_emit_buffer: list[dict] = []
_last_flush_time: float = 0.0
BATCH_SIZE = 32
BATCH_INTERVAL_MS = 16


def _emit_v2(ev: dict, priority: str = "normal") -> None:
    """Write a line-delimited JSON event to stdout with micro-batch flush.

    priority="normal": 微批缓冲（16ms 或 32 事件）
    priority="immediate": 立即 flush（协议屏障、agent_complete、stream_recovery、治理事件）
    """
    global _last_flush_time
    if priority == "immediate":
        _flush_emit_buffer()
        write_utf8_line(sys.stdout, json.dumps(ev, ensure_ascii=False))
        return
    _emit_buffer.append(ev)
    now_ms = int(time.time() * 1000)
    if len(_emit_buffer) >= BATCH_SIZE or (now_ms - _last_flush_time) >= BATCH_INTERVAL_MS:
        _flush_emit_buffer()
        _last_flush_time = now_ms


def _flush_emit_buffer() -> None:
    """Flush 所有缓冲事件到 stdout。"""
    global _emit_buffer
    if not _emit_buffer:
        return
    write_utf8_line(
        sys.stdout,
        "\n".join(json.dumps(e, ensure_ascii=False) for e in _emit_buffer),
    )
    _emit_buffer = []
