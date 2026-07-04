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
from pathlib import Path


class DaemonClient:
    """Manages a gitgo daemon subprocess and communicates via stdin/stdout JSON.

    Usage:
        client = DaemonClient("myproject")
        client.start()
        result = client.send_command({"cmd": "loop_status"})
        llm = client.send_llm_call([{"role":"user","content":"hello"}], "pid-123")
        client.stop()
    """

    def __init__(self, project_name: str) -> None:
        self.project_name = project_name
        self._process: subprocess.Popen | None = None
        self._running = False
        self._lock = threading.Lock()
        self._cmd_events: dict[str, threading.Event] = {}
        self._cmd_results: dict[str, dict] = {}
        self._llm_event = threading.Event()
        self._llm_data: dict | None = None
        self._started_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._stderr_lines: list[str] = []

        # Project root is 4 levels up from backend/core/daemon/
        self._project_root = Path(__file__).resolve().parent.parent.parent.parent

    # ── lifecycle ──────────────────────────────────────────────

    def start(self, timeout: float = 30.0) -> None:
        """Start daemon subprocess. Kills any existing daemon for this project first."""
        with self._lock:
            if self._running:
                return

            self._kill_existing()

            cmd = [
                sys.executable, "-m", "gitgo",
                "--mode", "daemon",
                "--project", self.project_name,
                "--daemon-action", "start",
                "--trial-interval", "9999",
                "--debounce", "2.0",
            ]

            self._process = subprocess.Popen(
                cmd,
                cwd=str(self._project_root.parent),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            self._started_event.clear()

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
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()

        with self._lock:
            self._running = False
            self._wake_all_waiters()

    def is_running(self) -> bool:
        """Check if daemon subprocess is alive and healthy."""
        with self._lock:
            if not self._running or self._process is None:
                return False
            return self._process.poll() is None

    # ── command interface ──────────────────────────────────────

    def send_command(self, cmd: dict, timeout: float = 30.0) -> dict:
        """Send a synchronous command and wait for its command_result.

        Returns the 'result' dict from the response, or raises RuntimeError on error.
        """
        cmd_name = cmd.get("cmd", "")
        if not cmd_name:
            raise ValueError("Command dict must contain 'cmd' key")

        # Clear previous result for this command name
        with self._lock:
            if cmd_name not in self._cmd_events:
                self._cmd_events[cmd_name] = threading.Event()
            self._cmd_events[cmd_name].clear()
            self._cmd_results.pop(cmd_name, None)

        self._write_cmd(cmd)

        event = self._cmd_events[cmd_name]
        if not event.wait(timeout=timeout):
            raise RuntimeError(
                f"Command '{cmd_name}' timed out after {timeout}s "
                f"(daemon running={self.is_running()})"
            )

        with self._lock:
            result = self._cmd_results.pop(cmd_name, None)

        if result is None:
            raise RuntimeError(
                f"Command '{cmd_name}': daemon disconnected before response"
            )

        if "error" in result:
            raise RuntimeError(f"Command '{cmd_name}' failed: {result['error']}")

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

                if event_type == "daemon_started":
                    self._started_event.set()

                elif event_type == "command_result":
                    cmd_name = event.get("cmd", "")
                    with self._lock:
                        self._cmd_results[cmd_name] = event
                        if cmd_name in self._cmd_events:
                            self._cmd_events[cmd_name].set()

                elif event_type == "llm_response":
                    with self._lock:
                        self._llm_data = event
                        self._llm_event.set()

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
                self._stderr_lines.append(line)
        except Exception:
            pass

    def _wake_all_waiters(self) -> None:
        """Wake up all threads waiting on command responses."""
        for event in self._cmd_events.values():
            event.set()
        self._llm_event.set()
        self._started_event.set()

    def _kill_existing(self) -> None:
        """Check PID file and kill any existing daemon for this project."""
        from backend.core.config import ConfigManager
        try:
            cfg = ConfigManager.load()
        except Exception:
            return

        proj = None
        for p in cfg.projects:
            if p.name == self.project_name:
                proj = p
                break
        if proj is None:
            return

        ws_path = proj.workspace.file_access.path if proj.workspace else ""
        if not ws_path:
            return

        pid_path = Path(ws_path) / ".gitgo" / "daemon.pid"
        if not pid_path.exists():
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

        # Process exists — kill it
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
