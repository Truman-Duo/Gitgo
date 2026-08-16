"""stdin command dispatch — registry-based handlers.

Extracted from daemon/__init__.py (pure structural refactor). The if/elif chain
is replaced by a ``COMMAND_HANDLERS`` dict; each handler receives the request_id
injecting ``emit`` shim instead of capturing a closure-local ``_emit``.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from backend.core.config import ConfigManager, ProjectConfig
from backend.core.sync_session import SyncSession
from backend.core.daemon.emit import _emit as _emit_global
from backend.core.daemon.persist import _save_session_checkpoint
from backend.core.daemon.policy_helpers import (
    _snapshot_workspace, _harvest_from_rejection_chain, _resolve_llm_config,
)
from backend.core.loop.models import RingLevel
from backend.core.loop.tools import ToolRegistry


# ── Command Handlers ────────────────────────────────────────


def _cmd_fork_agent(cmd, session, project, daemon_ctx, emit):
    apm = daemon_ctx.get("apm") if daemon_ctx else None
    if apm is None:
        emit({"event": "command_result", "cmd": "fork_agent",
              "error": "AgentProcessManager not available"})
        return
    role = cmd.get("role", "worker")
    ring = RingLevel.RING_3 if cmd.get("ring", "3") != "0" else RingLevel.RING_0
    tool_names = cmd.get("tools", [])
    max_steps = cmd.get("max_steps", 50)
    parent_id = cmd.get("parent_id")
    context_snapshot = cmd.get("context_snapshot")
    registry = ToolRegistry(tool_names)
    try:
        proc = apm.fork(parent_id=parent_id, role=role,
                       tool_registry=registry, max_steps=max_steps,
                       ring_level=ring, context_snapshot=context_snapshot)
        emit({"event": "command_result", "cmd": "fork_agent",
              "result": {"process_id": proc.process_id, "role": role,
                         "ring": ring.value}})
    except ValueError as e:
        emit({"event": "command_result", "cmd": "fork_agent",
              "error": str(e)})


def _cmd_dispatch_tool(cmd, session, project, daemon_ctx, emit):
    apm = daemon_ctx.get("apm") if daemon_ctx else None
    dispatcher = daemon_ctx.get("dispatcher") if daemon_ctx else None
    if dispatcher is None or apm is None:
        emit({"event": "command_result", "cmd": "dispatch_tool",
              "error": "ToolDispatcher or AgentProcessManager not available"})
        return
    process_id = cmd.get("process_id", "")
    tool_name = cmd.get("tool", "")
    tool_args = cmd.get("args", {})
    process = apm.get(process_id)
    if process is None:
        emit({"event": "command_result", "cmd": "dispatch_tool",
              "error": f"Process not found: {process_id}"})
        return
    result = dispatcher.dispatch(process, tool_name, tool_args)
    emit({"event": "command_result", "cmd": "dispatch_tool",
          "result": {
              "allowed": result.allowed,
              "data": result.data,
              "error": result.error,
              "duration_ms": result.duration_ms,
              "steps_remaining": result.steps_remaining,
              "process_status": process.status.value,
          }})


def _cmd_llm_configure(cmd, session, project, daemon_ctx, emit):
    base_url = cmd.get("base_url", "")
    api_key = cmd.get("api_key", "")
    model_id = cmd.get("model_id", "")
    if not base_url or not api_key or not model_id:
        emit({"event": "command_result", "cmd": "llm_configure",
              "error": "base_url, api_key, model_id are all required"})
        return
    from backend.core.loop.llm import LLMProvider
    daemon_ctx["llm"] = LLMProvider(base_url, api_key, model_id)
    emit({"event": "command_result", "cmd": "llm_configure",
          "result": {"model": model_id, "base_url": base_url}})


def _cmd_llm_call(cmd, session, project, daemon_ctx, emit):
    llm_provider = daemon_ctx.get("llm") if daemon_ctx else None
    evq = daemon_ctx.get("evq") if daemon_ctx else None
    if llm_provider is None:
        emit({"event": "command_result", "cmd": "llm_call",
              "error": "LLM not configured. Send llm_configure first."})
        return
    if evq is None:
        emit({"event": "command_result", "cmd": "llm_call",
              "error": "Event queue not available"})
        return
    messages = cmd.get("messages", [])
    process_id = cmd.get("process_id", "")
    if not messages:
        emit({"event": "command_result", "cmd": "llm_call",
              "error": "messages required"})
        return
    # Run LLM call in background thread to avoid blocking main loop
    def _call_llm_thread():
        try:
            response = llm_provider.chat(messages)
            evq.put({"event": "llm_response", "process_id": process_id,
                     "response": response, "status": "success"})
        except Exception as exc:
            evq.put({"event": "llm_response", "process_id": process_id,
                     "response": None, "status": "error",
                     "error": str(exc)})
    threading.Thread(target=_call_llm_thread, daemon=True,
                    name=f"llm-{process_id[:8]}").start()
    emit({"event": "command_result", "cmd": "llm_call",
          "result": {"status": "pending", "process_id": process_id}})


def _cmd_status(cmd, session, project, daemon_ctx, emit):
    raw = cmd.get("raw", False)
    semantic_only = cmd.get("semantic_only", False)
    if semantic_only:
        d = session.status_dict(semantic=True)
        emit({"event": "command_result", "cmd": "status",
              "result": d.get("semantic", {})})
    else:
        emit({"event": "command_result", "cmd": "status",
              "result": session.status_dict(semantic=not raw)})


def _cmd_scan(cmd, session, project, daemon_ctx, emit):
    emit({"event": "operation_started", "op": "scan"})
    try:
        session.step_scan(hash_cache=daemon_ctx.get("hash_cache"))
        session.step_load_commits()
        emit({"event": "operation_complete", "op": "scan",
              "status": "success",
              "result": session.status_dict(semantic=True)})
    except Exception as exc:
        emit({"event": "operation_complete", "op": "scan",
              "status": "failed", "error": str(exc)})


def _cmd_formalize(cmd, session, project, daemon_ctx, emit):
    indices = cmd.get("indices")
    message = cmd.get("message")
    session.step_load_commits()
    if indices is not None:
        session.selected_workspace = set(indices)
    fc = session.step_create_formal_commit(message=message)
    if fc:
        emit({"event": "command_result", "cmd": "formalize",
              "result": {"commit": f"[{fc.prefix}-{fc.number}]",
                         "message": fc.message}})
    else:
        emit({"event": "command_result", "cmd": "formalize",
              "result": None, "error": "create_formal_commit failed"})


def _cmd_sync(cmd, session, project, daemon_ctx, emit):
    emit({"event": "operation_started", "op": "sync"})
    ok = session.step_sync()
    emit({"event": "operation_complete", "op": "sync",
          "status": "success" if ok else "failed"})


def _cmd_push(cmd, session, project, daemon_ctx, emit):
    emit({"event": "operation_started", "op": "push"})
    ok, _ = session.step_push()
    emit({"event": "operation_complete", "op": "push",
          "status": "success" if ok else "failed"})


def _cmd_trial(cmd, session, project, daemon_ctx, emit):
    action = cmd.get("action", "list")
    if action == "list":
        result = [
            {"index": i, "hash": c.hash, "message": c.message,
             "author": c.author, "date": c.date,
             "triage": c.triage.value}
            for i, c in enumerate(session.incoming_changes)
        ]
        emit({"event": "command_result", "cmd": "trial",
              "result": result})
    elif action in ("accept", "promote", "discard"):
        idx = cmd.get("index")
        if idx is None:
            emit({"event": "command_result", "cmd": "trial",
                  "error": "index required"})
            return
        ok = session.step_triage_incoming(idx, action)
        emit({"event": "command_result", "cmd": "trial",
              "result": "ok" if ok else "failed"})


def _cmd_session(cmd, session, project, daemon_ctx, emit):
    action = cmd.get("action", "status")
    if action == "save":
        path = session.save_session()
        emit({"event": "command_result", "cmd": "session",
              "result": {"saved": str(path)}})
    elif action == "status":
        emit({"event": "command_result", "cmd": "session",
              "result": session.status_dict(semantic=True)})
    elif action == "resume":
        loaded = SyncSession.load_session(project, ConfigManager.load())
        emit({"event": "command_result", "cmd": "session",
              "result": {"resumed": loaded is not None}})


def _cmd_round_complete(cmd, session, project, daemon_ctx, emit):
    changed = _snapshot_workspace(session, project)

    # ── v0.35 Phase 3: 回收 —— round_complete 时从上下文撤出知识 ──
    try:
        from backend.core.knowledge.models import (
            classify_lesson_heat, get_sticky_lessons,
        )
        from backend.core.knowledge.lesson import LessonManager

        ws = Path(session.workspace_path)
        all_lessons = (
            LessonManager.load_instance(ws, project.name)
            + LessonManager.load_pending(ws, project.name)
        )
        sticky_ids = set(get_sticky_lessons(all_lessons))

        # 遍历 A Agent session（如果存在），标记非 sticky 的 recall 结果
        # 注意：此 worktree 版没有 ContextWindow；主动 prunes 留给未来
        emit({
            "event": "recycle_check",
            "total_lessons": len(all_lessons),
            "sticky_count": len(sticky_ids),
            "hot_lesson_ids": list(sticky_ids)[:5],
        })
    except Exception:
        pass

    emit({"event": "command_result", "cmd": "round_complete",
          "result": {"snapshot": changed is not None,
                     "files": len(changed) if changed else 0}})


def _cmd_reject(cmd, session, project, daemon_ctx, emit):
    reason = cmd.get("reason", "")
    instruction = cmd.get("instruction", "")
    from backend.core.history import HistoryManager
    HistoryManager.add_operation(
        project.name, "rejection", "recorded",
        {"round": cmd.get("round", 0),
         "reason": reason,
         "instruction": instruction,
         "timestamp": datetime.now().isoformat()},
        correlation_id=session._correlation_id,
    )
    entries = HistoryManager.load()
    project_entries = [e for e in entries if e.project_name == project.name]
    rejections = [e for e in project_entries if e.operation == "rejection"]
    if len(rejections) >= 3:
        recent = project_entries[-20:]
        last_rej_idx = max(
            (i for i, e in enumerate(recent) if e.operation == "rejection"),
            default=-1,
        )
        if last_rej_idx >= 0:
            post_rej = [e for i, e in enumerate(recent) if i > last_rej_idx
                        and e.operation == "policy_check_result"
                        and e.status == "success"]
            if post_rej:
                _harvest_from_rejection_chain(project.name, rejections, session)
    emit({"event": "command_result", "cmd": "reject",
          "result": {"rejection_count": len(rejections)}})


def _cmd_loop_status(cmd, session, project, daemon_ctx, emit):
    apm = daemon_ctx.get("apm") if daemon_ctx else None
    processes = {}
    if apm is not None:
        for pid, proc in apm._processes.items():
            processes[pid] = {
                "process_id": proc.process_id,
                "role": proc.role,
                "ring_level": proc.ring_level.value,
                "status": proc.status.value,
                "steps_used": proc.steps_used,
                "max_steps": proc.max_steps,
                "parent_id": proc.parent_id,
                "created_at": proc.created_at,
                "worktree_path": getattr(proc, "worktree_path", ""),
                "provider_id": getattr(proc, "provider_id", ""),
                "model_id": getattr(proc, "model_id", ""),
                "estimated_tokens": getattr(proc, "estimated_tokens", 0),
            }
    from backend.core.history import HistoryManager
    entries = HistoryManager.load()
    recent_tools = []
    for e in entries:
        if e.operation == "tool_executed" and e.project_name == project.name:
            d = e.detail
            recent_tools.append({
                "timestamp": e.timestamp,
                "process_id": d.get("process_id", ""),
                "tool_name": d.get("tool_name", ""),
                "allowed": d.get("allowed", False),
                "duration_ms": d.get("duration_ms", 0),
                "role": d.get("role", ""),
                "blocked_reason": d.get("blocked_reason", ""),
                "diff": d.get("diff", ""),
            })
    recent_tools = recent_tools[-20:]
    emit({"event": "command_result", "cmd": "loop_status",
          "result": {
              "daemon_online": True,
              "processes": processes,
              "recent_tool_executed": recent_tools,
              "providers": [],
          }})


def _cmd_task(cmd, session, project, daemon_ctx, emit):
    # ── 原生 Task 命令 —— Agent 编排的单一入口 ──
    # 整合了 MCP 层之前的 _resolve_llm_config / _ensure_agent / _chat_via_daemon 逻辑。
    # MCP 工具变为薄适配器：只构建上下文 + 调用此命令。
    apm = daemon_ctx.get("apm") if daemon_ctx else None
    dispatcher = daemon_ctx.get("dispatcher") if daemon_ctx else None
    evq = daemon_ctx.get("evq") if daemon_ctx else None
    llm_provider = daemon_ctx.get("llm") if daemon_ctx else None
    action = cmd.get("action", "chat")

    if action == "fork":
        # 仅 fork Agent，不执行
        if apm is None:
            emit({"event": "command_result", "cmd": "task",
                  "error": "AgentProcessManager not available"})
            return
        role = cmd.get("role", "executor")
        ring = RingLevel.RING_3 if str(cmd.get("ring_level", "3")) != "0" else RingLevel.RING_0
        tool_names = cmd.get("tool_registry", [])
        max_steps = cmd.get("max_steps", 50)
        parent_id = cmd.get("parent_id")
        context_snapshot = cmd.get("context_snapshot")
        provider_id = cmd.get("provider_id", "")
        model_id = cmd.get("model_id", "")
        registry = ToolRegistry(tool_names)
        try:
            proc = apm.fork(
                parent_id=parent_id, role=role,
                tool_registry=registry, max_steps=max_steps,
                ring_level=ring, context_snapshot=context_snapshot,
                workspace_path=str(session.workspace_path),
                provider_id=provider_id, model_id=model_id,
            )
            emit({"event": "command_result", "cmd": "task",
                  "result": {"process_id": proc.process_id, "role": role,
                             "ring_level": ring.value}})
        except ValueError as e:
            emit({"event": "command_result", "cmd": "task",
                  "error": str(e)})
        return

    if action == "status":
        # 查询所有 Agent 进程状态
        processes = {}
        if apm is not None:
            for pid, proc in apm._processes.items():
                processes[pid] = {
                    "process_id": proc.process_id,
                    "role": proc.role,
                    "ring_level": proc.ring_level.value,
                    "status": proc.status.value,
                    "steps_used": proc.steps_used,
                    "max_steps": proc.max_steps,
                    "parent_id": proc.parent_id,
                    "created_at": proc.created_at,
                    "worktree_path": getattr(proc, "worktree_path", ""),
                    "provider_id": getattr(proc, "provider_id", ""),
                    "model_id": getattr(proc, "model_id", ""),
                    "estimated_tokens": getattr(proc, "estimated_tokens", 0),
                }
        # v0.45: include recent_tool_executed from history for v4 dashboard
        from backend.core.history import HistoryManager
        entries = HistoryManager.load()
        recent_tools = []
        for e in entries:
            if e.operation == "tool_executed" and e.project_name == project.name:
                d = e.detail
                recent_tools.append({
                    "timestamp": e.timestamp,
                    "process_id": d.get("process_id", ""),
                    "tool_name": d.get("tool_name", ""),
                    "allowed": d.get("allowed", False),
                    "duration_ms": d.get("duration_ms", 0),
                    "role": d.get("role", ""),
                    "blocked_reason": d.get("blocked_reason", ""),
                    "diff": d.get("diff", ""),
                })
        recent_tools = recent_tools[-20:]
        emit({"event": "command_result", "cmd": "task",
              "result": {"daemon_online": True,
                         "processes": processes,
                         "recent_tool_executed": recent_tools,
                         "providers": []}})
        return

    if action == "kill":
        if apm is None:
            emit({"event": "command_result", "cmd": "task",
                  "error": "AgentProcessManager not available"})
            return
        process_id = cmd.get("process_id", "")
        apm.kill(process_id)
        emit({"event": "command_result", "cmd": "task",
              "result": {"killed": process_id}})
        return

    if action == "chat":
        # ── chat: 完整 Agent 编排 ──
        if apm is None or dispatcher is None or evq is None:
            emit({"event": "command_result", "cmd": "task",
                  "error": "AgentProcessManager, ToolDispatcher, or event queue not available"})
            return

        instruction = cmd.get("instruction", "")
        role = cmd.get("role", "executor")
        ring = RingLevel.RING_3 if str(cmd.get("ring_level", "3")) != "0" else RingLevel.RING_0
        max_steps = cmd.get("max_steps", 50)
        context_snapshot = cmd.get("context_snapshot")
        provider_id = cmd.get("provider_id", "")
        model_id = cmd.get("model_id", "")
        task_description = cmd.get("task_description", instruction[:200] if instruction else "")

        # Resolve LLM config
        llm = llm_provider
        if llm is None:
            cfg = _resolve_llm_config(str(session.workspace_path))
            if cfg:
                from backend.core.loop.llm import LLMProvider
                llm = LLMProvider(cfg[0], cfg[1], cfg[2])
        if llm is None:
            emit({"event": "command_result", "cmd": "task",
                  "error": "LLM not configured. Set env vars or configure in Dashboard."})
            return

        # Build governance context if not provided (v0.43: fix G6 — use existing build_governance_brief)
        if context_snapshot is None:
            try:
                from backend.core.loop.context_builder import build_governance_brief
                context_snapshot = build_governance_brief(
                    project.name, str(session.workspace_path),
                )
            except Exception:
                context_snapshot = {}

        # Inject daemon's latest governance signals into context
        gov_signals = daemon_ctx.get("governance_signals")
        if gov_signals and context_snapshot:
            if "signals" not in context_snapshot:
                brief_parts = []
                for s in gov_signals:
                    if s.severity.value in ("critical", "high"):
                        brief_parts.append(
                            f"[{s.severity.value.upper()}] {s.suggestion or s.rule}"
                        )
                context_snapshot = {
                    **context_snapshot,
                    "signals": gov_signals,
                    "brief": "; ".join(brief_parts[:5]) if brief_parts else "",
                }

        # v0.43: Try Scheduler multi-agent path first
        # Check if task should be decomposed across multiple B agents
        target_files = cmd.get("target_files", [])
        use_scheduler = cmd.get("multi_agent", False) and len(target_files) >= 2

        if use_scheduler:
            try:
                from backend.core.loop.scheduler import SlotScheduler
                from pathlib import Path
                ws = str(session.workspace_path)
                dep_graph = {}
                try:
                    from backend.core.contract import load_function_graph
                    dep_graph = load_function_graph(Path(ws))
                except Exception:
                    pass
                scheduler = SlotScheduler()
                scheduler_result = scheduler.run(
                    task_description=task_description,
                    target_files=target_files,
                    process=process if process is not None else None,
                    session=None,  # Scheduler creates sessions per slot
                    workspace_path=ws,
                    llm_provider=llm,
                    dep_graph=dep_graph,
                )
                emit({"event": "command_result", "cmd": "task",
                      "result": {
                          "status": scheduler_result.get("status", ""),
                          "total_slots": scheduler_result.get("total_slots", 0),
                          "completed_slots": scheduler_result.get("completed_slots", 0),
                      }})
                return
            except Exception as e:
                emit({"event": "scheduler_fallback", "reason": str(e)})
                # Fall through to single-B path

        # Find or fork agent
        process_id = cmd.get("process_id", "")
        process = apm.get(process_id) if process_id else None
        if process is None:
            # Fork new agent
            tool_names = cmd.get("tool_registry", [])
            registry = ToolRegistry(tool_names)
            try:
                process = apm.fork(
                    parent_id=cmd.get("parent_id"),
                    role=role, tool_registry=registry,
                    max_steps=max_steps, ring_level=ring,
                    context_snapshot=context_snapshot,
                    workspace_path=str(session.workspace_path),
                    provider_id=provider_id, model_id=model_id,
                )
            except ValueError as e:
                emit({"event": "command_result", "cmd": "task",
                      "error": str(e)})
                return
        else:
            # Resume existing agent — update context
            process.task_description = task_description
            if context_snapshot:
                process.context_snapshot = context_snapshot

        # Run agent_step in background thread
        from backend.core.loop.executor import agent_step

        # v0.45: backoff retry config for task thread crashes
        _MAX_TASK_RETRIES = 3
        _TASK_BASE_DELAY = 2.0
        _MAX_RAPID_FAILURES = 5
        _RAPID_WINDOW = 30.0
        _task_failure_times: list[float] = []

        def _run_task_thread():
            # v0.44: on_stream_event 闭包 —— 流式事件即时入队
            def _emit_stream_event(event):
                evq.put(event)

            last_error = None
            for attempt in range(_MAX_TASK_RETRIES + 1):
                try:
                    result = agent_step(
                        process, llm, instruction, dispatcher,
                        workspace_path=str(session.workspace_path),
                        on_stream_event=_emit_stream_event,
                    )
                    # v0.45: persist session checkpoint on success
                    _save_session_checkpoint(daemon_ctx, process)
                    evq.put({"event": "agent_complete",
                             "process_id": process.process_id,
                             "result": result})
                    return
                except Exception as exc:
                    last_error = exc
                    # v0.45: rapid failure detection
                    now = time.time()
                    _task_failure_times.append(now)
                    _task_failure_times[:] = [
                        t for t in _task_failure_times
                        if now - t < _RAPID_WINDOW
                    ]
                    if len(_task_failure_times) >= _MAX_RAPID_FAILURES:
                        break  # too many rapid failures, give up

                    if attempt < _MAX_TASK_RETRIES:
                        delay = min(
                            _TASK_BASE_DELAY * (2 ** attempt),
                            30.0,
                        )
                        time.sleep(delay)
                        # v0.45: kill lingering thread state before retry
                        process.steps_used = max(0, process.steps_used - 1)

            # All retries exhausted or rapid failure threshold hit
            _save_session_checkpoint(daemon_ctx, process)
            evq.put({"event": "agent_complete",
                     "process_id": process.process_id,
                     "error": f"task_crashed_after_{_MAX_TASK_RETRIES}_retries: {last_error}"})

        threading.Thread(
            target=_run_task_thread, daemon=True,
            name=f"task-{process.process_id[:8]}",
        ).start()
        emit({"event": "command_result", "cmd": "task",
              "result": {"status": "pending",
                         "process_id": process.process_id}})
        return

    emit({"event": "command_result", "cmd": "task",
          "error": f"Unknown task action: {action}"})


COMMAND_HANDLERS = {
    "fork_agent": _cmd_fork_agent,
    "dispatch_tool": _cmd_dispatch_tool,
    "llm_configure": _cmd_llm_configure,
    "llm_call": _cmd_llm_call,
    "status": _cmd_status,
    "scan": _cmd_scan,
    "formalize": _cmd_formalize,
    "sync": _cmd_sync,
    "push": _cmd_push,
    "trial": _cmd_trial,
    "session": _cmd_session,
    "round_complete": _cmd_round_complete,
    "reject": _cmd_reject,
    "loop_status": _cmd_loop_status,
    "task": _cmd_task,
}


def _handle_command(cmd: dict, session: SyncSession, project: ProjectConfig,
                    daemon_ctx: dict = None,
                    on_shutdown: callable = None) -> None:
    """Dispatch a stdin command to the appropriate step method."""
    cmd_name = cmd.get("cmd", "")

    # v0.44: 注入 request_id 到所有 command_result 事件，使 JS sendCommand 能匹配响应
    request_id = cmd.get("request_id", "")

    def emit(ev: dict) -> None:
        if ev.get("event") == "command_result" and request_id:
            ev = dict(ev)
            ev["request_id"] = request_id
        _emit_global(ev)

    if cmd_name == "shutdown":
        emit({"event": "shutdown_ack", "message": "Shutting down"})
        if on_shutdown:
            on_shutdown()
        return

    handler = COMMAND_HANDLERS.get(cmd_name)
    if handler is None:
        emit({"event": "command_result", "cmd": cmd_name,
              "error": f"Unknown command: {cmd_name}"})
        return

    handler(cmd, session, project, daemon_ctx, emit)
