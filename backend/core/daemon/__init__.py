"""Persistent Daemon Core — long-running process with file watch + trial poll + stdin commands.

Architecture:
    watcher (Thread-1) ──┐
    poller  (Thread-2) ──┼── event_queue ──► Main Loop (主线程) ──► stdout (JSON)
    reader  (Thread-3) ──┘

The main loop owns the SyncSession and dispatches events to step methods.
"""

from __future__ import annotations

import atexit
import os
import queue
import signal
import sys
import threading
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from backend.core.config import Config, ProjectConfig
from backend.core.sync_session import SyncSession, SessionStage
from backend.core.daemon.watcher import WorkspaceWatcher
from backend.core.daemon.poller import TrialPoller
from backend.core.daemon.commands import CommandReader
from backend.core.daemon.emit import _emit, _emit_v2, _flush_emit_buffer
from backend.core.daemon.pidfile import _pid_file_path, _acquire_pid_file, _release_pid_file
from backend.core.daemon.persist import _scan_incomplete_sessions
from backend.core.daemon.cleanup import _cleanup_resources
from backend.core.daemon.executors import (
    _exec_scan, _exec_status, _exec_formalize,
    _exec_recall_grep, _exec_recall_semantic, _exec_recall_rag,
    _exec_assemble_context, _exec_assemble_return_context,
    _exec_decompose_task,
)
from backend.core.daemon.dispatch import _handle_command


# ── Policy Engine ─────────────────────────────────────────

from backend.core.policy import PolicyEngine, build_policy_message
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.agent_tool import AgentTool, ToolEffect, CancellationMode
from backend.core.loop.test_manifest import run_registered_test
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
from backend.core.tools.catalog import build_workspace_tools
from backend.core.storage import StorageRuntime, assess_repository_scope


def _is_traced_runtime_event(event: dict) -> bool:
    """Recognize redacted Agent events without a per-event allowlist."""
    return (
        isinstance(event, dict)
        and bool(event.get("event"))
        and bool(event.get("trace_id"))
        and isinstance(event.get("seq"), int)
        and isinstance(event.get("schema_version"), int)
    )


def _background_llm_provider(daemon_ctx: dict):
    """Return the provider shared by foreground and event-driven work.

    A project daemon may observe a workspace change before its first chat turn,
    or while a different Native Host instance owns the foreground request.
    Requiring ``llm_configure``/task admission to have populated the in-memory
    slot makes automatic knowledge harvest silently dependent on that ordering.
    Resolve the encrypted active provider lazily at the safe event boundary and
    cache only the runtime client; provider switches already retire idle daemons.
    """
    current = daemon_ctx.get("llm")
    if current is not None:
        return current
    from backend.core.llm_config import LLMConfigManager
    from backend.core.loop.llm import LLMProvider as RuntimeLLMProvider

    configured = LLMConfigManager.get_active()
    if configured is None:
        return None
    current = RuntimeLLMProvider(
        configured.base_url,
        configured.api_key,
        configured.model_id,
        protocol=configured.protocol,
        capabilities=configured.runtime_capabilities(),
    )
    daemon_ctx["llm"] = current
    return current


def _publish_governance_snapshot(
    session, project, daemon_ctx: dict, apm,
    *, policy_results: dict | None = None,
) -> dict:
    """Refresh the one authoritative governance projection at a safe boundary."""
    from backend.core.loop.context_builder import (
        build_governance_context, build_policy_source_snapshot,
    )
    from backend.core.loop.governance_projection import GovernanceProjection

    fresh = build_governance_context(
        project.name,
        str(session.workspace_path),
        current_policy_results=policy_results,
        source_snapshot=build_policy_source_snapshot(session),
    )
    signals = fresh["signals"]
    daemon_ctx["governance_signals"] = signals
    daemon_ctx["governance_context"] = fresh
    if apm is not None:
        for process in list(apm._processes.values()):
            if process.status.value not in {
                "running", "waiting", "awaiting_user", "recovering",
                "resume_available",
            } or process.session is None:
                continue
            GovernanceProjection.publish(
                process,
                signals,
                base_brief=fresh["base_brief"],
                candidates=fresh.get("evidence_candidates"),
                evidence_sources=fresh.get("evidence_sources"),
                lessons=fresh.get("lessons"),
            )
    return fresh


