"""MCP tools — loop layer: process status, agent chat, agent instruct.

v0.34: MCP 工具变为薄适配器。编排逻辑已下沉到 daemon 的 task 命令。
       本文件只负责：构建治理上下文 + 通过 DaemonClient 转发到 daemon。
"""

from __future__ import annotations

import uuid


# The daemon owns the durable AgentSession. This adapter only remembers the
# opaque identity needed to continue the same project conversation.
_AGENT_SESSION_IDS: dict[str, str] = {}


def register(mcp):
    """Register loop tools on FastMCP instance."""

    @mcp.tool(description="获取项目 Agent 进程树状态、daemon 在线状态、最近工具调用记录")
    def gitgo_loop_status(project: str) -> dict:
        """Query daemon for live process tree. Falls back to history-file
        reconstruction if daemon is not running."""
        try:
            from mcp_tools.daemon_registry import get_client
            client = get_client(project)
            if client.is_running():
                result = client.send_command({"cmd": "task", "action": "status"})
                return {"project": project, **result}
        except Exception:
            pass

        return _loop_status_from_history(project)

    @mcp.tool(description="向项目 Agent 发送消息，触发 LLM 调用并返回回复")
    def gitgo_agent_chat(project: str, message: str) -> dict:
        """Send a message to the project's Agent. The daemon handles:
        LLM config resolution, agent lifecycle, governance context injection,
        and agent_step execution. This tool is a thin adapter.

        Falls back to direct LLM or mock if daemon is unavailable.
        """
        from backend.core.config import ConfigManager
        from backend.core.loop.context_builder import build_governance_context
        from backend.core.history import HistoryManager
        from backend.core.loop.outcome import TaskOutcome

        task_id = str(uuid.uuid4())

        cfg = ConfigManager.load()
        proj = next((p for p in cfg.projects if p.name == project), None)
        if proj is None:
            return {
                "project": project,
                **TaskOutcome.failed(
                    task_id=task_id,
                    process_id="",
                    process_status="not_created",
                    code="PROJECT_NOT_FOUND",
                    message=f"Project not found: {project}",
                    llm_used=False,
                ).to_dict(),
            }

        workspace = proj.workspace.file_access.path if proj.workspace else ""

        # Build governance context (structured — daemon may enrich it further)
        ctx = {}
        try:
            ctx = build_governance_context(project, workspace)
        except Exception:
            pass

        # Try daemon pathway via native task command
        daemon_error = ""
        try:
            from mcp_tools.daemon_registry import get_client
            client = get_client(project)

            if client.is_running():
                cmd = {
                    "cmd": "task",
                    "action": "chat",
                    "task_id": task_id,
                    "instruction": message,
                    "role": "supervisor",
                    "actor_kind": "supervisor",
                    "capability_profile_id": "supervisor.control",
                    "task_kind": "answer",
                    "max_steps": 50,
                    "context_snapshot": {
                        **ctx,
                        "signals": [
                            signal.to_dict() if hasattr(signal, "to_dict") else signal
                            for signal in ctx.get("signals", [])
                        ],
                    },
                    "task_description": message[:200],
                }
                session_id = _AGENT_SESSION_IDS.get(project, "")
                if session_id:
                    cmd["session_id"] = session_id
                transport_error = None
                try:
                    complete = client.send_task(cmd, timeout=300)
                except Exception as exc:
                    transport_error = exc
                    if session_id and "Session not found:" in str(exc):
                        # The daemon rejected the stale handle before starting
                        # work. Clear it and make one safe fresh-session retry.
                        _AGENT_SESSION_IDS.pop(project, None)
                        cmd.pop("session_id", None)
                        try:
                            complete = client.send_task(cmd, timeout=300)
                            transport_error = None
                        except Exception as retry_exc:
                            transport_error = retry_exc
                if transport_error is not None:
                    outcome = TaskOutcome.failed(
                        task_id=task_id,
                        process_id="",
                        process_status="unknown",
                        code="DAEMON_TASK_TRANSPORT_FAILED",
                        message=str(transport_error),
                        retryable=True,
                        llm_used=False,
                    )
                    HistoryManager.add_operation(
                        project, "agent_chat", "failed",
                        {"message": message[:200],
                         "task_id": task_id,
                         "error_code": outcome.error.code},
                    )
                    return {"project": project, **outcome.to_dict()}

                if complete.get("session_id"):
                    _AGENT_SESSION_IDS[project] = str(complete["session_id"])

                outcome = TaskOutcome.from_dict(complete.get("outcome", {}))
                HistoryManager.add_operation(
                    project, "agent_chat", outcome.status.value,
                    {"message": message[:200],
                     "response": outcome.response[:500],
                     "task_id": outcome.task_id,
                     "process_id": outcome.process_id,
                     "error_code": outcome.error.code if outcome.error else "",
                     "llm_used": outcome.llm_used},
                )
                return {"project": project, **outcome.to_dict()}
            daemon_error = "daemon_not_running"
        except Exception as exc:
            daemon_error = str(exc)

        # Fallback is only allowed before a daemon task is accepted.
        return _chat_fallback(
            project, message, workspace, ctx,
            task_id=task_id,
            fallback_reason=daemon_error or "daemon_unavailable",
        )

    @mcp.tool(description="向指定 Agent 发送补充指令")
    def gitgo_agent_instruct(project: str, process_id: str, instruction: str) -> dict:
        """Send a human instruction to a specific agent process via the daemon."""
        try:
            from mcp_tools.daemon_registry import get_client
            client = get_client(project)

            if client.is_running():
                result = client.send_command({
                    "cmd": "task",
                    "action": "instruct",
                    "process_id": process_id,
                    "instruction": instruction,
                })
                return {
                    "project": project,
                    "process_id": process_id,
                    **result,
                }
        except Exception as exc:
            return {
                "project": project,
                "process_id": process_id,
                "status": "failed",
                "error": {
                    "code": "INSTRUCTION_NOT_ACCEPTED",
                    "message": str(exc),
                },
            }

        return {
            "project": project,
            "process_id": process_id,
            "status": "failed",
            "error": {
                "code": "DAEMON_UNAVAILABLE",
                "message": "Instruction was not accepted because daemon is unavailable",
            },
        }

    @mcp.tool(description="打断指定 Agent 进程（停止其任务线程）")
    def gitgo_stop_process(project: str, process_id: str) -> dict:
        """Stop a running agent process via the daemon's task kill action."""
        try:
            from mcp_tools.daemon_registry import get_client
            client = get_client(project)

            if client.is_running():
                result = client.send_command({
                    "cmd": "task",
                    "action": "kill",
                    "process_id": process_id,
                })
                return {"project": project, "process_id": process_id, **result}
        except Exception:
            pass

        return {
            "project": project,
            "process_id": process_id,
            "requested": False,
            "status": "unconfirmed",
            "error": {
                "code": "DAEMON_UNAVAILABLE",
                "message": "Cannot confirm cancellation because daemon is unavailable",
            },
        }


