"""Persistent Daemon Core — long-running process with file watch + trial poll + stdin commands.

Architecture:
    watcher (Thread-1) ──┐
    poller  (Thread-2) ──┼── event_queue ──► Main Loop (主线程) ──► stdout (JSON)
    reader  (Thread-3) ──┘

The main loop owns the SyncSession and dispatches events to step methods.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.core.sync_session import SyncSession, SessionStage
from backend.core.daemon.watcher import WorkspaceWatcher
from backend.core.daemon.poller import TrialPoller
from backend.core.daemon.commands import CommandReader


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
    priority="immediate": 立即 flush（agent_complete、stream_recovery、治理事件）
    """
    global _last_flush_time
    if priority == "immediate":
        _flush_emit_buffer()
        sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
        sys.stdout.flush()
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
    for e in _emit_buffer:
        sys.stdout.write(json.dumps(e, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    _emit_buffer = []


def _pid_file_path(project: ProjectConfig) -> Path:
    ws_path = project.workspace.file_access.path
    return Path(ws_path) / ".gitgo" / "daemon.pid"


def _acquire_pid_file(project: ProjectConfig) -> bool:
    """Create PID file. Returns False if another daemon is already running."""
    pid_path = _pid_file_path(project)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
            os.kill(old_pid, 0)  # signal 0 = existence check (Unix only)
        except (OSError, ValueError, ProcessLookupError, SystemError):
            pass  # stale or Windows — overwrite
        else:
            return False  # alive

    pid_path.write_text(str(os.getpid()))
    return True


def _release_pid_file(project: ProjectConfig) -> None:
    pid_path = _pid_file_path(project)
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


# ── Policy Engine ─────────────────────────────────────────

from backend.core.policy import PolicyEngine, build_policy_message
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.agent_tool import AgentTool
from backend.core.loop.tool_wrappers import (
    contract_detect_drift,
    contract_get_impact,
    contract_get_changed_symbols,
    lesson_search,
    lesson_discard,
    lesson_verify,
    lesson_harvest,
    lesson_promote,
    lesson_list,
    privacy_scan,
    memory_snapshot,
    memory_restore,
)
from backend.core.dispatch import ToolDispatcher


def _snapshot_workspace(session: SyncSession, project: ProjectConfig) -> list[str] | None:
    """每轮结束时在 workspace 做 git commit 快照。"""
    import subprocess

    ws = str(session.workspace_path)
    changed = [e.rel_path for e in session.entries if e.status != "same"]

    try:
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        subprocess.run(["git", "add", "-A"], cwd=ws,
                       capture_output=True, text=True,
                       creationflags=creationflags, timeout=30)

        msg = f"gitgo: round snapshot [{datetime.now().strftime('%H:%M:%S')}]\n\n"
        msg += f"变更文件: {len(changed)}\n"
        if changed:
            msg += "\n".join(f"  {f}" for f in changed[:20])
            if len(changed) > 20:
                msg += f"\n  ... 还有 {len(changed) - 20} 个文件"

        result = subprocess.run(["git", "commit", "-m", msg], cwd=ws,
                                capture_output=True, text=True,
                                creationflags=creationflags, timeout=30)

        if result.returncode == 0:
            from backend.core.history import HistoryManager
            HistoryManager.add_operation(
                project.name, "workspace_state_snapshot", "success",
                {"files_changed": changed,
                 "round_time": datetime.now().isoformat()},
                correlation_id=session._correlation_id,
            )
            _emit({"event": "workspace_snapshot", "files": len(changed)})
            return changed
        return []  # no changes to commit
    except (subprocess.SubprocessError, OSError) as e:
        _emit({"event": "snapshot_error", "error": str(e)})
        return None


def _harvest_from_rejection_chain(
    project_name: str, rejections: list, session: SyncSession
) -> None:
    """从连续 rejection 中提取 pending lesson。"""
    from backend.core.knowledge.lesson import LessonManager
    from backend.core.knowledge.models import Lesson
    from backend.core.history import HistoryManager

    recent_3 = rejections[-3:]
    reasons = []
    for r in recent_3:
        d = r.detail if isinstance(r.detail, dict) else {}
        reasons.append(d.get("reason", ""))

    last_detail = recent_3[-1].detail if isinstance(recent_3[-1].detail, dict) else {}
    final_rule = last_detail.get("instruction", "")

    lesson = Lesson(
        tech_stack="",
        category="process",
        severity="high",
        trigger=f"连续 3 次被人否定: {'; '.join(reasons[-2:])}",
        rule=final_rule or "人连续纠正了多次方向性错误，最终方案需要被记录。",
    )
    lesson.id = f"rejection_{project_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    ws = Path(session.workspace_path)
    LessonManager.save_pending(ws, lesson)

    HistoryManager.add_operation(
        project_name, "governance_lesson", "success",
        {"harvested_count": 1, "lesson_id": lesson.id, "trigger": "rejection_chain"},
        correlation_id=session._correlation_id,
    )
    _emit({"event": "lesson_harvested", "lesson_id": lesson.id,
           "trigger": "rejection_chain"})


def _resolve_llm_config(workspace: str) -> tuple | None:
    """解析 LLM 配置。优先级：环境变量 > llm_config.json active_provider。"""
    base_url = os.environ.get("GITGO_LLM_BASE_URL", "")
    api_key = os.environ.get("GITGO_LLM_API_KEY", "")
    model_id = os.environ.get("GITGO_LLM_MODEL", "")

    if base_url and api_key and model_id:
        return (base_url, api_key, model_id)

    if workspace:
        try:
            from backend.core.llm_config import LLMConfigManager
            active = LLMConfigManager.get_active()
            if active:
                return (active.base_url, active.api_key, active.model_id)
        except Exception:
            pass

    return None


# ── v0.45: Session persistence helpers ─────────────────────

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


# ── v0.45: Resource cleanup on shutdown ─────────────────────

def _cleanup_resources(workspace_path: str) -> None:
    """清理 daemon 关闭时的临时资源。

    - 清理旧快照备份（.gitgo/snapshots/）
    - 清理已完成会话的持久化文件（.gitgo/sessions/ 中无对应运行进程的）
    - 不删除正在运行的进程的会话文件
    """
    ws = Path(workspace_path)

    # 清理过期快照（保留最近 MAX_SNAPSHOTS 个）
    snap_dir = ws / ".gitgo" / "snapshots"
    if snap_dir.exists():
        try:
            backups = sorted(snap_dir.glob("*@v*"),
                           key=lambda p: p.stat().st_mtime)
            from backend.core.loop.tool_execution import MAX_SNAPSHOTS
            if len(backups) > MAX_SNAPSHOTS:
                for old in backups[:len(backups) - MAX_SNAPSHOTS]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    # 清理临时文件（.gitgo/tmp/）
    tmp_dir = ws / ".gitgo" / "tmp"
    if tmp_dir.exists():
        try:
            import shutil
            shutil.rmtree(str(tmp_dir), ignore_errors=True)
        except OSError:
            pass


def run_daemon(
    cfg: Config,
    project: ProjectConfig,
    trial_interval: float = 300.0,
    debounce_sec: float = 2.0,
) -> None:
    """Main daemon loop — blocks until shutdown command or SIGTERM/SIGINT.

    Outputs line-delimited JSON events to stdout.
    """
    if not _acquire_pid_file(project):
        _emit({"event": "error", "message": "Daemon already running for this project"})
        sys.exit(1)

    atexit.register(lambda: _release_pid_file(project))

    session = SyncSession(project, cfg)

    # Wire progress to JSON stream
    session.on_progress = lambda c, t, m: _emit({
        "event": "progress", "current": c, "total": t, "message": m,
    })
    session.on_log = lambda m: _emit({"event": "log", "message": m})

    def _on_stage_changed(stage: SessionStage) -> None:
        _emit({"event": "state_changed", "stage": stage.name})

    session.on_stage_changed = _on_stage_changed

    # File hash cache — avoids re-hashing unchanged files every scan
    from backend.core.cache import FileHashCache
    hash_cache = FileHashCache(Path(session.workspace_path) / ".gitgo")

    # Initial scan + trial check
    session.step_scan(hash_cache=hash_cache)
    session.step_load_commits()
    session.step_check_trial()

    from backend.core.history import HistoryManager
    HistoryManager.set_workspace(str(session.workspace_path))

    # Agent process manager — forks externally via MCP/stdin, reaped here
    apm = AgentProcessManager()

    # v0.45: Session persistence — JSONL + atomic checkpoint
    from backend.core.loop.manager import SessionStore
    session_store = SessionStore(str(session.workspace_path))

    # v0.45: Startup recovery — scan for incomplete sessions
    _incomplete = _scan_incomplete_sessions(session_store, apm)
    if _incomplete:
        _emit({
            "event": "sessions_recovered",
            "count": len(_incomplete),
            "process_ids": _incomplete,
        })

    # Tool executors — thin wrappers that delegate to SyncSession methods
    def _exec_scan(args: dict) -> dict:
        changed = args.get("files", [])
        if changed:
            session.step_scan_files(changed, hash_cache=hash_cache)
        else:
            session.step_scan(hash_cache=hash_cache)
        session.step_load_commits()
        return session.status_dict(semantic=True)

    def _exec_status(args: dict) -> dict:
        return session.status_dict(semantic=args.get("semantic", True))

    def _exec_formalize(args: dict) -> dict:
        indices = args.get("indices")
        message = args.get("message")
        if indices is not None:
            session.selected_workspace = set(indices)
        fc = session.step_create_formal_commit(message=message)
        if fc:
            return {"commit": f"[{fc.prefix}-{fc.number}]", "message": fc.message}
        return {"error": "FORMALIZE_FAILED"}

    # v0.35: Knowledge recall tools
    def _exec_recall_grep(args: dict) -> dict:
        from backend.core.knowledge.recall import recall_grep
        return recall_grep(
            query=args.get("query", ""),
            project=project.name,
            top_k=args.get("top_k", 10),
            agent_context=args.get("agent_context"),
            workspace=str(session.workspace_path),
        )

    def _exec_recall_semantic(args: dict) -> dict:
        from backend.core.knowledge.recall import recall_semantic
        return recall_semantic(
            query=args.get("query", ""),
            project=project.name,
            top_k=args.get("top_k", 10),
            agent_context=args.get("agent_context"),
            workspace=str(session.workspace_path),
        )

    def _exec_recall_rag(args: dict) -> dict:
        from backend.core.knowledge.recall import recall_rag
        return recall_rag(
            query=args.get("query", ""),
            project=project.name,
            agent_context=args.get("agent_context"),
            workspace=str(session.workspace_path),
        )

    # v0.38: AgentTool 定义 —— 替代裸 dict[str, Callable]
    _WRITE_TOOLS = {"formalize", "write", "edit", "push", "sync",
                    "bash", "delete", "rm", "mv", "cp", "mkdir"}

    tool_executors = {
        "scan": AgentTool(
            name="scan",
            description="扫描项目工作区，检测文件变更和 git 状态。当需要了解项目当前状态、检查哪些文件被修改时使用。",
            parameters={"type": "object", "properties": {"files": {"type": "array", "items": {"type": "string"}, "description": "可选，指定要扫描的文件列表"}}, "required": []},
            execute=_exec_scan,
            read_only=True,
        ),
        "status": AgentTool(
            name="status",
            description="获取工作区语义化状态摘要（文件变更、合同漂移、治理信号）。",
            parameters={"type": "object", "properties": {"semantic": {"type": "boolean", "description": "是否返回语义化摘要"}}, "required": []},
            execute=_exec_status,
            read_only=True,
        ),
        "formalize": AgentTool(
            name="formalize",
            description="基于选中的工作区文件创建正式的结构化提交（formal commit）。需要 indices 和 message 参数。",
            parameters={"type": "object", "properties": {"indices": {"type": "array", "items": {"type": "integer"}}, "message": {"type": "string"}}, "required": ["message"]},
            execute=_exec_formalize,
            read_only=False,
            resources=["filesystem:*"],
        ),
        "recall_grep": AgentTool(
            name="recall_grep",
            description="全文搜索知识库中的历史教训（lessons），按关键词匹配。用于查找相似问题的处理经验。",
            parameters={"type": "object", "properties": {"query": {"type": "string", "description": "搜索关键词"}, "top_k": {"type": "integer"}, "agent_context": {"type": "string"}}, "required": ["query"]},
            execute=_exec_recall_grep,
            read_only=True,
        ),
        "recall_semantic": AgentTool(
            name="recall_semantic",
            description="语义搜索知识库中的历史教训，按向量相似度匹配。",
            parameters={"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}, "agent_context": {"type": "string"}}, "required": ["query"]},
            execute=_exec_recall_semantic,
            read_only=True,
        ),
        "recall_rag": AgentTool(
            name="recall_rag",
            description="RAG（检索增强生成）搜索知识库。",
            parameters={"type": "object", "properties": {"query": {"type": "string"}, "agent_context": {"type": "string"}}, "required": ["query"]},
            execute=_exec_recall_rag,
            read_only=True,
        ),
    }

    # ── v0.36: Context Assembler 工具 ──
    def _exec_assemble_context(args: dict) -> dict:
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

    def _exec_assemble_return_context(args: dict) -> dict:
        process_id = args.get("process_id", "")
        process = apm.get(process_id) if apm else None
        if not process:
            return {"error": "process not found"}

        return _build_return_context(process)

    # ── v0.36: decompose_task executor ──
    def _exec_decompose_task(args: dict) -> dict:
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

    tool_executors.update({
        "assemble_context": AgentTool(
            name="assemble_context",
            description="汇编上下文：从 policy signals + recall + dependency graph 三层收集相关上下文。需要 task 和 files 参数。",
            parameters={"type": "object", "properties": {"task": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}}, "required": ["task"]},
            execute=_exec_assemble_context,
            read_only=True,
        ),
        "assemble_return_context": AgentTool(
            name="assemble_return_context",
            description="构建 B Agent 返回给 A Agent 的上下文转录。需要 process_id 参数。",
            parameters={"type": "object", "properties": {"process_id": {"type": "string"}}, "required": ["process_id"]},
            execute=_exec_assemble_return_context,
            read_only=True,
        ),
        "decompose_task": AgentTool(
            name="decompose_task",
            description=(
                "将当前复杂任务分析并建议拆分为多个子任务。"
                "分解成本高（每个子 slot 消耗独立 max_steps 预算），只在必要时使用。"
                "仅当任务涉及多个文件且有交叉依赖时考虑。返回建议的子任务列表。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "当前任务描述"},
                    "files": {"type": "array", "items": {"type": "string"}, "description": "涉及的文件列表"},
                    "reason": {"type": "string", "description": "为什么建议拆分"},
                },
                "required": ["task", "files"],
            },
            execute=_exec_decompose_task,
            read_only=True,
        ),
    })

    # ── v0.45: 差异化后端工具 ──
    tool_executors.update({
        "contract_detect_drift": AgentTool(
            name="contract_detect_drift",
            description="检测本轮文件变更与项目合约的偏差。返回告警列表（feature_deleted, signature_changed 等）。当需要验证变更是否符合合约时使用。",
            parameters={
                "type": "object",
                "properties": {
                    "workspace_path": {"type": "string", "description": "工作区路径"},
                    "changed_files": {"type": "array", "items": {"type": "string"}, "description": "变更文件列表"},
                    "contract_path": {"type": "string", "description": "contract.yaml 路径，可选"},
                },
                "required": ["workspace_path"],
            },
            execute=contract_detect_drift,
            read_only=True,
        ),
        "contract_get_impact": AgentTool(
            name="contract_get_impact",
            description="查询文件的影响面：哪些文件依赖它（dependents），哪些函数调用了它（callers）。修改文件前评估爆炸半径时使用。",
            parameters={
                "type": "object",
                "properties": {
                    "workspace_path": {"type": "string", "description": "工作区路径"},
                    "file_path": {"type": "string", "description": "目标文件路径（相对于 workspace）"},
                    "func_name": {"type": "string", "description": "可选，指定函数名以精确查询调用者"},
                },
                "required": ["workspace_path", "file_path"],
            },
            execute=contract_get_impact,
            read_only=True,
        ),
        "contract_get_changed_symbols": AgentTool(
            name="contract_get_changed_symbols",
            description="对比文件两个版本的 AST，返回变更的函数/类名列表。用于精确判断代码变更的符号级影响。",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                    "old_content": {"type": "string", "description": "旧版本内容，可选"},
                    "new_content": {"type": "string", "description": "新版本内容，可选"},
                },
                "required": ["file_path"],
            },
            execute=contract_get_changed_symbols,
            read_only=True,
        ),
        "lesson_search": AgentTool(
            name="lesson_search",
            description="在知识库中搜索历史经验教训（lessons）。同时搜索抽象层和实例层。用于查找相似问题的处理经验、避免重复错误。",
            parameters={
                "type": "object",
                "properties": {
                    "workspace_path": {"type": "string", "description": "工作区路径"},
                    "query": {"type": "string", "description": "搜索关键词"},
                    "project_name": {"type": "string", "description": "项目名，可选"},
                    "tech_stack": {"type": "string", "description": "技术栈标签，可选"},
                },
                "required": ["workspace_path", "query"],
            },
            execute=lesson_search,
            read_only=True,
        ),
        "lesson_discard": AgentTool(
            name="lesson_discard",
            description="删除一条经验教训（从 pending 或 instance 中移除）。用于清理过时或错误的 lesson。",
            parameters={
                "type": "object",
                "properties": {
                    "workspace_path": {"type": "string", "description": "工作区路径"},
                    "lesson_id": {"type": "string", "description": "要删除的 lesson ID"},
                    "project_name": {"type": "string", "description": "项目名，可选"},
                },
                "required": ["workspace_path", "lesson_id"],
            },
            execute=lesson_discard,
            read_only=False,
            resources=["filesystem:*"],
        ),
        "lesson_verify": AgentTool(
            name="lesson_verify",
            description="确认一条经验教训（从 pending 提升为正式，或增加 verified_count）。需要 Ring 0 权限。",
            parameters={
                "type": "object",
                "properties": {
                    "workspace_path": {"type": "string", "description": "工作区路径"},
                    "lesson_id": {"type": "string", "description": "要确认的 lesson ID"},
                    "project_name": {"type": "string", "description": "项目名，可选"},
                },
                "required": ["workspace_path", "lesson_id"],
            },
            execute=lesson_verify,
            read_only=False,
            resources=["filesystem:*"],
        ),
        "lesson_harvest": AgentTool(
            name="lesson_harvest",
            description="从 git log、CLAUDE.md、scan history、governance signals 四个数据源收割新经验教训。操作较重（扫描 4 源），需要 Ring 0 权限。",
            parameters={
                "type": "object",
                "properties": {
                    "workspace_path": {"type": "string", "description": "工作区路径"},
                    "project_name": {"type": "string", "description": "项目名称"},
                    "tech_stack": {"type": "string", "description": "技术栈标签，可选"},
                },
                "required": ["workspace_path", "project_name"],
            },
            execute=lesson_harvest,
            read_only=False,
            timeout=120.0,
            resources=["filesystem:*"],
        ),
        "privacy_scan": AgentTool(
            name="privacy_scan",
            description="扫描变更文件的隐私风险（敏感信息泄露、AI 痕迹、密钥硬编码等）。push 前或代码审查时使用。",
            parameters={
                "type": "object",
                "properties": {
                    "workspace_path": {"type": "string", "description": "工作区路径"},
                    "file_list": {"type": "array", "items": {"type": "string"}, "description": "要扫描的文件列表"},
                    "level": {"type": "integer", "description": "扫描级别 1-3，默认 2"},
                    "deep_scan": {"type": "boolean", "description": "是否深度扫描，默认 false"},
                },
                "required": ["workspace_path"],
            },
            execute=privacy_scan,
            read_only=True,
        ),
        "memory_snapshot": AgentTool(
            name="memory_snapshot",
            description="将工作区工具记忆（CLAUDE.md 等）快照到 backup 目录，并列出所有可用快照。用于备份当前记忆状态。",
            parameters={
                "type": "object",
                "properties": {
                    "workspace_path": {"type": "string", "description": "工作区路径"},
                    "backup_path": {"type": "string", "description": "备份目标路径"},
                },
                "required": ["workspace_path", "backup_path"],
            },
            execute=memory_snapshot,
            read_only=False,
            resources=["filesystem:*"],
        ),
        "memory_restore": AgentTool(
            name="memory_restore",
            description="从 backup 目录的快照恢复工具记忆到工作区。snapshot_timestamp 为空时使用最新快照。",
            parameters={
                "type": "object",
                "properties": {
                    "backup_path": {"type": "string", "description": "备份源路径"},
                    "workspace_path": {"type": "string", "description": "工作区路径"},
                    "snapshot_timestamp": {"type": "string", "description": "快照时间戳，可选，默认最新"},
                },
                "required": ["backup_path", "workspace_path"],
            },
            execute=memory_restore,
            read_only=False,
            resources=["filesystem:*"],
        ),
        "lesson_promote": AgentTool(
            name="lesson_promote",
            description="将一条实例层经验教训提升为抽象层（跨项目复用）。需要 Ring 0 权限。",
            parameters={
                "type": "object",
                "properties": {
                    "workspace_path": {"type": "string", "description": "工作区路径"},
                    "lesson_id": {"type": "string", "description": "要提升的 lesson ID"},
                    "project_name": {"type": "string", "description": "项目名，可选"},
                    "tech_stack": {"type": "string", "description": "技术栈标签，可选"},
                },
                "required": ["workspace_path", "lesson_id"],
            },
            execute=lesson_promote,
            read_only=False,
            resources=["filesystem:*"],
        ),
        "lesson_list": AgentTool(
            name="lesson_list",
            description="列出所有经验教训（抽象层 + 实例层 + 待确认）。用于查看知识库全貌、盘点现有 lessons。",
            parameters={
                "type": "object",
                "properties": {
                    "workspace_path": {"type": "string", "description": "工作区路径"},
                    "project_name": {"type": "string", "description": "项目名，可选（为空时返回全部抽象层 lessons）"},
                },
                "required": ["workspace_path"],
            },
            execute=lesson_list,
            read_only=True,
        ),
    })

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

    from backend.core.loop.gate import RingGate
    from backend.adapters.local_git_runner import LocalGitRunner
    _git_runner = LocalGitRunner(session.workspace_path)
    dispatcher = ToolDispatcher(
        RingGate(), tool_executors,
        history_writer=HistoryManager.add_operation,
        git_runner=_git_runner,
    )

    # Event queue — must be created before daemon_ctx references it
    evq: queue.Queue = queue.Queue()

    # Context bundle for _handle_command — avoids growing its parameter list
    daemon_ctx = {
        "apm": apm,
        "dispatcher": dispatcher,
        "evq": evq,
        "hash_cache": hash_cache,
        "llm": None,  # set via config or stdin command
        "session_store": session_store,  # v0.45: session persistence
    }

    _emit({
        "event": "daemon_started",
        "project": project.name,
        "pid": os.getpid(),
        "status": session.status_dict(semantic=True),
    })

    # Background threads
    exclude = list(project.force_exclude) if project.force_exclude else []
    watcher = WorkspaceWatcher(
        workspace_path=session.workspace_path,
        exclude_patterns=exclude,
        on_dirty=lambda changed=None: evq.put({"event": "workspace_dirty", "changed_files": changed or []}),
        debounce_sec=debounce_sec,
    )

    poller = TrialPoller(evq, interval_sec=trial_interval)
    reader = CommandReader(evq)

    watcher_thread = threading.Thread(target=watcher.start, daemon=True, name="watcher")
    poller_thread = threading.Thread(target=poller.run, daemon=True, name="poller")
    reader_thread = threading.Thread(target=reader.run, daemon=True, name="reader")

    # Graceful shutdown handler
    _shutdown_flag = threading.Event()

    def _handle_shutdown():
        if _shutdown_flag.is_set():
            return
        _shutdown_flag.set()
        evq.put({"event": "shutdown"})

    signal.signal(signal.SIGTERM, lambda *_: _handle_shutdown())
    signal.signal(signal.SIGINT, lambda *_: _handle_shutdown())

    watcher_thread.start()
    poller_thread.start()
    reader_thread.start()

    try:
        while not _shutdown_flag.is_set():
            try:
                ev = evq.get(timeout=1.0)
            except queue.Empty:
                continue

            event_type = ev.get("event", "")

            # Reap orphaned agent processes each cycle
            apm.reap()

            # ── v0.35: Pending Digest 定时检查（独立于 harvest 事件）──
            now_ts = time.time()
            last_digest = getattr(run_daemon, '_last_pending_digest', 0.0)
            if now_ts - last_digest >= 3600:  # 每小时
                run_daemon._last_pending_digest = now_ts
                try:
                    from backend.core.knowledge.lesson import LessonManager as _LM
                    from backend.core.knowledge.harvest import (
                        auto_discard_invalid, auto_verify_high_confidence,
                    )
                    ws = Path(session.workspace_path)
                    pending_n = _LM.pending_count(ws, project.name)
                    if pending_n >= 50:
                        n = auto_discard_invalid(ws, project.name)
                        if n:
                            _emit({"event": "lessons_discarded",
                                   "count": n, "reason": "auto_invalid"})
                    if pending_n >= 100:
                        n = auto_verify_high_confidence(ws, project.name)
                        if n:
                            _emit({"event": "lessons_verified",
                                   "count": n, "reason": "auto_verify"})
                    if pending_n >= 200:
                        _emit_v2({"event": "pending_overflow",
                               "count": pending_n,
                               "message": "Pending 已满，阻塞新 harvest。请 verify 或 discard。"},
                              priority="immediate")
                except Exception:
                    pass

            if event_type == "workspace_dirty":
                # ── Debounce ──
                now = time.time()
                last_check = getattr(run_daemon, '_last_policy_check', 0.0)
                if now - last_check < debounce_sec:
                    continue
                run_daemon._last_policy_check = now
                _emit({"event": "workspace_dirty", "project": project.name})
                _emit({"event": "operation_started", "op": "scan"})
                # 文件变更 → drift_cache 失效（内存 + 持久化）
                if "drift_cache" in daemon_ctx:
                    daemon_ctx["drift_cache"]["dirty"] = True
                HistoryManager.add_operation(
                    project.name, "drift_cache", "success",
                    {"alerts": [], "dirty": True},
                    correlation_id=session._correlation_id,
                )
                try:
                    # Invalidate cache entries for changed files
                    changed = ev.get("changed_files", [])
                    for f in changed:
                        hash_cache.invalidate(f)
                    # Incremental scan if watchdog provides changed files
                    if changed:
                        session.step_scan_files(changed, hash_cache=hash_cache)
                    else:
                        session.step_scan(hash_cache=hash_cache)
                    session.step_load_commits()

                    # ── Policy Engine 三步检查 ──
                    from backend.core.history import HistoryManager

                    from backend.core.fact import derive_facts
                    derive_facts(project.name)

                    engine = PolicyEngine()
                    results = engine.run(session, project)
                    gov_warnings = sum(len(v) for v in results.values())

                    for l in results.get("lesson_triggers", []):
                        _emit_v2({"event": "lesson_matched", "lesson_id": l["lesson_id"],
                               "severity": l["severity"], "rule": l["rule"]},
                              priority="immediate")
                    for d in results.get("contract_drift", []):
                        _emit_v2({"event": "governance_drift", "rule": d.get("rule", "contract"),
                               "level": "warning", "message": d.get("message", "")},
                              priority="immediate")
                        HistoryManager.add_operation(
                            project.name, "governance_drift", "warning",
                            {"rule": d.get("rule", "contract"), "message": d.get("message", "")},
                            correlation_id=session._correlation_id)
                    for w in results.get("identity_integrity", []):
                        _emit_v2({"event": "governance_drift", "rule": w.get("rule", "integrity"),
                               "level": w.get("level", "warning"), "message": w.get("message", "")},
                              priority="immediate")
                        HistoryManager.add_operation(
                            project.name, "governance_drift", w.get("level", "warning"),
                            {"rule": w.get("rule", "integrity"), "message": w.get("message", "")},
                            correlation_id=session._correlation_id)

                    HistoryManager.add_operation(
                        project.name, "policy_check_result",
                        "warning" if gov_warnings else "success",
                        results, correlation_id=session._correlation_id)
                    msg = build_policy_message(results)
                    if msg:
                        _emit_v2({"event": "policy_results",
                               "governance_warnings": gov_warnings, "message": msg},
                              priority="immediate")

                    # ── Signal Normalization (四源) + Drift Cache ──
                    from backend.core.loop.signal_normalizer import SignalNormalizer
                    from backend.core.knowledge.lesson import LessonManager

                    normalizer = SignalNormalizer()
                    ws_path = str(session.workspace_path)
                    lessons = (
                        LessonManager.load_instance(ws_path, project.name)
                        + LessonManager.load_pending(ws_path, project.name)
                    )
                    project_entries = [
                        e for e in HistoryManager.load()
                        if e.project_name == project.name
                    ]
                    rejections = [
                        e for e in project_entries if e.operation == "rejection"
                    ][-10:]
                    new_facts = [
                        e for e in project_entries if e.operation == "fact_derived"
                    ][-10:]
                    signals = normalizer.normalize(
                        policy_results=results,
                        lessons=lessons,
                        rejections=rejections,
                        facts=[],  # Fact objects parsed from entries below
                    )
                    daemon_ctx["governance_signals"] = signals

                    # v0.43: G1 —— 注入增量治理信号到正在运行的 B 进程
                    if signals and apm is not None:
                        for pid, running_proc in apm._processes.items():
                            if running_proc.status.value != "running":
                                continue
                            if running_proc.session is None:
                                continue
                            # 获取该 B fork 时的旧 signals
                            old_ctx = running_proc.context_snapshot or {}
                            old_signals = old_ctx.get("signals", [])
                            old_ids = {getattr(s, 'signal_id', '') for s in old_signals}
                            # 找出 B 尚未见过的增量信号
                            new_for_b = [s for s in signals
                                         if s.signal_id not in old_ids]
                            if new_for_b:
                                # 注入为隐用户输入（B 无法区分来自用户还是系统）
                                brief_parts = []
                                for s in new_for_b[:5]:  # 最多 5 条，避免上下文污染
                                    if s.severity.value in ("critical", "high"):
                                        brief_parts.append(
                                            f"[{s.severity.value.upper()}] {s.suggestion or s.rule}"
                                        )
                                if brief_parts:
                                    running_proc.session.append_user(
                                        "[治理更新] 你最近的操作触发了新的治理信号。"
                                        "请检查并修正：\n" + "\n".join(brief_parts),
                                        message_type="governance_nudge",
                                        referenced_files=[
                                            f for s in new_for_b[:5]
                                            for f in (s.target_files or [])
                                        ],
                                    )

                    # Drift cache: PolicyEngine 产出 → Gate 可直接复用
                    # 写入 HistoryManager 使 Gate 可通过历史记录读取（系统维护，非 LLM 维护）
                    drift_alerts = results.get("contract_drift", [])
                    daemon_ctx["drift_cache"] = {
                        "alerts": drift_alerts,
                        "dirty": False,
                    }
                    import json as _json
                    HistoryManager.add_operation(
                        project.name, "drift_cache", "success",
                        {"alerts": [
                            {k: v for k, v in a.items() if k != "message"}
                            for a in drift_alerts
                        ], "dirty": False},
                        correlation_id=session._correlation_id,
                    )

                    if signals:
                        block_count = sum(1 for s in signals if s.category.value == "block")
                        _emit_v2({
                            "event": "governance_signals",
                            "total": len(signals),
                            "block_count": block_count,
                            "sources": list(set(s.source for s in signals)),
                        }, priority="immediate")

                    # ── v0.35: Harvest 信号捕获 ──
                    from backend.core.knowledge.harvest import (
                        capture_signal, should_trigger_harvest,
                        mark_harvest_triggered, harvest_llm_summary,
                    )
                    from backend.core.knowledge.lesson import LessonManager as _LM

                    # 捕获 lesson trigger 信号
                    for lt in results.get("lesson_triggers", []):
                        capture_signal("lesson_trigger", {
                            "trigger": lt.get("file", ""),
                            "rule": lt.get("rule", ""),
                            "severity": lt.get("severity", "medium"),
                            "detail": lt,
                        }, project.name)

                    # 捕获 contract drift 信号
                    for drift in results.get("contract_drift", []):
                        capture_signal("contract_drift", {
                            "trigger": drift.get("file", ""),
                            "rule": drift.get("rule", "contract drift"),
                            "detail": drift,
                        }, project.name)

                    # 检查是否触发 LLM 总结
                    for sig_type in ("lesson_trigger", "contract_drift"):
                        if should_trigger_harvest(sig_type, project.name):
                            mark_harvest_triggered(sig_type)
                            signals_batch = get_unprocessed_signals(
                                project.name, sig_type,
                            )
                            if signals_batch and daemon_ctx.get("llm"):
                                try:
                                    new_lessons = harvest_llm_summary(
                                        signals_batch, daemon_ctx["llm"],
                                        str(session.workspace_path), project.name,
                                    )
                                    ws = Path(session.workspace_path)
                                    for lesson in new_lessons:
                                        _LM.save_pending(ws, lesson)
                                    if new_lessons:
                                        _emit({
                                            "event": "lessons_harvested",
                                            "count": len(new_lessons),
                                            "signal_type": sig_type,
                                        })
                                except Exception:
                                    pass

                    _emit({
                        "event": "operation_complete", "op": "scan",
                        "status": "success",
                        "status_dict": session.status_dict(semantic=True),
                        "governance_warnings": gov_warnings,
                    })
                except Exception as exc:
                    _emit({"event": "operation_complete", "op": "scan",
                           "status": "failed", "error": str(exc)})

            elif event_type == "trial_check":
                _emit({"event": "operation_started", "op": "trial_check"})
                try:
                    incoming = session.step_check_trial()
                    _emit({
                        "event": "operation_complete", "op": "trial_check",
                        "status": "success",
                        "new_count": len(incoming),
                        "status_dict": session.status_dict(semantic=True),
                    })
                except Exception as exc:
                    _emit({"event": "operation_complete", "op": "trial_check",
                           "status": "failed", "error": str(exc)})

            elif event_type == "stdin_command":
                _handle_command(ev["cmd"], session, project, daemon_ctx,
                                on_shutdown=_handle_shutdown)

            elif event_type == "llm_response":
                # Forward LLM response from background thread directly to stdout
                _emit(ev)

            # ── v0.44: 流式事件 + agent_complete 修复 ──
            elif event_type in ("text_delta", "toolcall_start",
                                "toolcall_delta", "tool_progress",
                                "stream_recovery"):
                _emit_v2(ev)  # priority="normal" — 微批
            elif event_type == "agent_complete":
                _emit_v2(ev, priority="immediate")  # 修复：之前无分支→静默丢弃
                # v0.45: cleanup session files on normal completion
                result = ev.get("result", {})
                if result.get("status") == "completed":
                    pid = ev.get("process_id", "")
                    if pid and session_store:
                        session_store.delete_session(pid)

            elif event_type == "shutdown":
                _handle_shutdown()

            elif event_type == "error":
                _emit(ev)

            # v0.44: 每轮末尾 flush 微批 buffer，防止空闲时事件滞留
            _flush_emit_buffer()

    finally:
        watcher.stop()
        poller.stop()
        reader.stop()
        hash_cache.flush()
        _release_pid_file(project)
        # v0.45: cleanup temp resources on shutdown
        _cleanup_resources(str(session.workspace_path))
        _emit({"event": "daemon_stopped", "project": project.name})