def _start_background_harvest(
    daemon_ctx: dict,
    *,
    event_queue,
    provider,
    workspace_path: str,
    project_name: str,
    signal_type: str,
    thread_factory=threading.Thread,
) -> bool:
    """Lease and execute one harvest without blocking the daemon command loop.

    SQLite owns the durable lease and retry state. The worker performs only the
    slow provider call and persistence; it returns a small event so the daemon
    main loop remains the sole owner of process/governance projection updates
    and user-visible emissions.
    """
    if daemon_ctx.get("knowledge_harvest_inflight"):
        return False

    from backend.core.knowledge.harvest import (
        complete_harvest, fail_harvest, harvest_llm_summary,
        lease_harvest_signals, mark_harvest_triggered,
    )

    batch = lease_harvest_signals(project_name)
    if not batch:
        return False

    harvest_id = "harvest_" + uuid.uuid4().hex
    daemon_ctx["knowledge_harvest_inflight"] = harvest_id
    mark_harvest_triggered(project_name)

    def _worker() -> None:
        signal_ids = [item["signal_id"] for item in batch]
        event = {
            "event": "knowledge_harvest_result",
            "harvest_id": harvest_id,
            "time": datetime.now(timezone.utc).isoformat(),
            "signal_type": signal_type,
        }
        try:
            from backend.core.knowledge.lesson import LessonManager as _LM

            lessons = harvest_llm_summary(
                batch,
                provider,
                workspace_path,
                project_name,
                raise_on_error=True,
            )
            workspace = Path(workspace_path)
            for lesson in lessons:
                _LM.save_pending(workspace, lesson)
            complete_harvest(
                project_name,
                signal_ids,
                [lesson.id for lesson in lessons],
            )
            event.update({"status": "success", "count": len(lessons)})
        except Exception as exc:
            fail_harvest(project_name, signal_ids, str(exc))
            event.update({
                "status": "failed",
                "reason": "harvest_failed",
                "error": str(exc),
                "retryable": True,
            })
        finally:
            event_queue.put(event)

    try:
        worker = thread_factory(
            target=_worker,
            name=f"gitgo-harvest-{project_name}",
            daemon=True,
        )
        worker.start()
    except Exception as exc:
        daemon_ctx.pop("knowledge_harvest_inflight", None)
        signal_ids = [item["signal_id"] for item in batch]
        fail_harvest(project_name, signal_ids, str(exc))
        event_queue.put({
            "event": "knowledge_harvest_result",
            "harvest_id": harvest_id,
            "time": datetime.now(timezone.utc).isoformat(),
            "signal_type": signal_type,
            "status": "failed",
            "reason": "worker_start_failed",
            "error": str(exc),
            "retryable": True,
        })
        return False
    return True


