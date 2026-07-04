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
    """Write a line-delimited JSON event to stdout."""
    print(json.dumps(event, ensure_ascii=False), flush=True)


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
            os.kill(old_pid, 0)  # signal 0 = existence check
        except (OSError, ValueError, ProcessLookupError):
            pass  # stale — overwrite
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

    tool_executors = {
        "scan": _exec_scan,
        "status": _exec_status,
        "formalize": _exec_formalize,
    }

    from backend.core.loop.gate import RingGate
    dispatcher = ToolDispatcher(
        RingGate(), tool_executors,
        history_writer=HistoryManager.add_operation,
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

            if event_type == "workspace_dirty":
                # ── Debounce ──
                now = time.time()
                last_check = getattr(run_daemon, '_last_policy_check', 0.0)
                if now - last_check < debounce_sec:
                    continue
                run_daemon._last_policy_check = now
                _emit({"event": "workspace_dirty", "project": project.name})
                _emit({"event": "operation_started", "op": "scan"})
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
                        _emit({"event": "lesson_matched", "lesson_id": l["lesson_id"],
                               "severity": l["severity"], "rule": l["rule"]})
                    for d in results.get("contract_drift", []):
                        _emit({"event": "governance_drift", "rule": d.get("rule", "contract"),
                               "level": "warning", "message": d.get("message", "")})
                        HistoryManager.add_operation(
                            project.name, "governance_drift", "warning",
                            {"rule": d.get("rule", "contract"), "message": d.get("message", "")},
                            correlation_id=session._correlation_id)
                    for w in results.get("identity_integrity", []):
                        _emit({"event": "governance_drift", "rule": w.get("rule", "integrity"),
                               "level": w.get("level", "warning"), "message": w.get("message", "")})
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
                        _emit({"event": "policy_results",
                               "governance_warnings": gov_warnings, "message": msg})

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

            elif event_type == "shutdown":
                _handle_shutdown()

            elif event_type == "error":
                _emit(ev)

    finally:
        watcher.stop()
        poller.stop()
        reader.stop()
        hash_cache.flush()
        _release_pid_file(project)
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
                    "status": e.status,
                })
        recent_tools = recent_tools[-20:]
        _emit({"event": "command_result", "cmd": "loop_status",
               "result": {
                   "daemon_online": True,
                   "processes": processes,
                   "recent_tool_executed": recent_tools,
               }})

    else:
        _emit({"event": "command_result", "cmd": cmd_name,
               "error": f"Unknown command: {cmd_name}"})
