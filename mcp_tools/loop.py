"""MCP tools — loop layer: process status, agent chat, agent instruct.

All three tools route through the gitgo daemon via DaemonClient (subprocess
stdin/stdout JSON protocol). When the daemon is unavailable or LLM is not
configured, they fall back to mock / history-file behavior.
"""

from __future__ import annotations

import os


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
                result = client.send_command({"cmd": "loop_status"})
                return {"project": project, **result}
        except Exception:
            pass

        # Fallback: reconstruct from history files (original behavior)
        return _loop_status_from_history(project)

    @mcp.tool(description="向项目 A 级 Agent 发送消息，触发 LLM 调用并返回回复")
    def gitgo_agent_chat(project: str, message: str) -> dict:
        """Send a message to the project's A-level Agent (planner, ring 0).

        Flow:
        1. Ensure daemon is running and LLM is configured
        2. Find or fork an A-level agent (role=planner, ring=0)
        3. Build governance brief as system context
        4. Call LLM through daemon, wait for response
        5. Fall back to mock if LLM is unavailable
        """
        from backend.core.config import ConfigManager
        from backend.core.loop.context_builder import build_governance_brief
        from backend.core.history import HistoryManager

        # Resolve workspace path for governance brief
        cfg = ConfigManager.load()
        proj = None
        for p in cfg.projects:
            if p.name == project:
                proj = p
                break
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}

        workspace = proj.workspace.file_access.path if proj.workspace else ""

        # Build governance brief
        brief = build_governance_brief(project, workspace)
        system_prompt = (
            f"你是项目 {project} 的 A 级治理 Agent (ring 0)。\n"
            f"工作区: {workspace}\n\n"
            f"## 治理简报\n"
            f"{brief.get('phase_brief', '')}\n"
            f"{brief.get('contract_summary', '')}\n"
            f"{brief.get('lesson_matches', '')}\n"
            f"{brief.get('rejection_history', '')}\n"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ]

        # Try daemon pathway
        try:
            from mcp_tools.daemon_registry import get_client
            client = get_client(project)

            if client.is_running():
                return _chat_via_daemon(
                    client, project, messages, brief, workspace,
                )
        except Exception:
            pass

        # Fallback: direct LLM or mock
        return _chat_fallback(project, message, messages, brief, workspace)

    @mcp.tool(description="向指定 B 级 Agent 发送补充指令")
    def gitgo_agent_instruct(project: str, process_id: str, instruction: str) -> dict:
        """Send a human instruction to a specific B-level agent process.

        Dispatches via daemon's ToolDispatcher using 'status' tool as carrier.
        Falls back to history-only logging if daemon is unavailable.
        """
        # Try daemon pathway
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
        except Exception as e:
            pass

        # Fallback: log to history only
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


# ── daemon-path helpers ──────────────────────────────────────

def _resolve_llm_config(workspace: str) -> tuple | None:
    """Resolve LLM config with priority: env vars > config file > None."""
    base_url = os.environ.get("GITGO_LLM_BASE_URL", "")
    api_key = os.environ.get("GITGO_LLM_API_KEY", "")
    model_id = os.environ.get("GITGO_LLM_MODEL", "")

    if base_url and api_key and model_id:
        return (base_url, api_key, model_id)

    if workspace:
        try:
            from backend.core.llm_config import LLMConfigManager
            active = LLMConfigManager.get_active(workspace)
            if active:
                return (active.base_url, active.api_key, active.model_id)
        except Exception:
            pass

    return None


def _chat_via_daemon(client, project: str, messages: list[dict],
                     brief: dict, workspace: str = "") -> dict:
    """Route agent_chat through daemon: configure LLM → fork A agent → llm_call."""
    from backend.core.history import HistoryManager

    llm_cfg = _resolve_llm_config(workspace)
    llm_configured = False

    if llm_cfg:
        base_url, api_key, model_id = llm_cfg
        try:
            client.send_command({
                "cmd": "llm_configure",
                "base_url": base_url,
                "api_key": api_key,
                "model_id": model_id,
            })
            llm_configured = True
        except Exception:
            pass

    if not llm_configured:
        # LLM not available through daemon — fall back to mock
        return _chat_fallback(
            project, messages[-1]["content"], messages, brief, workspace,
        )

    # Find or fork A-level agent (planner, ring 0)
    a_pid = _ensure_a_agent(client, project)

    # Send LLM call through daemon
    try:
        llm_result = client.send_llm_call(messages, process_id=a_pid)
        response = llm_result.get("response", "") or "(无回复)"

        HistoryManager.add_operation(
            project, "agent_chat", "success",
            {"message": messages[-1]["content"][:200],
             "response": response[:500],
             "process_id": a_pid, "llm_used": True},
        )

        return {
            "project": project,
            "process_id": a_pid,
            "response": response,
            "llm_used": True,
        }
    except Exception as e:
        HistoryManager.add_operation(
            project, "agent_chat", "failed",
            {"message": messages[-1]["content"][:200],
             "error": str(e)[:500],
             "process_id": a_pid, "llm_used": True},
        )
        raise


def _ensure_a_agent(client, project: str) -> str:
    """Find existing A-level agent (planner, ring 0) or fork a new one."""
    loop = client.send_command({"cmd": "loop_status"})
    processes = loop.get("processes", {})

    for pid, proc in processes.items():
        if proc.get("role") == "planner" and proc.get("ring_level") == 0:
            if proc.get("status") == "running":
                return pid

    # Fork new A agent
    fork_result = client.send_command({
        "cmd": "fork_agent",
        "role": "planner",
        "ring": "0",
        "max_steps": 50,
    })
    return fork_result.get("process_id", "")


def _chat_fallback(project: str, message: str, messages: list[dict],
                   brief: dict, workspace: str) -> dict:
    """Direct LLM call (env vars or config file) or mock response."""
    from backend.core.history import HistoryManager

    llm_used = False
    response = ""

    llm_cfg = _resolve_llm_config(workspace)
    if llm_cfg:
        base_url, api_key, model_id = llm_cfg
        try:
            from backend.core.loop.llm import LLMProvider
            provider = LLMProvider(base_url, api_key, model_id)
            response = provider.chat(messages)
            llm_used = True
        except Exception:
            pass

    if not llm_used:
        response = (
            f"[Mock A Agent] 收到消息: {message[:100]}\n\n"
            f"项目: {project}\n"
            f"工作区: {workspace}\n"
            f"治理简报已加载: phase_brief={bool(brief.get('phase_brief'))}, "
            f"contract={bool(brief.get('contract_summary'))}, "
            f"lessons={bool(brief.get('lesson_matches'))}\n\n"
            f"（LLM 未配置。在 Dashboard 按 L 键打开 LLM 配置面板，"
            f"或设置 GITGO_LLM_BASE_URL / GITGO_LLM_API_KEY / "
            f"GITGO_LLM_MODEL 环境变量。）"
        )

    HistoryManager.add_operation(
        project, "agent_chat", "success",
        {"message": message[:200], "response": response[:500],
         "llm_used": llm_used},
    )

    return {
        "project": project,
        "response": response,
        "llm_used": llm_used,
    }


def _loop_status_from_history(project: str) -> dict:
    """Reconstruct process state from HistoryManager (original fallback)."""
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
