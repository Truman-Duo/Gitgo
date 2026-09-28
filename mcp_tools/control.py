"""Local automation controls over Gitgo's canonical Native Host.

This module is intentionally a transport adapter.  It does not construct
Agent sessions, infer task kinds, grant permissions, or call an LLM directly;
all state-changing work goes through the same NativeHost used by the formal
Dashboard.  The separate server keeps this small, auditable control surface
out of the broad legacy compatibility MCP.
"""

from __future__ import annotations

import atexit
import io
import threading
import uuid

from backend.core.native_host import NativeHost


_HOST: NativeHost | None = None
_LOCK = threading.RLock()


def _host() -> NativeHost:
    global _HOST
    with _LOCK:
        if _HOST is None:
            # Native protocol envelopes must never be written onto MCP stdio.
            # Durable Trace remains available through gitgo_control_trace.
            _HOST = NativeHost(stdout=io.StringIO())
        return _HOST


def shutdown() -> None:
    global _HOST
    with _LOCK:
        host, _HOST = _HOST, None
    if host is not None:
        host.close()


atexit.register(shutdown)


def register(mcp) -> None:
    @mcp.tool(description=(
        "Read one project's canonical runtime projection: A/B processes, "
        "context, cache, decisions, storage health, and durable conversations."
    ))
    def gitgo_control_status(project: str) -> dict:
        return _host()._runtime_status(project)

    @mcp.tool(description=(
        "Send a user message through Gitgo's canonical Native Host. This uses "
        "the same admission, governance, permissions, Agent loop and persistence "
        "as the formal Dashboard; it never uses the legacy direct-LLM fallback."
    ))
    def gitgo_control_chat(
        project: str,
        message: str,
        task_kind: str = "",
        max_steps: int = 50,
        manual_delegation: bool = False,
        fresh_session: bool = False,
    ) -> dict:
        arguments = {
            "project": project,
            "message": message,
            "max_steps": max_steps,
            "manual_delegation": manual_delegation,
            "session_mode": "fresh" if fresh_session else "continue",
        }
        if task_kind:
            arguments["task_kind"] = task_kind
        return _host()._runtime_chat(str(uuid.uuid4()), arguments)

    @mcp.tool(description=(
        "Answer one exact Gitgo decision card. Supply the task_id, process_id "
        "and decision_id returned by status/chat; the Host validates all three."
    ))
    def gitgo_control_decide(
        project: str, task_id: str, process_id: str, decision_id: str, answer: str,
    ) -> dict:
        return _host()._runtime_chat(str(uuid.uuid4()), {
            "project": project,
            "task_id": task_id,
            "process_id": process_id,
            "decision_id": decision_id,
            "message": answer,
        }, action="decision")

    @mcp.tool(description=(
        "Send feedback to a live B, or continue the same durable B session "
        "after it has completed."
    ))
    def gitgo_control_feedback(project: str, process_id: str, message: str) -> dict:
        return _host()._runtime_feedback(
            project, process_id, message, request_id=str(uuid.uuid4()),
        )

    @mcp.tool(description="Interrupt one exact Agent process through the canonical cancellation path.")
    def gitgo_control_stop(project: str, process_id: str) -> dict:
        return _host()._runtime_stop(project, process_id)

    @mcp.tool(description=(
        "Run manual compaction for one project/process. A force choice is only "
        "accepted with the exact pending decision id."
    ))
    def gitgo_control_compact(
        project: str, process_id: str = "", decision_id: str = "", choice: str = "",
    ) -> dict:
        return _host()._runtime_compact(
            project, process_id, decision_id=decision_id, choice=choice,
        )

    @mcp.tool(description=(
        "Read the durable Gitgo Trace. action is list, read, summary, or detail; "
        "large tool/reasoning bodies remain content-addressed detail refs."
    ))
    def gitgo_control_trace(
        project: str,
        action: str = "list",
        trace_id: str = "",
        process_id: str = "",
        after_seq: int = 0,
        limit: int = 200,
        ref: str = "",
        include_deltas: bool = False,
    ) -> dict:
        return _host()._runtime_trace(project, {
            "action": action,
            "trace_id": trace_id,
            "process_id": process_id,
            "after_seq": after_seq,
            "limit": max(1, min(limit, 1000)),
            "ref": ref,
            "include_deltas": include_deltas,
        })