def _handle_command(cmd: dict, session: SyncSession, project: ProjectConfig,
                    daemon_ctx: dict = None,
                    on_shutdown: callable = None) -> None:
    """Dispatch a stdin command to the appropriate step method."""
    cmd_name = cmd.get("cmd", "")
    apm = daemon_ctx.get("apm") if daemon_ctx else None
    dispatcher = daemon_ctx.get("dispatcher") if daemon_ctx else None
    evq = daemon_ctx.get("evq") if daemon_ctx else None
    llm_provider = daemon_ctx.get("llm") if daemon_ctx else None

    # v0.44: 注入 request_id 到所有 command_result 事件，使 JS sendCommand 能匹配响应
    request_id = cmd.get("request_id", "")
    _emit_global = _emit

    def _emit(ev: dict) -> None:
        if ev.get("event") == "command_result" and request_id:
            ev = dict(ev)
            ev["request_id"] = request_id
        _emit_global(ev)

    if cmd_name == "shutdown":
        _emit({"event": "shutdown_ack", "message": "Shutting down"})
        if on_shutdown:
            on_shutdown()
        return

    if cmd_name == "fork_agent":
        if apm is None:
            _emit({"event": "command_result", "cmd": "fork_agent",
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
            _emit({"event": "command_result", "cmd": "fork_agent",
                   "result": {"process_id": proc.process_id, "role": role,
                              "ring": ring.value}})
        except ValueError as e:
            _emit({"event": "command_result", "cmd": "fork_agent",
                   "error": str(e)})
        return

    if cmd_name == "dispatch_tool":
        if dispatcher is None or apm is None:
            _emit({"event": "command_result", "cmd": "dispatch_tool",
                   "error": "ToolDispatcher or AgentProcessManager not available"})
            return
        process_id = cmd.get("process_id", "")
        tool_name = cmd.get("tool", "")
        tool_args = cmd.get("args", {})
        process = apm.get(process_id)
        if process is None:
            _emit({"event": "command_result", "cmd": "dispatch_tool",
                   "error": f"Process not found: {process_id}"})
            return
        result = dispatcher.dispatch(process, tool_name, tool_args)
        _emit({"event": "command_result", "cmd": "dispatch_tool",
               "result": {
                   "allowed": result.allowed,
                   "data": result.data,
                   "error": result.error,
                   "duration_ms": result.duration_ms,
                   "steps_remaining": result.steps_remaining,
                   "process_status": process.status.value,
               }})
        return

    if cmd_name == "llm_configure":
        base_url = cmd.get("base_url", "")
        api_key = cmd.get("api_key", "")
        model_id = cmd.get("model_id", "")
        if not base_url or not api_key or not model_id:
            _emit({"event": "command_result", "cmd": "llm_configure",
                   "error": "base_url, api_key, model_id are all required"})
            return
        from backend.core.loop.llm import LLMProvider
        daemon_ctx["llm"] = LLMProvider(base_url, api_key, model_id)
        _emit({"event": "command_result", "cmd": "llm_configure",
               "result": {"model": model_id, "base_url": base_url}})
        return

    if cmd_name == "llm_call":
        if llm_provider is None:
            _emit({"event": "command_result", "cmd": "llm_call",
                   "error": "LLM not configured. Send llm_configure first."})
            return
        if evq is None:
            _emit({"event": "command_result", "cmd": "llm_call",
                   "error": "Event queue not available"})
            return
        messages = cmd.get("messages", [])
        process_id = cmd.get("process_id", "")
        if not messages:
            _emit({"event": "command_result", "cmd": "llm_call",
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
        _emit({"event": "command_result", "cmd": "llm_call",
               "result": {"status": "pending", "process_id": process_id}})
        return

    if cmd_name == "status":
        raw = cmd.get("raw", False)
        semantic_only = cmd.get("semantic_only", False)
        if semantic_only:
            d = session.status_dict(semantic=True)
            _emit({"event": "command_result", "cmd": "status",
                   "result": d.get("semantic", {})})
        else:
            _emit({"event": "command_result", "cmd": "status",
                   "result": session.status_dict(semantic=not raw)})

    elif cmd_name == "scan":
        _emit({"event": "operation_started", "op": "scan"})
        try:
            session.step_scan(hash_cache=daemon_ctx.get("hash_cache"))
            session.step_load_commits()
            _emit({"event": "operation_complete", "op": "scan",
                   "status": "success",
                   "result": session.status_dict(semantic=True)})
        except Exception as exc:
            _emit({"event": "operation_complete", "op": "scan",
                   "status": "failed", "error": str(exc)})

    elif cmd_name == "formalize":
        indices = cmd.get("indices")
        message = cmd.get("message")
        session.step_load_commits()
        if indices is not None:
            session.selected_workspace = set(indices)
        fc = session.step_create_formal_commit(message=message)
        if fc:
            _emit({"event": "command_result", "cmd": "formalize",
                   "result": {"commit": f"[{fc.prefix}-{fc.number}]",
                              "message": fc.message}})
        else:
            _emit({"event": "command_result", "cmd": "formalize",
                   "result": None, "error": "create_formal_commit failed"})

    elif cmd_name == "sync":
        _emit({"event": "operation_started", "op": "sync"})
        ok = session.step_sync()
        _emit({"event": "operation_complete", "op": "sync",
               "status": "success" if ok else "failed"})

    elif cmd_name == "push":
        _emit({"event": "operation_started", "op": "push"})
        ok, _ = session.step_push()
        _emit({"event": "operation_complete", "op": "push",
               "status": "success" if ok else "failed"})

    elif cmd_name == "trial":
        action = cmd.get("action", "list")
        if action == "list":
            result = [
                {"index": i, "hash": c.hash, "message": c.message,
                 "author": c.author, "date": c.date,
                 "triage": c.triage.value}
                for i, c in enumerate(session.incoming_changes)
            ]
            _emit({"event": "command_result", "cmd": "trial",
                   "result": result})
        elif action in ("accept", "promote", "discard"):
            idx = cmd.get("index")
            if idx is None:
                _emit({"event": "command_result", "cmd": "trial",
                       "error": "index required"})
                return
            ok = session.step_triage_incoming(idx, action)
            _emit({"event": "command_result", "cmd": "trial",
                   "result": "ok" if ok else "failed"})

    elif cmd_name == "session":
        action = cmd.get("action", "status")
        if action == "save":
            path = session.save_session()
            _emit({"event": "command_result", "cmd": "session",
                   "result": {"saved": str(path)}})
        elif action == "status":
            _emit({"event": "command_result", "cmd": "session",
                   "result": session.status_dict(semantic=True)})
        elif action == "resume":
            loaded = SyncSession.load_session(project, ConfigManager.load())
            _emit({"event": "command_result", "cmd": "session",
                   "result": {"resumed": loaded is not None}})

    elif cmd_name == "round_complete":
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
            _emit({
                "event": "recycle_check",
                "total_lessons": len(all_lessons),
                "sticky_count": len(sticky_ids),
                "hot_lesson_ids": list(sticky_ids)[:5],
            })
        except Exception:
            pass

        _emit({"event": "command_result", "cmd": "round_complete",
               "result": {"snapshot": changed is not None,
                          "files": len(changed) if changed else 0}})

    elif cmd_name == "reject":
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
        _emit({"event": "command_result", "cmd": "reject",
               "result": {"rejection_count": len(rejections)}})

    elif cmd_name == "loop_status":
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
        _emit({"event": "command_result", "cmd": "loop_status",
               "result": {
                   "daemon_online": True,
                   "processes": processes,
                   "recent_tool_executed": recent_tools,
                   "providers": [],
               }})

    elif cmd_name == "task":
        # ── 原生 Task 命令 —— Agent 编排的单一入口 ──
        # 整合了 MCP 层之前的 _resolve_llm_config / _ensure_agent / _chat_via_daemon 逻辑。
        # MCP 工具变为薄适配器：只构建上下文 + 调用此命令。
        action = cmd.get("action", "chat")

        if action == "fork":
            # 仅 fork Agent，不执行
            if apm is None:
                _emit({"event": "command_result", "cmd": "task",
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
                _emit({"event": "command_result", "cmd": "task",
                       "result": {"process_id": proc.process_id, "role": role,
                                  "ring_level": ring.value}})
            except ValueError as e:
                _emit({"event": "command_result", "cmd": "task",
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
            _emit({"event": "command_result", "cmd": "task",
                   "result": {"daemon_online": True,
                              "processes": processes,
                              "recent_tool_executed": recent_tools,
                              "providers": []}})
            return

        if action == "kill":
            if apm is None:
                _emit({"event": "command_result", "cmd": "task",
                       "error": "AgentProcessManager not available"})
                return
            process_id = cmd.get("process_id", "")
            apm.kill(process_id)
            _emit({"event": "command_result", "cmd": "task",
                   "result": {"killed": process_id}})
            return

        if action == "chat":
            # ── chat: 完整 Agent 编排 ──
            if apm is None or dispatcher is None or evq is None:
                _emit({"event": "command_result", "cmd": "task",
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
                _emit({"event": "command_result", "cmd": "task",
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
                    _emit({"event": "command_result", "cmd": "task",
                           "result": {
                               "status": scheduler_result.get("status", ""),
                               "total_slots": scheduler_result.get("total_slots", 0),
                               "completed_slots": scheduler_result.get("completed_slots", 0),
                           }})
                    return
                except Exception as e:
                    _emit({"event": "scheduler_fallback", "reason": str(e)})
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
                    _emit({"event": "command_result", "cmd": "task",
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
            _emit({"event": "command_result", "cmd": "task",
                   "result": {"status": "pending",
                              "process_id": process.process_id}})
            return

        _emit({"event": "command_result", "cmd": "task",
               "error": f"Unknown task action: {action}"})

    else:
        _emit({"event": "command_result", "cmd": cmd_name,
               "error": f"Unknown command: {cmd_name}"})
