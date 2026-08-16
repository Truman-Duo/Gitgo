"""Agent tool executors — lifted from run_daemon closures (pure structural refactor).

Each executor takes an explicit ``(daemon_ctx, session, project, args)`` signature
instead of capturing free variables from the enclosing run_daemon scope.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.history import HistoryManager


def _exec_scan(daemon_ctx, session, project, args: dict) -> dict:
    hash_cache = daemon_ctx["hash_cache"]
    changed = args.get("files", [])
    if changed:
        session.step_scan_files(changed, hash_cache=hash_cache)
    else:
        session.step_scan(hash_cache=hash_cache)
    session.step_load_commits()
    return session.status_dict(semantic=True)


def _exec_status(daemon_ctx, session, project, args: dict) -> dict:
    return session.status_dict(semantic=args.get("semantic", True))


def _exec_formalize(daemon_ctx, session, project, args: dict) -> dict:
    indices = args.get("indices")
    message = args.get("message")
    if indices is not None:
        session.selected_workspace = set(indices)
    fc = session.step_create_formal_commit(message=message)
    if fc:
        return {"commit": f"[{fc.prefix}-{fc.number}]", "message": fc.message}
    return {"error": "FORMALIZE_FAILED"}


# v0.35: Knowledge recall tools
def _exec_recall_grep(daemon_ctx, session, project, args: dict) -> dict:
    from backend.core.knowledge.recall import recall_grep
    return recall_grep(
        query=args.get("query", ""),
        project=project.name,
        top_k=args.get("top_k", 10),
        agent_context=args.get("agent_context"),
        workspace=str(session.workspace_path),
    )


def _exec_recall_semantic(daemon_ctx, session, project, args: dict) -> dict:
    from backend.core.knowledge.recall import recall_semantic
    return recall_semantic(
        query=args.get("query", ""),
        project=project.name,
        top_k=args.get("top_k", 10),
        agent_context=args.get("agent_context"),
        workspace=str(session.workspace_path),
    )


def _exec_recall_rag(daemon_ctx, session, project, args: dict) -> dict:
    from backend.core.knowledge.recall import recall_rag
    return recall_rag(
        query=args.get("query", ""),
        project=project.name,
        agent_context=args.get("agent_context"),
        workspace=str(session.workspace_path),
    )


# v0.36: Context Assembler 工具
def _exec_assemble_context(daemon_ctx, session, project, args: dict) -> dict:
    from backend.core.knowledge.recall import recall_grep
    from backend.core.loop.signal_normalizer import SignalNormalizer
    from backend.core.contract import load_function_graph, get_callers

    task = args.get("task", "")
    files = args.get("files", [])
    ws = str(session.workspace_path)

    # Phase 1: needed — PolicyEngine 最近一次 check
    entries = HistoryManager.load()
    policy = [e for e in entries[-20:]
              if e.operation == "policy_check_result"]
    normalizer = SignalNormalizer()
    signals = normalizer.normalize(
        policy_results=policy[0].detail if policy else {},
    )
    needed = [{
        "source": s.source,
        "severity": s.severity.value,
        "rule": s.rule,
        "target_files": s.target_files,
        "target_tools": s.target_tools,
        "required_tools": s.required_tools,
    } for s in signals
        if any(f in s.target_files for f in files)]

    # Phase 2: relevant — recall 检索
    search_terms = " ".join(files) + " " + task
    recalled = recall_grep(
        search_terms, project.name,
        workspace=str(session.workspace_path),
    )
    lessons = recalled.get("lessons", [])

    # Phase 3: dependency — 函数级调用链
    dependency = {}
    try:
        func_graph = load_function_graph(Path(ws))
    except Exception:
        func_graph = {}
    for f in files:
        callers = get_callers(Path(ws), f) if func_graph else []
        dependency[f] = {"callers": callers[:10]}

    # 预估 token
    estimated = len(str(needed)) // 4 + len(str(lessons)) // 4

    return {
        "needed": needed,
        "relevant": lessons[:10],
        "dependency": dependency,
        "transcript_tokens": estimated,
        "context_utilization_ratio": round(estimated / 128000, 3),
    }


def _exec_assemble_return_context(daemon_ctx, session, project, args: dict) -> dict:
    process_id = args.get("process_id", "")
    apm = daemon_ctx.get("apm")
    process = apm.get(process_id) if apm else None
    if not process:
        return {"error": "process not found"}

    return _build_return_context(process)


# v0.36: decompose_task executor
def _exec_decompose_task(daemon_ctx, session, project, args: dict) -> dict:
    """decompose_task 工具实现。

    LLM 调用此工具建议拆分方案。Scheduler 进行 structural 验证。
    """
    from backend.core.loop.decomposition import suggest_split
    task = args.get("task", "")
    files = args.get("files", [])
    suggestions = suggest_split(task, files)
    return {
        "suggested_splits": [
            {
                "task_description": s.task_description,
                "target_files": s.target_files,
                "estimated_steps": s.estimated_steps,
            }
            for s in suggestions
        ],
        "total_suggestions": len(suggestions),
        "note": "拆分建议需经 Scheduler structural 验证后才能执行。",
    }


def _build_return_context(process) -> dict:
    """从 AgentProcess 构建 B→A 返回转录。"""
    from backend.core.loop.transcript import TaskTranscriptBuilder
    # 如果 process 有 transcript builder 实例，用它的
    tb = getattr(process, '_transcript_builder', None)
    if tb:
        return tb.to_return_context()

    # fallback: 从 session 提取
    session = process.session
    tools = []
    if session:
        for m in session.messages:
            if m.get("message_type") == "tool_result":
                tn = m.get("_tool_name", "unknown")
                if tn not in tools:
                    tools.append(tn)

    return {
        "task": process.task_description or "",
        "status": process.status.value,
        "steps": process.steps_used,
        "tools": tools,
        "lessons_triggered": [],
        "governance_events": [],
    }