def _start_usability_collector(storage, event_queue):
    """Optional measurement initialization must never prevent task admission."""
    try:
        from backend.core.usability import UsabilityCollector
        collector = UsabilityCollector(storage.paths.project_root, on_warning=event_queue.put)
        collector.start()
        return collector
    except Exception:
        event_queue.put({"event": "error", "code": "USABILITY_COLLECTION_START_FAILED",
            "message": "Background usage statistics could not start; task execution is unaffected."})
        return None


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

    # Refuse catastrophic repository roots before any broad initial scan.  A
    # non-git directory is only a warning during the staged migration, but a
    # home/volume root is never an acceptable Agent workspace.
    scope = assess_repository_scope(session.workspace_path)
    if not scope.allowed:
        _emit_v2({
            "event": "repository_scope_blocked",
            "severity": "error",
            "workspace": str(scope.workspace),
            "git_root": str(scope.git_root or ""),
            "reasons": list(scope.reasons),
        }, priority="immediate")
        _release_pid_file(project)
        return
    if scope.severity == "warning":
        _emit_v2({
            "event": "repository_scope_warning",
            "severity": "warning",
            "workspace": str(scope.workspace),
            "reason": ",".join(scope.reasons),
        }, priority="immediate")

    # Session/task/message/receipt state is authoritative in SQLite.  Do not
    # fall back to JSONL when initialization or migration fails: that would
    # recreate two competing sources of truth.
    try:
        storage_runtime = StorageRuntime(session.workspace_path)
        from backend.core.loop.manager import SessionStore
        session_store = SessionStore(
            str(session.workspace_path), storage=storage_runtime,
        )
    except Exception as exc:
        _emit_v2({
            "event": "storage_health",
            "storage": {
                "level": "blocked",
                "reasons": ["authoritative_session_storage_failed"],
                "message": str(exc),
            },
        }, priority="immediate")
        try:
            storage_runtime.close()
        except Exception:
            pass
        _release_pid_file(project)
        return

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
    HistoryManager.set_workspace(
        str(session.workspace_path), storage=storage_runtime,
    )
    from backend.core.knowledge.lesson import LessonManager
    LessonManager.bind_storage(
        Path(session.workspace_path), storage_runtime,
    )

    # Agent process manager. Git repositories receive a host-owned worktree
    # controller rooted in the external project state directory; non-Git
    # workspaces keep the existing bounded shared-workspace mode.
    worktree_manager = None
    try:
        from backend.core.loop.worktree import AgentWorktreeManager
        if scope.git_root is not None:
            worktree_manager = AgentWorktreeManager(
                session.workspace_path, storage_runtime,
            )
    except Exception as exc:
        _emit_v2({
            "event": "worktree_unavailable",
            "severity": "warning",
            "reason": str(exc),
        }, priority="immediate")
    from backend.core.application.process_presentation import ProcessPresentation
    apm = AgentProcessManager(worktree_manager=worktree_manager,
                              presentation=ProcessPresentation(storage_runtime))
    # Runtime services share the same authoritative project storage.  Binding
    # it once on the manager keeps tool spill, checkpoints and DAG state on one
    # path without reopening SQLite from isolated helper code.
    apm.storage = storage_runtime
    apm.session_store = session_store

    # Context bundle for executors + _handle_command — populated incrementally.
    # executors only need apm/hash_cache at bind time; dispatcher/evq added later.
    daemon_ctx = {
        "apm": apm,
        "hash_cache": hash_cache,
        "llm": None,  # set via config or stdin command
        "session_store": session_store,
        "storage": storage_runtime,
        "recovery_available": [],
        "recovery_candidates": [],
        "btw_tasks": {},
        "btw_tasks_lock": threading.RLock(),
    }

    # v0.38: AgentTool 定义 —— 替代裸 dict[str, Callable]
    _WRITE_TOOLS = {"formalize", "write", "edit", "push", "sync",
                    "bash", "delete", "rm", "mv", "cp", "mkdir"}

    tool_executors = {
        "scan": AgentTool(
            name="scan",
            description="扫描项目工作区，检测文件变更和 git 状态。当需要了解项目当前状态、检查哪些文件被修改时使用。",
            parameters={"type": "object", "properties": {"files": {"type": "array", "items": {"type": "string"}, "description": "可选，指定要扫描的文件列表"}}, "required": []},
            execute=partial(_exec_scan, daemon_ctx, session, project),
            read_only=True,
        ),
        "status": AgentTool(
            name="status",
            description="获取工作区语义化状态摘要（文件变更、合同漂移、治理信号）。",
            parameters={"type": "object", "properties": {"semantic": {"type": "boolean", "description": "是否返回语义化摘要"}}, "required": []},
            execute=partial(_exec_status, daemon_ctx, session, project),
            read_only=True,
        ),
        "formalize": AgentTool(
            name="formalize",
            description="基于选中的工作区文件创建正式的结构化提交（formal commit）。需要 indices 和 message 参数。",
            parameters={"type": "object", "properties": {"indices": {"type": "array", "items": {"type": "integer"}}, "message": {"type": "string"}}, "required": ["message"]},
            execute=partial(_exec_formalize, daemon_ctx, session, project),
            read_only=False,
            resources=["filesystem:*"],
            effect=ToolEffect.WORKSPACE_WRITE,
            isolated=True,
            cancellation=CancellationMode.ISOLATED_PROCESS,
            runner_name="formalize",
        ),
        "recall_grep": AgentTool(
            name="recall_grep",
            description="全文搜索知识库中的历史教训（lessons），按关键词匹配。用于查找相似问题的处理经验。",
            parameters={"type": "object", "properties": {"query": {"type": "string", "description": "搜索关键词"}, "top_k": {"type": "integer"}, "agent_context": {"type": "string"}}, "required": ["query"]},
            execute=partial(_exec_recall_grep, daemon_ctx, session, project),
            read_only=True,
        ),
        "recall_semantic": AgentTool(
            name="recall_semantic",
            description="语义搜索知识库中的历史教训，按向量相似度匹配。",
            parameters={"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}, "agent_context": {"type": "string"}}, "required": ["query"]},
            execute=partial(_exec_recall_semantic, daemon_ctx, session, project),
            read_only=True,
        ),
        "recall_rag": AgentTool(
            name="recall_rag",
            description="RAG（检索增强生成）搜索知识库。",
            parameters={"type": "object", "properties": {"query": {"type": "string"}, "agent_context": {"type": "string"}}, "required": ["query"]},
            execute=partial(_exec_recall_rag, daemon_ctx, session, project),
            read_only=True,
        ),
    }

    tool_executors.update({
        "assemble_context": AgentTool(
            name="assemble_context",
            description="汇编上下文：从 policy signals + recall + dependency graph 三层收集相关上下文。需要 task 和 files 参数。",
            parameters={"type": "object", "properties": {"task": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}}, "required": ["task"]},
            execute=partial(_exec_assemble_context, daemon_ctx, session, project),
            read_only=True,
        ),
        "assemble_return_context": AgentTool(
            name="assemble_return_context",
            description="构建 B Agent 返回给 A Agent 的上下文转录。需要 process_id 参数。",
            parameters={"type": "object", "properties": {"process_id": {"type": "string"}}, "required": ["process_id"]},
            execute=partial(_exec_assemble_return_context, daemon_ctx, session, project),
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
            execute=partial(_exec_decompose_task, daemon_ctx, session, project),
            read_only=True,
        ),
        "run_test": AgentTool(
            name="run_test",
            description=(
                "Run one registered pytest file/node under deterministic seeds and "
                "atomically update .gitgo/test_manifest.json."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "test_id": {"type": "string"},
                    "target": {"type": "string"},
                    "seeds": {"type": "array", "items": {"type": "integer"}},
                    "timeout": {"type": "integer"},
                },
                "required": ["test_id", "target"],
            },
            execute=partial(run_registered_test, session.workspace_path),
            read_only=False,
            effect=ToolEffect.PROCESS,
            cancellation=CancellationMode.ISOLATED_PROCESS,
            resources=["process:pytest", "filesystem:.gitgo/test_manifest.json"],
            timeout=900,
        ),
    })

    # Bind the canonical development tool set to this daemon workspace.  These
    # names are additive; legacy daemon/governance tools keep their contracts.
    tool_executors.update(build_workspace_tools(session.workspace_path))

    def _bind_private_workspace(tool: AgentTool) -> None:
        previous = tool.prepare_args

        def prepare(args: dict) -> dict:
            prepared = previous(args) if previous else dict(args or {})
            prepared = dict(prepared)
            prepared["_workspace"] = str(session.workspace_path)
            properties = tool.parameters.get("properties", {})
            if "workspace_path" in properties:
                prepared["workspace_path"] = str(session.workspace_path)
            if "project_name" in properties:
                # Project identity is host-owned runtime context.  Leaving an
                # optional model argument blank silently redirected knowledge
                # tools away from the active project's instance/pending store.
                prepared["project_name"] = project.name
            return prepared

        tool.prepare_args = prepare

    isolated_existing = {
        "contract_detect_drift", "contract_get_impact",
        "contract_get_changed_symbols", "lesson_search", "lesson_discard",
        "lesson_verify", "lesson_harvest", "lesson_promote", "lesson_list",
        "privacy_scan", "memory_snapshot", "memory_restore", "run_test",
    }
    for name in isolated_existing:
        tool = tool_executors.get(name)
        if tool is None:
            continue
        tool.isolated = True
        tool.runner_name = name
        tool.cancellation = CancellationMode.ISOLATED_PROCESS
        _bind_private_workspace(tool)

    # Formalize is stateful, so the child receives a serializable configuration
    # and the daemon refreshes its in-memory view from the child's atomic state.
    from backend.core.config import _serialize_config
    formalize_tool = tool_executors["formalize"]

    def _prepare_formalize(args: dict) -> dict:
        prepared = dict(args or {})
        prepared["_workspace"] = str(session.workspace_path)
        prepared["_project_name"] = project.name
        prepared["_config"] = _serialize_config(cfg)
        return prepared

    def _refresh_formalize(result: dict) -> dict:
        restored = SyncSession.load_session(project, cfg)
        if restored is not None:
            session.formal_commits = restored.formal_commits
            session.stage = restored.stage
        return result

    formalize_tool.prepare_args = _prepare_formalize
    formalize_tool.finalize_result = _refresh_formalize

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

    # The governance definitions above replace earlier dictionary entries, so
    # apply the isolated execution contract after the complete registry exists.
    for name in isolated_existing:
        tool = tool_executors.get(name)
        if tool is None:
            continue
        tool.isolated = True
        tool.runner_name = name
        tool.cancellation = CancellationMode.ISOLATED_PROCESS
        _bind_private_workspace(tool)

    from backend.core.loop.gate import RingGate
    from backend.adapters.local_git_runner import LocalGitRunner
    _git_runner = LocalGitRunner(session.workspace_path)
    dispatcher = ToolDispatcher(
        RingGate(), tool_executors,
        history_writer=HistoryManager.add_operation,
        git_runner=_git_runner,
    )

    # Event queue — created before wiring into daemon_ctx
    evq: queue.Queue = queue.Queue()
    daemon_ctx["dispatcher"] = dispatcher
    daemon_ctx["evq"] = evq

    storage_runtime.set_health_listener(lambda event: evq.put(event))

    # Rebuild durable process trees only after the canonical tool catalog and
    # event queue exist. Recovery restores no self-execution lease and starts no
    # model/tool work; every candidate waits for an explicit user action.
    try:
        from backend.core.loop.recovery import restore_incomplete_processes
        recovery_candidates = restore_incomplete_processes(
            session_store, apm, session.workspace_path,
        )
    except Exception as exc:
        _emit_v2({
            "event": "session_recovery_failed",
            "severity": "error",
            "error": str(exc),
        }, priority="immediate")
        storage_runtime.close()
        _release_pid_file(project)
        return
    daemon_ctx["recovery_candidates"] = recovery_candidates
    daemon_ctx["recovery_available"] = [
        item["process_id"] for item in recovery_candidates
    ]
    if recovery_candidates:
        _emit_v2({
            "event": "sessions_recovery_available",
            "count": len(recovery_candidates),
            "process_ids": list(daemon_ctx["recovery_available"]),
            "sessions": recovery_candidates,
        }, priority="immediate")

    # Startup is a protocol barrier: clients cannot send work until they see it.
    # A normal-priority event may remain in the micro-batch forever while the
    # daemon is idle, causing the client to time out and only observe this event
    # during shutdown when the buffer is finally flushed.
    _emit_v2({
        "event": "daemon_started",
        "project": project.name,
        "pid": os.getpid(),
        "status": session.status_dict(semantic=True),
    }, priority="immediate")

    # Background threads
    usability = _start_usability_collector(storage_runtime, evq)
    daemon_ctx["usability"] = usability
    # The watcher and the scanner must share one exclusion policy.  Using only
    # project.force_exclude here omitted host-private paths such as .gitgo/;
    # every checkpoint/tool receipt then dirtied the workspace and recursively
    # triggered a full scan on the daemon's command loop.
    from backend.core.operations import get_exclude_patterns
    exclude = get_exclude_patterns(
        project, Path(session.workspace_path), file_adapter=session.ws_adapter,
    )
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
                _emit({
                    "event": "workspace_dirty",
                    "project": project.name,
                    "changed_files": list(ev.get("changed_files", []) or []),
                })
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

                    engine = PolicyEngine(changed_files=list(changed or []))
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
                    ws_path = str(session.workspace_path)
                    daemon_ctx["last_policy_results"] = results
                    fresh_governance = _publish_governance_snapshot(
                        session, project, daemon_ctx, apm,
                        policy_results=results,
                    )
                    signals = fresh_governance["signals"]

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
                    )

                    # 捕获 lesson trigger 信号
                    for index, lt in enumerate(results.get("lesson_triggers", [])):
                        capture_signal("lesson_trigger", {
                            "trigger": lt.get("file", ""),
                            "rule": lt.get("rule", ""),
                            "severity": lt.get("severity", "medium"),
                            "detail": lt,
                        }, project.name, source_event_id=(
                            f"{session._correlation_id}:lesson_trigger:{index}:"
                            f"{lt.get('file', '')}:{lt.get('rule', '')}"
                        ))

                    # 捕获 contract drift 信号
                    for index, drift in enumerate(results.get("contract_drift", [])):
                        capture_signal("contract_drift", {
                            "trigger": drift.get("file", ""),
                            "rule": drift.get("rule", "contract drift"),
                            "detail": drift,
                        }, project.name, source_event_id=(
                            f"{session._correlation_id}:contract_drift:{index}:"
                            f"{drift.get('file', '')}:{drift.get('rule', '')}"
                        ))

                    # 检查是否触发 LLM 总结。Provider resolution belongs to
                    # this event-driven boundary; it must not depend on whether
                    # a foreground task happened to populate daemon_ctx first.
                    for sig_type in ("lesson_trigger", "contract_drift"):
                        if should_trigger_harvest(sig_type, project.name):
                            try:
                                harvest_provider = _background_llm_provider(daemon_ctx)
                            except Exception as exc:
                                _emit({
                                    "event": "lesson_harvest_failed",
                                    "time": datetime.now(timezone.utc).isoformat(),
                                    "reason": "provider_unavailable",
                                    "error": str(exc),
                                    "retryable": True,
                                })
                                break
                            if harvest_provider is None:
                                _emit({
                                    "event": "lesson_harvest_failed",
                                    "time": datetime.now(timezone.utc).isoformat(),
                                    "reason": "provider_not_configured",
                                    "error": "Configure and test an active Provider.",
                                    "retryable": True,
                                })
                                break
                            _start_background_harvest(
                                daemon_ctx,
                                event_queue=evq,
                                provider=harvest_provider,
                                workspace_path=ws_path,
                                project_name=project.name,
                                signal_type=sig_type,
                            )
                            break

                    _emit({
                        "event": "operation_complete", "op": "scan",
                        "status": "success",
                        "status_dict": session.status_dict(semantic=True),
                        "governance_warnings": gov_warnings,
                    })
                except Exception as exc:
                    _emit({"event": "operation_complete", "op": "scan",
                           "status": "failed", "error": str(exc)})

            elif event_type == "knowledge_harvest_result":
                if daemon_ctx.get("knowledge_harvest_inflight") == ev.get("harvest_id"):
                    daemon_ctx.pop("knowledge_harvest_inflight", None)
                if ev.get("status") == "success":
                    _publish_governance_snapshot(
                        session, project, daemon_ctx, apm,
                        policy_results=daemon_ctx.get("last_policy_results"),
                    )
                    _emit({
                        "event": "lessons_harvested",
                        "harvest_id": ev.get("harvest_id"),
                        "time": ev.get("time"),
                        "count": int(ev.get("count", 0)),
                        "signal_type": ev.get("signal_type", ""),
                    })
                else:
                    _emit({
                        "event": "lesson_harvest_failed",
                        "time": ev.get("time"),
                        "reason": ev.get("reason", "harvest_failed"),
                        "error": ev.get("error", "Knowledge harvest failed."),
                        "retryable": bool(ev.get("retryable", True)),
                    })

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
            elif event_type in ("text_delta", "reasoning_delta", "progress_summary",
                                "toolcall_start", "toolcall_delta",
                                "toolcall_done", "tool_progress",
                                "stream_recovery", "agent_started",
                                "agent_terminal", "decision_required",
                                "tool_result", "provider_request_started",
                                "provider_response_completed",
                                "provider_response_incomplete", "provider_usage",
                                "governance_snapshot", "context_window_action",
                                "context_compaction_completed", "completion_gate",
                                "task_bundle_delegated", "storage_health",
                                "repository_scope_warning",
                                "session_recovery_resumed",
                                "session_recovery_discarded",
                                "session_recovery_blocked",
                                "coordination_event",
                                "coordination_event_resolved",
                                "coordination_observation_failed"):
                _emit_v2(ev)  # priority="normal" — 微批
            elif event_type == "agent_complete":
                _emit_v2(ev, priority="immediate")  # 修复：之前无分支→静默丢弃
                # Session is durable across tasks (4A). Process checkpoints are
                # retained until an explicit retention policy reaps them.

            elif event_type == "shutdown":
                _handle_shutdown()

            elif event_type == "error":
                _emit(ev)

            # TraceJournal is the trust boundary for Agent runtime events: it
            # redacts credentials and adds a monotonic envelope.  Forward that
            # envelope generically so new semantic events cannot disappear
            # merely because a second daemon allowlist was not updated.
            elif _is_traced_runtime_event(ev):
                _emit_v2(ev)

            # v0.44: 每轮末尾 flush 微批 buffer，防止空闲时事件滞留
            _flush_emit_buffer()

    finally:
        watcher.stop()
        poller.stop()
        reader.stop()
        # BTW sidecars are intentionally outside the durable A/B process DAG,
        # but they still own cancellable provider/tool work. Quiesce them before
        # storage and process resources close.
        btw_lock = daemon_ctx.get("btw_tasks_lock")
        with btw_lock if btw_lock is not None else nullcontext():
            btw_entries = list(daemon_ctx.get("btw_tasks", {}).values())
        for entry in btw_entries:
            entry.get("cancel_event", threading.Event()).set()
            sidecar_process = entry.get("process")
            if sidecar_process is not None:
                sidecar_process.cancel_requested = True
                sidecar_process.cancellation_reason = "daemon_shutdown"
        # Quiesce real task threads while authoritative storage is still open.
        # Recovery-only candidates have no thread and remain durable for the
        # next explicit resume/discard decision; they must not be auto-run or
        # silently converted to cancellation during an ordinary daemon stop.
        live_thread_ids = [
            process_id for process_id, thread in apm._threads.items()
            if thread.is_alive()
        ]
        live_roots = []
        for process_id in live_thread_ids:
            process = apm.get(process_id)
            if process is None:
                continue
            root = process
            while root.parent_id and apm.get(root.parent_id) is not None:
                root = apm.get(root.parent_id)
            if root.process_id not in live_roots:
                live_roots.append(root.process_id)
        for process_id in live_roots:
            apm.kill(process_id, reason="daemon_shutdown")
        shutdown_deadline = time.monotonic() + max(
            1.0, min(float(os.getenv("GITGO_DAEMON_SHUTDOWN_GRACE_SECONDS", "15")), 60.0)
        )
        for entry in btw_entries:
            thread = entry.get("thread")
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
        for process_id in live_thread_ids:
            thread = apm._threads.get(process_id)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
        storage_writers_alive = [
            process_id for process_id in live_thread_ids
            if (thread := apm._threads.get(process_id)) is not None and thread.is_alive()
        ]
        if storage_writers_alive:
            _emit_v2({
                "event": "storage_health",
                "storage": {
                    "level": "warning",
                    "reasons": ["daemon_shutdown_grace_exhausted"],
                    "message": (
                        "Task threads did not quiesce before shutdown; the OS will "
                        "close SQLite handles at process exit to avoid a close/write race."
                    ),
                    "process_ids": storage_writers_alive,
                },
            }, priority="immediate")
        hash_cache.flush()
        if usability is not None:
            usability.stop()
        if storage_runtime is not None and not storage_writers_alive:
            storage_runtime.close()
        _release_pid_file(project)
        # v0.45: cleanup temp resources on shutdown
        if not storage_writers_alive:
            _cleanup_resources(str(session.workspace_path))
        _emit({"event": "daemon_stopped", "project": project.name})
