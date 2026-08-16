"""Session persistence helpers — checkpoint + incomplete-session recovery.

Extracted from daemon/__init__.py (pure structural refactor).
"""

from __future__ import annotations


def _save_session_checkpoint(daemon_ctx: dict, process) -> None:
    """Save session checkpoint after agent_step completes or errors."""
    store = daemon_ctx.get("session_store")
    if store is None:
        return
    sess = getattr(process, "session", None)
    if sess is None:
        return
    store.save_checkpoint(process.process_id, sess)
    store.append_event(process.process_id, "agent_complete", {
        "status": process.status.value,
        "steps_used": process.steps_used,
    })


def _scan_incomplete_sessions(session_store, apm) -> list[str]:
    """扫描 .gitgo/sessions/ 中的未完成会话，返回可恢复的 process_id 列表。

    有 checkpoint 或 jsonl 数据但进程不在 apm 中的视为"未完成"。
    """
    incomplete = session_store.list_incomplete()
    recoverable = []
    for pid in incomplete:
        if apm.get(pid) is None:
            msgs = session_store.load_session(pid)
            if msgs:
                recoverable.append(pid)
    return recoverable
