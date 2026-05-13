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

    # Initial scan + trial check
    session.step_scan()
    session.step_load_commits()
    session.step_check_trial()

    _emit({
        "event": "daemon_started",
        "project": project.name,
        "pid": os.getpid(),
        "status": session.status_dict(semantic=True),
    })

    # Event queue
    evq: queue.Queue = queue.Queue()

    # Background threads
    exclude = list(project.force_exclude) if project.force_exclude else []
    watcher = WorkspaceWatcher(
        workspace_path=session.workspace_path,
        exclude_patterns=exclude,
        on_dirty=lambda: evq.put({"event": "workspace_dirty"}),
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

            if event_type == "workspace_dirty":
                _emit({"event": "workspace_dirty", "project": project.name})
                _emit({"event": "operation_started", "op": "scan"})
                try:
                    session.step_scan()
                    session.step_load_commits()
                    _emit({"event": "operation_complete", "op": "scan",
                           "status": "success", "status_dict": session.status_dict(semantic=True)})
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
                _handle_command(ev["cmd"], session, project,
                                on_shutdown=_handle_shutdown)

            elif event_type == "shutdown":
                _handle_shutdown()

            elif event_type == "error":
                _emit(ev)

    finally:
        watcher.stop()
        poller.stop()
        reader.stop()
        _release_pid_file(project)
        _emit({"event": "daemon_stopped", "project": project.name})


def _handle_command(cmd: dict, session: SyncSession, project: ProjectConfig,
                    on_shutdown: callable = None) -> None:
    """Dispatch a stdin command to the appropriate step method."""
    cmd_name = cmd.get("cmd", "")

    if cmd_name == "shutdown":
        _emit({"event": "shutdown_ack", "message": "Shutting down"})
        if on_shutdown:
            on_shutdown()
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
            session.step_scan()
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

    else:
        _emit({"event": "command_result", "cmd": cmd_name,
               "error": f"Unknown command: {cmd_name}"})
