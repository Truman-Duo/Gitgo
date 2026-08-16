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

    # Context bundle for executors + _handle_command — populated incrementally.
    # executors only need apm/hash_cache at bind time; dispatcher/evq added later.
    daemon_ctx = {
        "apm": apm,
        "hash_cache": hash_cache,
        "llm": None,  # set via config or stdin command
        "session_store": session_store,  # v0.45: session persistence
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