# ── Fallback helpers ─────────────────────────────────────────


def _chat_fallback(project: str, message: str, workspace: str,
                   ctx: dict, *, task_id: str,
                   fallback_reason: str) -> dict:
    """Direct LLM call (env vars or config file) or mock response."""
    from backend.core.history import HistoryManager
    from backend.core.loop.outcome import OutcomeStatus, TaskError, TaskOutcome
    import os

    llm_used = False
    response = ""

    # Resolve LLM config
    base_url = os.environ.get("GITGO_LLM_BASE_URL", "")
    api_key = os.environ.get("GITGO_LLM_API_KEY", "")
    model_id = os.environ.get("GITGO_LLM_MODEL", "")
    protocol = os.environ.get("GITGO_LLM_PROTOCOL", "openai_chat")
    capabilities = {}

    if not (base_url and api_key and model_id) and workspace:
        try:
            from backend.core.llm_config import LLMConfigManager
            active = LLMConfigManager.get_active()
            if active:
                base_url, api_key, model_id = active.base_url, active.api_key, active.model_id
                protocol, capabilities = active.protocol, active.runtime_capabilities()
        except Exception:
            pass

    if base_url and api_key and model_id:
        try:
            from backend.core.loop.llm import LLMProvider
            provider = LLMProvider(
                base_url, api_key, model_id,
                protocol=protocol, capabilities=capabilities,
            )
            brief_text = ctx.get("brief", "")
            messages = [
                {"role": "system", "content": f"你是项目 {project} 的 Agent。\n\n{brief_text}"},
                {"role": "user", "content": message},
            ]
            response = provider.chat(messages)
            llm_used = True
        except Exception:
            pass

    if not llm_used:
        response = (
            f"[Mock Agent] 收到消息: {message[:100]}\n\n"
            f"项目: {project}\n"
            f"工作区: {workspace}\n"
            f"治理上下文: brief={bool(ctx.get('brief'))}, "
            f"signals={len(ctx.get('signals', []))}\n\n"
            f"（LLM 未配置。在 Dashboard 按 L 键打开 LLM 配置面板，"
            f"或设置 GITGO_LLM_BASE_URL / GITGO_LLM_API_KEY / "
            f"GITGO_LLM_MODEL 环境变量。）"
        )

    error = TaskError(
        code="AGENT_RUNTIME_UNAVAILABLE" if llm_used else "LLM_NOT_CONFIGURED",
        message=(
            f"Agent runtime unavailable; direct LLM fallback used: {fallback_reason}"
            if llm_used else
            f"Agent runtime unavailable and no LLM configured: {fallback_reason}"
        ),
        retryable=True,
    )
    outcome = TaskOutcome(
        task_id=task_id,
        process_id="",
        status=OutcomeStatus.DEGRADED,
        process_status="not_created",
        response=response,
        error=error,
        llm_used=llm_used,
        metadata={"fallback_reason": fallback_reason, "mock": not llm_used},
    )
    HistoryManager.add_operation(
        project, "agent_chat", "degraded",
        {"message": message[:200], "response": response[:500],
         "task_id": task_id, "llm_used": llm_used,
         "error_code": error.code},
    )
    return {"project": project, **outcome.to_dict()}


