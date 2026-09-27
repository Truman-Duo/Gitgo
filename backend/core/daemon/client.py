"""DaemonClient — subprocess-based client for gitgo daemon communication.

Communicates with a gitgo daemon process via line-delimited JSON on stdin/stdout.
Handles both synchronous commands (request→command_result) and async events (llm_response).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from backend.core.process_control import (
    attach_kill_job,
    close_job,
    creation_flags,
    terminate_tree,
)


class DaemonCommandError(RuntimeError):
    """Structured command failure returned by a still-running daemon."""

    def __init__(self, command: str, result: dict):
        self.command = command
        self.code = str(result.get("error") or "DAEMON_COMMAND_FAILED")
        self.message = str(result.get("message") or result.get("detail") or self.code)
        self.error_info = dict(result.get("error_info") or {})
        self.details = dict(self.error_info.get("details") or {})
        super().__init__(f"Command '{command}' failed: {self.code}: {self.message}")


class DaemonClient:
    """Manages a gitgo daemon subprocess and communicates via stdin/stdout JSON.

    Usage:
        client = DaemonClient("myproject")
        client.start()
        result = client.send_command({"cmd": "loop_status"})
        llm = client.send_llm_call([{"role":"user","content":"hello"}], "pid-123")
        client.stop()
    """

    MAX_RECONNECT_ATTEMPTS = 5
    RECONNECT_BACKOFF_CAP = 16  # seconds
    MAX_STDERR_LINES = 1000

    IDEMPOTENT_COMMANDS = {
        "status", "loop_status", "task_result", "scan", "llm_configure", "llm_call",
    }
    TASK_RESULT_POLL_SECONDS = 10.0

    def __init__(self, project_name: str, *, existing_policy: str = "fail") -> None:
        if existing_policy not in ("fail", "replace"):
            raise ValueError("existing_policy must be 'fail' or 'replace'")
        self.project_name = project_name
        self.existing_policy = existing_policy
        self._process: subprocess.Popen | None = None
        self._job_handle = None
        self._running = False
        self._lock = threading.Lock()
        self._cmd_events: dict[str, threading.Event] = {}
        self._cmd_results: dict[str, dict] = {}
        self._llm_event = threading.Event()
        self._llm_data: dict | None = None
        self._agent_events: dict[str, threading.Event] = {}
        self._agent_data: dict[str, dict] = {}
        self._started_event = threading.Event()
        self._daemon_started = False
        self._startup_events: list[dict] = []
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lines: list[str] = []
        self._event_listeners: list[Callable[[dict], None]] = []

        # Project root is 4 levels up from backend/core/daemon/
        self._project_root = Path(__file__).resolve().parent.parent.parent.parent

    # ── lifecycle ──────────────────────────────────────────────

    def start(self, timeout: float = 30.0) -> None:
        """Start an owned daemon subprocess.

        The default is fail-closed when a live daemon already owns the project.
        Replacement is available only through an explicit constructor policy.
        """
        with self._lock:
            if self._running:
                return

            if self.existing_policy == "replace":
                self._kill_existing()
            else:
                self._fail_if_existing()

            # Execute the repository entry point directly.  The bundled
            # portable Python deliberately pins ``sys.path`` to the Gitgo
            # repository so ``python -m gitgo`` cannot resolve the repository
            # as a package from its parent directory.  A script entry point is
            # stable for both that runtime and ordinary Python installations.
            cmd = [
                sys.executable, str(self._project_root / "__main__.py"),
                "--mode", "daemon",
                "--project", self.project_name,
                "--daemon-action", "start",
                "--trial-interval", "9999",
                "--debounce", "2.0",
            ]

            self._process = subprocess.Popen(
                cmd,
                cwd=str(self._project_root),
                env={
                    **os.environ,
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
                creationflags=creation_flags(),
            )
            # The daemon owns Provider requests and every B/tool descendant.
            # A kill-on-close Job Object makes an abrupt Dashboard/Native Host
            # exit fail closed on Windows instead of orphaning paid work.
            self._job_handle = attach_kill_job(self._process)

            self._started_event.clear()
            self._daemon_started = False
            self._startup_events.clear()

            self._reader_thread = threading.Thread(
                target=self._read_stdout,
                daemon=True,
                name=f"daemon-out-{self.project_name}",
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                daemon=True,
                name=f"daemon-err-{self.project_name}",
            )

            self._running = True
            self._reader_thread.start()
            self._stderr_thread.start()

        # Wait for daemon_started outside the lock
        if not self._started_event.wait(timeout=timeout):
            self.stop()
            stderr_tail = "".join(self._stderr_lines[-5:])
            raise RuntimeError(
                f"Daemon for '{self.project_name}' did not start within {timeout}s. "
                f"stderr tail: {stderr_tail[-300:]}"
            )
        # ``_wake_all_waiters`` also releases startup waiters when stdout
        # closes.  That is a failure wake-up, not a successful handshake.
        # Without this check an early import/config crash makes ``start``
        # return successfully and the first real command fails later with the
        # misleading message "Daemon is not running".
        if not self._daemon_started or not self.is_running():
            process = self._process
            return_code = process.poll() if process is not None else None
            if process is not None and return_code is None:
                try:
                    return_code = process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            # stdout EOF can win the race against the stderr reader.  Give the
            # bounded diagnostic reader a chance to drain the already-closed
            # pipe so the frontend receives the actual startup cause.
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=0.5)
            stderr_tail = self.diagnostic_tail(2000).strip()
            startup_event = self._startup_events[-1] if self._startup_events else None
            close_job(self._job_handle)
            self._job_handle = None
            raise RuntimeError(
                f"Daemon for '{self.project_name}' exited before readiness "
                f"(code={return_code})."
                + (f" startup event: {json.dumps(startup_event, ensure_ascii=False)}"
                   if startup_event else "")
                + (f" stderr tail: {stderr_tail}" if stderr_tail else "")
            )

    def stop(self) -> None:
        """Send shutdown command and wait for subprocess to exit."""
        with self._lock:
            if not self._running or self._process is None:
                self._running = False
                return

        try:
            self._write_cmd({"cmd": "shutdown"})
        except Exception:
            pass

        try:
            self._process.wait(timeout=5.0)
            close_job(self._job_handle)
        except subprocess.TimeoutExpired:
            terminate_tree(self._process, self._job_handle)
        finally:
            self._job_handle = None

        with self._lock:
            self._running = False
            self._wake_all_waiters()

    def is_running(self) -> bool:
        """Check if daemon subprocess is alive and healthy."""
        with self._lock:
            if not self._running or self._process is None:
                return False
            return self._process.poll() is None

    def diagnostic_tail(self, max_chars: int = 2000) -> str:
        """Return a bounded daemon stderr tail for transport diagnostics."""
        with self._lock:
            value = "".join(self._stderr_lines[-20:])
        return value[-max(200, min(int(max_chars), 10000)):]

    def add_event_listener(self, listener: Callable[[dict], None]) -> None:
        """Subscribe to every valid daemon event without taking ownership."""
        with self._lock:
            if listener not in self._event_listeners:
                self._event_listeners.append(listener)

    def remove_event_listener(self, listener: Callable[[dict], None]) -> None:
        with self._lock:
            if listener in self._event_listeners:
                self._event_listeners.remove(listener)

    # ── command interface ──────────────────────────────────────

    def send_command(self, cmd: dict, timeout: float = 30.0) -> dict:
        """Send a synchronous command and wait for its command_result.

        Returns the 'result' dict from the response, or raises RuntimeError on error.
        Automatically retries with reconnection for idempotent commands.
        """
        cmd_name = cmd.get("cmd", "")
        idempotent = cmd_name in self.IDEMPOTENT_COMMANDS
        max_attempts = self.MAX_RECONNECT_ATTEMPTS if idempotent else 1

        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                return self._send_command_once(cmd, timeout)
            except (RuntimeError, BrokenPipeError, OSError) as e:
                last_error = e
                if attempt >= max_attempts - 1:
                    raise
                # Only retry if daemon is actually dead
                if self.is_running():
                    raise
                backoff = min(2 ** attempt, self.RECONNECT_BACKOFF_CAP)
                time.sleep(backoff)
                try:
                    self.start()
                except Exception:
                    continue

        raise last_error  # type: ignore[misc]

    def send_btw(self, cmd: dict, timeout: float = 135.0) -> dict:
        """Start an isolated BTW sidecar and wait for its correlated terminal event."""
        command = dict(cmd)
        sidecar_id = str(command.get("sidecar_id") or uuid.uuid4())
        command["sidecar_id"] = sidecar_id
        completed = threading.Event()
        terminal: dict = {}

        def listener(event: dict) -> None:
            if (
                event.get("event") == "btw_complete"
                and str(event.get("sidecar_id") or "") == sidecar_id
            ):
                terminal.update(event)
                completed.set()

        self.add_event_listener(listener)
        try:
            acknowledgement = self.send_command(command, timeout=min(timeout, 30.0))
            if not acknowledgement.get("accepted"):
                raise RuntimeError(f"BTW sidecar admission failed: {acknowledgement}")
            if not completed.wait(timeout=timeout):
                try:
                    self.send_command({
                        "cmd": "task", "action": "btw_cancel",
                        "sidecar_id": sidecar_id,
                    }, timeout=5.0)
                except Exception:
                    pass
                raise RuntimeError(f"BTW sidecar {sidecar_id} timed out after {timeout}s")
            if terminal.get("error"):
                raise RuntimeError(str(terminal["error"]))
            result = terminal.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("BTW sidecar terminal event omitted result")
            return result
        finally:
            self.remove_event_listener(listener)

    def _send_command_once(self, cmd: dict, timeout: float = 30.0) -> dict:
        """Single attempt at sending a command (no retry/reconnect)."""
        cmd_name = cmd.get("cmd", "")
        if not cmd_name:
            raise ValueError("Command dict must contain 'cmd' key")

        request_id = str(uuid.uuid4())
        cmd["request_id"] = request_id

        with self._lock:
            self._cmd_events[request_id] = threading.Event()
            self._cmd_events[request_id].clear()

        self._write_cmd(cmd)

        event = self._cmd_events[request_id]
        if not event.wait(timeout=timeout):
            with self._lock:
                self._cmd_events.pop(request_id, None)
            raise RuntimeError(
                f"Command '{cmd_name}' timed out after {timeout}s "
                f"(daemon running={self.is_running()})"
            )

        with self._lock:
            result = self._cmd_results.pop(request_id, None)
            self._cmd_events.pop(request_id, None)

        if result is None:
            process = self._process
            return_code = process.poll() if process is not None else None
            stderr_tail = self.diagnostic_tail(2000).strip()
            raise RuntimeError(
                f"Command '{cmd_name}': daemon disconnected before response "
                f"(code={return_code})"
                + (f". stderr tail: {stderr_tail}" if stderr_tail else "")
            )

        if "error" in result:
            raise DaemonCommandError(cmd_name, result)

        return result.get("result", {})

    def send_llm_call(
        self, messages: list[dict], process_id: str = "", timeout: float = 120.0
    ) -> dict:
        """Send llm_call and wait for the async llm_response event.

        Returns the llm_response dict with keys: process_id, response, status, error.
        """
        with self._lock:
            self._llm_event.clear()
            self._llm_data = None

        # Send llm_call — this returns immediately with status=pending
        ack = self.send_command({
            "cmd": "llm_call",
            "messages": messages,
            "process_id": process_id,
        })
        if ack.get("status") != "pending":
            raise RuntimeError(f"llm_call not accepted: {ack}")

        # Wait for the async llm_response
        if not self._llm_event.wait(timeout=timeout):
            raise RuntimeError(
                f"llm_call response timed out after {timeout}s"
            )

        with self._lock:
            data = self._llm_data
            self._llm_data = None

        if data is None:
            raise RuntimeError("llm_call: daemon disconnected before llm_response")

        if data.get("status") == "error":
            raise RuntimeError(f"llm_call error: {data.get('error', 'unknown')}")

        return data

    def send_task(
        self,
        cmd: dict,
        timeout: float = 300.0,
        on_ack: Callable[[dict], None] | None = None,
    ) -> dict:
        """Send a task command and wait for the async agent_complete event.

        A client-generated task_id is registered before submission, so a fast
        agent_complete cannot race ahead of the waiter.

        Transport/protocol failures raise. Agent failures are returned as the
        canonical TaskOutcome in ``event["outcome"]``.
        """
        command = dict(cmd)
        task_id = str(command.get("task_id") or uuid.uuid4())
        command["task_id"] = task_id

        event = threading.Event()
        with self._lock:
            if task_id in self._agent_events:
                raise RuntimeError(f"Duplicate task_id: {task_id}")
            self._agent_events[task_id] = event

        started_at = time.monotonic()
        try:
            # The waiter already exists when daemon execution can begin.
            # Task admission can legitimately include governance/context work.
            # Keep acknowledgement and execution inside one caller-owned
            # deadline instead of silently applying send_command's 30s default.
            ack = self.send_command(command, timeout=timeout)
        except Exception:
            with self._lock:
                self._agent_events.pop(task_id, None)
                self._agent_data.pop(task_id, None)
            raise

        process_id = ack.get("process_id", "")
        if not process_id:
            with self._lock:
                self._agent_events.pop(task_id, None)
            raise RuntimeError(f"task command did not return process_id: {ack}")
        if ack.get("task_id") != task_id:
            with self._lock:
                self._agent_events.pop(task_id, None)
                self._agent_data.pop(task_id, None)
            raise RuntimeError(
                f"task acknowledgement correlation mismatch: {ack}"
            )
        if on_ack is not None:
            on_ack(dict(ack))

        # ``agent_complete`` is the fast notification path.  Reconcile against
        # the daemon's authoritative process state at a low frequency as well:
        # an OS pipe, listener, or event-buffer defect must not turn already
        # durable successful work into a false timeout and destructive cancel.
        deadline = started_at + timeout
        while not event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if event.wait(timeout=min(self.TASK_RESULT_POLL_SECONDS, remaining)):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                state = self.send_command({
                    "cmd": "task_result",
                    "task_id": task_id,
                    "process_id": process_id,
                }, timeout=min(5.0, remaining))
            except (RuntimeError, BrokenPipeError, OSError):
                # The original task deadline remains authoritative.  A
                # transient reconciliation failure is not itself task failure.
                continue
            if state.get("terminal") and isinstance(state.get("outcome"), dict):
                recovered = {
                    "event": "agent_complete",
                    "task_id": task_id,
                    "process_id": process_id,
                    "session_id": str(state.get("session_id") or ""),
                    "outcome": dict(state["outcome"]),
                    "recovered_from": "task_result",
                }
                recovered_inserted = False
                with self._lock:
                    if task_id not in self._agent_data:
                        self._agent_data[task_id] = recovered
                        recovered_inserted = True
                    self._agent_events[task_id].set()
                if recovered_inserted:
                    self._notify_event(recovered)
                break

        if not event.is_set():
            with self._lock:
                self._agent_events.pop(task_id, None)
                self._agent_data.pop(task_id, None)
            raise RuntimeError(
                f"Agent task {task_id} for {process_id} timed out after {timeout}s"
            )

        with self._lock:
            data = self._agent_data.pop(task_id, None)
            self._agent_events.pop(task_id, None)

        if data is None:
            tail = self.diagnostic_tail()
            raise RuntimeError(
                "task: daemon disconnected before agent_complete"
                + (f"; stderr tail: {tail}" if tail else "")
            )
        outcome = data.get("outcome")
        if not isinstance(outcome, dict):
            raise RuntimeError(f"task: invalid agent_complete payload: {data}")
        from backend.core.loop.outcome import TaskOutcome
        TaskOutcome.from_dict(outcome)
        return data

    # ── internals ──────────────────────────────────────────────

    def _write_cmd(self, cmd: dict) -> None:
        """Write a JSON command to daemon stdin."""
        with self._lock:
            if not self._running or self._process is None or self._process.stdin is None:
                raise RuntimeError("Daemon is not running")
            line = json.dumps(cmd, ensure_ascii=False)
            self._process.stdin.write(line + "\n")
            self._process.stdin.flush()

    def _read_stdout(self) -> None:
        """Background thread: read line-delimited JSON from daemon stdout."""
        try:
            assert self._process is not None
            for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON output (e.g. traceback mixed in) — store for debugging
                    self._stderr_lines.append(f"[stdout non-JSON] {line}")
                    continue

                event_type = event.get("event", "")

                if not self._daemon_started:
                    self._startup_events.append(dict(event))
                    if len(self._startup_events) > 20:
                        self._startup_events = self._startup_events[-20:]

                if event_type == "daemon_started":
                    self._daemon_started = True
                    self._started_event.set()

                elif event_type == "command_result":
                    rid = event.get("request_id", event.get("cmd", ""))
                    with self._lock:
                        self._cmd_results[rid] = event
                        if rid in self._cmd_events:
                            self._cmd_events[rid].set()

                elif event_type == "llm_response":
                    with self._lock:
                        self._llm_data = event
                        self._llm_event.set()

                elif event_type == "agent_complete":
                    task_id = event.get("task_id", "") or event.get("process_id", "")
                    with self._lock:
                        if task_id in self._agent_events:
                            self._agent_data[task_id] = event
                            self._agent_events[task_id].set()

                # Correlation is transport-critical and must happen before
                # observers serialize/render the event.  A verbose reasoning
                # stream or slow Dashboard listener must never make an already
                # received terminal event appear to time out.
                self._notify_event(event)

                # Other events (progress, log, state_changed, etc.) are ignored
        except Exception:
            # Process stdout closed or read error
            pass
        finally:
            with self._lock:
                self._running = False
            self._wake_all_waiters()

    def _read_stderr(self) -> None:
        """Background thread: capture stderr for debugging."""
        try:
            assert self._process is not None
            for line in self._process.stderr:
                with self._lock:
                    self._stderr_lines.append(line)
                    if len(self._stderr_lines) > self.MAX_STDERR_LINES:
                        self._stderr_lines = self._stderr_lines[-self.MAX_STDERR_LINES:]
        except Exception:
            pass

    def _wake_all_waiters(self) -> None:
        """Wake up all threads waiting on command/LLM/agent responses."""
        for event in self._cmd_events.values():
            event.set()
        self._llm_event.set()
        for event in self._agent_events.values():
            event.set()
        self._started_event.set()

    def _notify_event(self, event: dict) -> None:
        with self._lock:
            listeners = list(self._event_listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                # Observers must never be able to break transport correlation.
                continue

    def _fail_if_existing(self) -> None:
        """Reject a second owner while cleaning only demonstrably stale pidfiles."""
        pid_path = self._pid_path()
        if pid_path is None or not pid_path.exists():
            return
        try:
            old_pid = int(pid_path.read_text().strip())
            os.kill(old_pid, 0)
        except (OSError, ValueError, ProcessLookupError):
            try:
                pid_path.unlink()
            except OSError:
                pass
            return
        raise RuntimeError(
            f"DAEMON_ALREADY_RUNNING: project '{self.project_name}' is owned by pid {old_pid}"
        )

    def _pid_path(self) -> Path | None:
        from backend.core.config import ConfigManager
        try:
            cfg = ConfigManager.load()
        except Exception:
            return None
        project = next((p for p in cfg.projects if p.name == self.project_name), None)
        if project is None or not project.workspace_path:
            return None
        return Path(project.workspace_path) / ".gitgo" / "daemon.pid"

    def _kill_existing(self) -> None:
        """Check PID file and kill any existing daemon for this project."""
        pid_path = self._pid_path()
        if pid_path is None or not pid_path.exists():
            return

        try:
            old_pid = int(pid_path.read_text().strip())
            os.kill(old_pid, 0)  # signal 0 = existence check
        except (OSError, ValueError, ProcessLookupError):
            # Stale PID file
            try:
                pid_path.unlink()
            except Exception:
                pass
            return

        # Process exists — terminate the exact pid recorded by this project's
        # daemon lease. Windows exposes no SIGKILL constant and os.kill does
        # not provide descendant-tree semantics; taskkill is the native
        # equivalent of the Job Object fallback used for owned children.
        if sys.platform == "win32":
            completed = subprocess.run(
                ["taskkill", "/PID", str(old_pid), "/T", "/F"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode not in (0, 128):
                raise RuntimeError(
                    f"DAEMON_REPLACE_FAILED: pid {old_pid}: "
                    f"{(completed.stderr or completed.stdout).strip()}"
                )
            try:
                pid_path.unlink()
            except OSError:
                pass
            return

        try:
            os.kill(old_pid, signal.SIGTERM)
        except OSError:
            pass
        else:
            # Wait up to 2s for graceful shutdown
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    os.kill(old_pid, 0)
                    time.sleep(0.1)
                except OSError:
                    break
            else:
                try:
                    os.kill(old_pid, signal.SIGKILL)
                except OSError:
                    pass

        try:
            pid_path.unlink()
        except Exception:
            pass
