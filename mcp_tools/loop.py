"""MCP tools — loop layer: process status, agent chat, agent instruct.

v0.34: MCP 工具变为薄适配器。编排逻辑已下沉到 daemon 的 task 命令。
       本文件只负责：构建治理上下文 + 通过 DaemonClient 转发到 daemon。
"""

from __future__ import annotations


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

        cfg = ConfigManager.load()
        proj = next((p for p in cfg.projects if p.name == project), None)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}

        workspace = proj.workspace.file_access.path if proj.workspace else ""

        # Build governance context (structured — daemon may enrich it further)
        ctx = {}
        try:
            ctx = build_governance_context(project, workspace)
        except Exception:
            pass

        # Try daemon pathway via native task command
        try:
            from mcp_tools.daemon_registry import get_client
            client = get_client(project)

            if client.is_running():
                cmd = {
                    "cmd": "task",
                    "action": "chat",
                    "instruction": message,
                    "role": "executor",
                    "ring_level": 3,
                    "max_steps": 50,
                    "context_snapshot": ctx,
                    "task_description": message[:200],
                }
                complete = client.send_task(cmd, timeout=300)
                process_id = complete.get("process_id", "")
                response = complete.get("result", {}).get("response", "")
                HistoryManager.add_operation(
                    project, "agent_chat", "success",
                    {"message": message[:200], "response": response[:500],
                     "process_id": process_id, "llm_used": True},
                )
                return {
                    "project": project,
                    "process_id": process_id,
                    "response": response or "(无回复)",
                    "status": complete.get("result", {}).get("status", ""),
                    "steps_used": complete.get("result", {}).get("steps_used", 0),
                    "llm_used": True,
                }
        except Exception:
            pass

        # Fallback: direct LLM or mock
        return _chat_fallback(project, message, workspace, ctx)

    @mcp.tool(description="向指定 Agent 发送补充指令")
    def gitgo_agent_instruct(project: str, process_id: str, instruction: str) -> dict:
        """Send a human instruction to a specific agent process via the daemon."""
        try:
            from mcp_tools.daemon_registry import get_client
            client = get_client(project)

            if client.is_running():
                result = client.send_command({
                    "cmd": "dispatch_tool",
                    "process_id": process_id,
                    "tool": "status",
                    "args": {"instruction": instruction, "semantic": True},
                })
                return {
                    "project": project,
                    "process_id": process_id,
                    "status": "dispatched",
                    "dispatch_result": result,
                }
        except Exception:
            pass

        from backend.core.history import HistoryManager
        HistoryManager.add_operation(
            project, "agent_instruct", "recorded",
            {"process_id": process_id, "instruction": instruction[:500]},
        )
        return {
            "project": project,
            "process_id": process_id,
            "status": "instruction_recorded",
            "instruction": instruction[:200],
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

        # Fallback: record kill in history so status reconstruction reflects it
        from backend.core.history import HistoryManager
        HistoryManager.add_operation(
            project, "agent_killed", "recorded",
            {"process_id": process_id},
        )
        return {
            "project": project,
            "process_id": process_id,
            "killed": process_id,
        }


# ── Fallback helpers ─────────────────────────────────────────


def _chat_fallback(project: str, message: str, workspace: str,
                   ctx: dict) -> dict:
    """Direct LLM call (env vars or config file) or mock response."""
    from backend.core.history import HistoryManager
    import os

    llm_used = False
    response = ""

    # Resolve LLM config
    base_url = os.environ.get("GITGO_LLM_BASE_URL", "")
    api_key = os.environ.get("GITGO_LLM_API_KEY", "")
    model_id = os.environ.get("GITGO_LLM_MODEL", "")

    if not (base_url and api_key and model_id) and workspace:
        try:
            from backend.core.llm_config import LLMConfigManager
            active = LLMConfigManager.get_active()
            if active:
                base_url, api_key, model_id = active.base_url, active.api_key, active.model_id
        except Exception:
            pass

    if base_url and api_key and model_id:
        try:
            from backend.core.loop.llm import LLMProvider
            provider = LLMProvider(base_url, api_key, model_id)
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

    HistoryManager.add_operation(
        project, "agent_chat", "success",
        {"message": message[:200], "response": response[:500],
         "llm_used": llm_used},
    )
    return {"project": project, "response": response, "llm_used": llm_used}


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
