"""Local MCP adapter attached to the formal Dashboard's existing NativeHost.

No implicit headless Host fallback: a missing attachment is an explicit error,
never a misleading offline view of another live owner.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading

from backend.core.application import OperationError
from backend.core.observer.bridge import ObserverClient

_DESCRIPTOR = ""
_LOCK = threading.RLock()


def shutdown() -> None:
    # A control client never owns, cancels or closes the Dashboard/daemon.
    global _DESCRIPTOR
    with _LOCK:
        _DESCRIPTOR = ""


def _call(operation, arguments=None, *, confirm=False, request_id=""):
    with _LOCK:
        descriptor = _DESCRIPTOR or os.environ.get("GITGO_CONTROL_DESCRIPTOR", "")
    if not descriptor:
        raise OperationError("OBSERVER_NOT_ATTACHED", "Attach to the running Dashboard's private attach.json; a second Host will not be started.")
    writable = operation not in {"host.capabilities", "runtime.status", "runtime.trace", "runtime.usage",
                                "provider.status", "provider.model.options", "observer.status", "observer.events", "observer.body"}
    if writable and not confirm:
        raise OperationError("OBSERVER_CONTROL_CONFIRMATION_REQUIRED", "Only perform a control action after an explicit user instruction.")
    try:
        result = ObserverClient(descriptor, control=writable).call(operation, arguments,
                   confirm=confirm, request_id=request_id, timeout=1900 if writable else 30)
        return result.get("result") if result.get("ok") else result
    except FileNotFoundError as exc:
        raise OperationError("OBSERVER_DESCRIPTOR_MISSING", "The Host attachment is absent or closed; reconnect to a live run.") from exc


def register(mcp) -> None:
    @mcp.tool(description="Attach read-only to an existing formal Dashboard Host using its private attach.json. Does not start or stop any Host.")
    def gitgo_control_attach(descriptor: str) -> dict:
        global _DESCRIPTOR
        path = str(Path(descriptor).absolute())
        result = ObserverClient(path).call("observer.status")
        if result.get("ok"):
            with _LOCK:
                _DESCRIPTOR = path
        return result.get("result") if result.get("ok") else result

    @mcp.tool(description="Read canonical runtime state from the same Host that owns the Dashboard and its running project daemon.")
    def gitgo_control_status(project: str) -> dict:
        return _call("runtime.status", {"project": project})

    @mcp.tool(description="Read observer coverage, dropped records, archive sources, frontend metrics availability and trial round counters. Read-only.")
    def gitgo_control_observer() -> dict:
        return _call("observer.status")

    @mcp.tool(description="Poll one capture source by cursor, optionally wait up to 20 seconds. Returns metadata and body references; read bodies on demand.")
    def gitgo_control_events(source_id: str, after_seq: int = 0, limit: int = 200, wait_ms: int = 0) -> dict:
        return _call("observer.events", {"source_id": source_id, "after_seq": after_seq, "limit": limit, "wait_ms": wait_ms})

    @mcp.tool(description="Read a redacted capture body in bounded base64 UTF-8 chunks. Concatenate chunks and verify sha256; source/ref come from events.")
    def gitgo_control_body(source_id: str, ref: str, offset: int = 0, limit: int = 262144) -> dict:
        return _call("observer.body", {"source_id": source_id, "ref": ref, "offset": offset, "limit": limit})

    @mcp.tool(description="Read durable task/request/token/cache usage with pagination. Usage is separate from user conversation round counts.")
    def gitgo_control_usage(project: str = "", limit: int = 100, cursor: str = "") -> dict:
        return _call("runtime.usage", {"project": project, "limit": limit, "cursor": cursor})

    @mcp.tool(description="Read configured provider/model identities without credentials. Switching is a separate explicit control action.")
    def gitgo_control_providers(models: bool = False) -> dict:
        return _call("provider.model.options" if models else "provider.status")

    @mcp.tool(description="Switch the original Host's provider at an idle task boundary; preserves session, history and daemon. Requires user authorization and confirm=true.")
    def gitgo_control_switch(provider_id: str, confirm: bool = False, model_id: str = "", expected_revision: str = "") -> dict:
        operation = "provider.model.select" if model_id else "provider.switch"
        args = {"provider_id": provider_id}
        if model_id:
            args["model_id"] = model_id
            args["expected_revision"] = expected_revision
        return _call(operation, args, confirm=confirm)

    @mcp.tool(description="Send an explicitly authorized user message through the original Host admission/governance path. No automatic conversation generation. Omitted max_steps uses the normal product budget.")
    def gitgo_control_chat(project: str, message: str, task_kind: str = "", max_steps: int | None = None,
                          manual_delegation: bool = False, fresh_session: bool = False,
                          confirm: bool = False, request_id: str = "") -> dict:
        args = {"project": project, "message": message, "manual_delegation": manual_delegation,
                "session_mode": "fresh" if fresh_session else "continue"}
        if max_steps is not None:
            args["max_steps"] = max_steps
        if task_kind:
            args["task_kind"] = task_kind
        return _call("runtime.chat", args, confirm=confirm, request_id=request_id)

    @mcp.tool(description="Submit a real authorized answer with exact task/process/decision IDs. Never fabricate a decision ID or grant permissions implicitly.")
    def gitgo_control_decide(project: str, task_id: str, process_id: str, decision_id: str, answer: str,
                            confirm: bool = False, request_id: str = "") -> dict:
        return _call("runtime.decision", {"project": project, "task_id": task_id,
            "process_id": process_id, "decision_id": decision_id, "message": answer}, confirm=confirm, request_id=request_id)

    @mcp.tool(description="Send explicitly authorized feedback or continuation to an exact existing process through its original Host.")
    def gitgo_control_feedback(project: str, process_id: str, message: str, confirm: bool = False) -> dict:
        return _call("runtime.feedback", {"project": project, "process_id": process_id, "message": message}, confirm=confirm)

    @mcp.tool(description="Interrupt an exact process only when the user requests intervention. Never automatically stop work because an observer detects a problem.")
    def gitgo_control_stop(project: str, process_id: str, confirm: bool = False) -> dict:
        return _call("runtime.stop", {"project": project, "process_id": process_id}, confirm=confirm)

    @mcp.tool(description="Explicit manual compaction through the same runtime. Force requires the exact pending decision ID and user instruction.")
    def gitgo_control_compact(project: str, process_id: str = "", decision_id: str = "", choice: str = "", confirm: bool = False) -> dict:
        return _call("runtime.compact", {"project": project, "process_id": process_id,
                     "decision_id": decision_id, "choice": choice}, confirm=confirm)

    @mcp.tool(description="Read canonical Trace list/read/detail/summary/index/block. For context sources use action=detail and options.view=input_sources/input_source/record_source. Not formal UI acceptance.")
    def gitgo_control_trace(project: str, action: str = "list", trace_id: str = "", process_id: str = "",
                           after_seq: int = 0, limit: int = 200, ref: str = "", include_deltas: bool = False,
                           options: dict | None = None) -> dict:
        return _call("runtime.trace", {**(options or {}), "project": project, "action": action,
                     "trace_id": trace_id, "process_id": process_id, "after_seq": after_seq,
                     "limit": max(1, min(limit, 1000)), "ref": ref, "include_deltas": include_deltas})