def _loop_status_from_history(project: str) -> dict:
    """Reconstruct process state from HistoryManager (fallback)."""
    from backend.core.history import HistoryManager

    entries = HistoryManager.load()
    project_entries = [e for e in entries if e.project_name == project]

    processes: dict[str, dict] = {}
    for e in project_entries:
        if e.operation == "agent_forked":
            d = e.detail
            pid = d.get("process_id", "")
            processes[pid] = {
                "process_id": pid,
                "role": d.get("role", ""),
                "ring_level": d.get("ring_level", 0),
                "status": "running",
                "steps_used": 0,
                "max_steps": d.get("max_steps", 50),
                "parent_id": d.get("parent_id"),
                "created_at": e.timestamp,
            }
        elif e.operation == "agent_killed":
            pid = e.detail.get("process_id", "")
            if pid in processes:
                processes[pid]["status"] = "killed"
        elif e.operation == "agent_reaped":
            pid = e.detail.get("process_id", "")
            if pid in processes:
                processes[pid]["status"] = "orphaned"

    for e in project_entries:
        if e.operation == "tool_executed":
            pid = e.detail.get("process_id", "")
            if pid in processes:
                processes[pid]["steps_used"] = processes[pid].get("steps_used", 0) + 1

    recent_tools = []
    for e in project_entries:
        if e.operation == "tool_executed":
            d = e.detail
            recent_tools.append({
                "timestamp": e.timestamp,
                "process_id": d.get("process_id", ""),
                "tool_name": d.get("tool_name", ""),
                "allowed": d.get("allowed", False),
                "duration_ms": d.get("duration_ms", 0),
                "role": d.get("role", ""),
            })
    recent_tools = recent_tools[-20:]

    return {
        "project": project,
        "daemon_online": False,
        "processes": processes,
        "recent_tool_executed": recent_tools,
    }
