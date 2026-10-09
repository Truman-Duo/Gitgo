"""Gitgo native Dashboard host.

The host is the single process-level owner for Dashboard communication.  It
exposes a versioned JSON-lines protocol, routes transport-neutral application
operations, and lazily supervises one daemon runtime per project.
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.core.application import ApplicationServices, OperationError
from backend.core.daemon.client import DaemonClient, DaemonCommandError
from backend.core.protocol_io import dump_protocol_json, write_utf8_line
from backend.core.unicode_safety import normalize_unicode_text, unicode_integrity_error


PROTOCOL_VERSION = 1
HOST_VERSION = "1.0.0"


@dataclass(frozen=True)
class RuntimeLimits:
    max_agent_steps: int = 50
    max_daemon_start_seconds: int = 120
    max_task_seconds: int = 300
    max_task_hard_seconds: int = 0  # explicit RuntimeLimits retains fixed-budget compatibility
    max_tree_agents: int = 8
    max_provider_calls: int = 100
    max_output_tokens: int = 131_072

    @classmethod
    def from_environment(cls) -> "RuntimeLimits":
        return cls(
            max_agent_steps=max(1, min(int(os.getenv("GITGO_MAX_AGENT_STEPS", "50")), 200)),
            max_daemon_start_seconds=max(
                10, min(int(os.getenv("GITGO_MAX_DAEMON_START_SECONDS", "120")), 600)
            ),
            max_task_seconds=max(10, min(int(os.getenv("GITGO_MAX_TASK_SECONDS", "300")), 3600)),
            max_task_hard_seconds=max(10, min(int(os.getenv("GITGO_MAX_TASK_HARD_SECONDS", "1800")), 86_400)),
            max_tree_agents=max(1, min(int(os.getenv("GITGO_MAX_TREE_AGENTS", "8")), 64)),
            max_provider_calls=max(
                1, min(int(os.getenv("GITGO_MAX_PROVIDER_CALLS", "100")), 10_000)
            ),
            max_output_tokens=max(
                256, min(int(os.getenv("GITGO_MAX_OUTPUT_TOKENS", "131072")), 10_000_000)
            ),
        )


class NativeHost:
    """Owns native request correlation and per-project daemon lifecycles."""

    def __init__(self, *, stdin=None, stdout=None, daemon_factory=DaemonClient):
        from backend.core.frontend_origin import frontend_origin
        self.frontend_origin = frontend_origin()
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._daemon_factory = daemon_factory
        self._daemons: dict[str, DaemonClient] = {}
        self._daemon_starting: dict[str, tuple[DaemonClient, threading.Event]] = {}
        self._daemon_start_errors: dict[str, str] = {}
        self._daemon_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._task_requests: dict[str, tuple[str, str]] = {}
        self._active_processes: dict[str, tuple[str, str]] = {}
        self._admission_requests: dict[str, tuple[str, str]] = {}
        self._cancelled_requests: set[str] = set()
        self._btw_requests: dict[str, tuple[str, str]] = {}
        self._active_btw: dict[str, tuple[str, str]] = {}
        self._sessions: dict[str, str] = {}
        self._conversation_processes: dict[str, str] = {}
        self._root_admissions: set[str] = set()
        self._stopping = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="native-host")
        self._summary_pool = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="project-summary",
        )
        self._summary_lock = threading.RLock()
        self._summary_refresh_lock = threading.Lock()
        self._summary_cache: dict[str, dict] = {}
        # A timed-out thread cannot be force-cancelled safely.  Keep at most
        # one outstanding summary read per project and reuse it on the next
        # poll; otherwise a slow SQLite/UNC/daemon read would enqueue another
        # copy every five seconds until the pool and Host request queue were
        # saturated.
        self._summary_inflight: dict[str, Any] = {}
        self._projection_lock = threading.RLock()
        self._storage_projections: dict[str, Any] = {}
        self._storage_projection_inflight: dict[str, Any] = {}
        self._storage_projection_errors: dict[str, str] = {}
        self.limits = RuntimeLimits.from_environment()
        self.services = ApplicationServices(daemon_online=self.daemon_online,
                                            prepare_deletion=self._prepare_deletion)
        self._maintenance_thread = None
        self._config_fingerprint = self._read_config_fingerprint()

    @staticmethod
    def _read_config_fingerprint() -> tuple[int, int] | None:
        from backend.core.config import ConfigManager
        try:
            stat = ConfigManager.default_path().stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _prepare_deletion(self, project: str) -> None:
        from backend.core.application.deletion import blocked, TERMINAL
        with self._daemon_lock:
            if project in self._root_admissions or any(p == project for p, _ in self._active_btw.values()):
                raise blocked("Project has an active request; deletion never cancels it implicitly")
            client = self._daemons.get(project)
            if client and client.is_running():
                status = client.send_command({"cmd": "loop_status"}, timeout=10)
                processes = status.get("processes")
                if not isinstance(processes, dict) or any(p.get("status") not in TERMINAL for p in processes.values()):
                    raise blocked("Runtime is active or its status could not be verified")
                client.stop()
                self._daemons.pop(project, None)
            self._sessions.pop(project, None)
            self._conversation_processes.pop(project, None)

    def _maintenance_loop(self) -> None:
        # External editor saves have no process event. A metadata-only watch
        # publishes them through the same event bus as UI writes; deletion
        # maintenance keeps its original ten-second cadence.
        deletion_tick = 0
        while not self._stopping.wait(1):
            try:
                fingerprint = self._read_config_fingerprint()
                if fingerprint != self._config_fingerprint:
                    self._config_fingerprint = fingerprint
                    try:
                        from backend.core.config import ConfigManager
                        ConfigManager.load(strict=True)
                    except Exception as config_exc:
                        self._emit({"type": "event", "payload": {
                            "event": "config_error", "source": "filesystem",
                            "message": f"Configuration was not applied: {config_exc}",
                        }})
                    else:
                        self._emit({"type": "event", "payload": {
                            "event": "config_changed", "source": "filesystem",
                            "fingerprint": list(fingerprint) if fingerprint else None,
                        }})
                deletion_tick += 1
                if deletion_tick < 10:
                    continue
                deletion_tick = 0
                with self.services._write_lock:
                    results = self.services.deletion.run_due()
                for result in results:
                    self._emit({"type": "event", "payload": {
                        "event": "deletion_status", "plan_id": result["plan_id"],
                        "state": result["state"], "message": result.get("error") or "Deletion completed",
                    }})
            except Exception as exc:
                self._emit({"type": "event", "payload": {"event": "deletion_status",
                    "state": "blocked", "message": f"Deletion maintenance stopped: {exc}"}})
                return  # fail visible; never repeatedly hammer a broken store

    def daemon_online(self, project: str) -> bool:
        with self._daemon_lock:
            client = self._daemons.get(project)
            return bool(client and client.is_running())

    def _emit(self, payload: dict) -> None:
        envelope = {"protocol_version": PROTOCOL_VERSION, **payload}
        line = dump_protocol_json(envelope, separators=(",", ":"))
        with self._write_lock:
            write_utf8_line(self.stdout, line)

    def _response(self, request_id: str, *, result: Any = None,
                  error: dict | None = None) -> None:
        payload = {"type": "response", "request_id": request_id,
                   "ok": error is None}
        if error is None:
            payload["result"] = result
        else:
            payload["error"] = error
        self._emit(payload)

    def _get_daemon(self, project: str, *, start: bool) -> DaemonClient | None:
        owns_start = False
        with self._daemon_lock:
            client = self._daemons.get(project)
            if client is not None and client.is_running():
                return client
            if not start:
                return None
            starting = self._daemon_starting.get(project)
            if starting is None:
                client = self._daemon_factory(project)
                client.add_event_listener(
                    lambda event, project_name=project: self._on_daemon_event(project_name, event)
                )
                ready = threading.Event()
                self._daemon_starting[project] = (client, ready)
                self._daemon_start_errors.pop(project, None)
                owns_start = True
            else:
                client, ready = starting

        # Never hold the global daemon registry lock across cold startup.  UI
        # projections can report daemon_online=false/starting while one project
        # scans instead of timing out behind the 120-second readiness barrier.
        if not owns_start:
            if not ready.wait(timeout=self.limits.max_daemon_start_seconds):
                raise OperationError(
                    "DAEMON_START_FAILED",
                    f"Project '{project}' daemon startup is still pending",
                    details={"project": project, "state": "starting"},
                )
            with self._daemon_lock:
                running = self._daemons.get(project)
                failure = self._daemon_start_errors.get(project, "")
            if running is not None and running.is_running():
                return running
            raise OperationError(
                "DAEMON_START_FAILED",
                failure or f"Project '{project}' daemon did not become ready",
                details={"project": project},
            )

        try:
            # A cold project performs its initial workspace scan before it is
            # ready to accept commands. Keep that timeout separate from the
            # task deadline; progress is forwarded through the listener.
            client.start(timeout=self.limits.max_daemon_start_seconds)
            with self._daemon_lock:
                self._daemons[project] = client
                self._daemon_start_errors.pop(project, None)
            return client
        except Exception as exc:
            diagnostic = client.diagnostic_tail() if hasattr(client, "diagnostic_tail") else ""
            message = f"Project '{project}' daemon failed to start: {exc}"
            with self._daemon_lock:
                self._daemon_start_errors[project] = message
            raise OperationError(
                "DAEMON_START_FAILED",
                message,
                details={"project": project, "diagnostic_tail": diagnostic},
            ) from exc
        finally:
            with self._daemon_lock:
                current = self._daemon_starting.get(project)
                if current is not None and current[0] is client:
                    self._daemon_starting.pop(project, None)
                    current[1].set()

    def _provider_switch(self, provider_id: str) -> dict:
        """Switch the global provider at a safe task boundary.

        Daemons own concrete provider clients, so changing only the metadata
        would leave already-running projects on the old API.  Reject an
        in-flight switch, commit the new encrypted configuration, then retire
        every idle daemon.  The next turn restores the same durable session
        under the selected provider instead of silently creating a new chat.
        """
        with self._daemon_lock:
            if self._root_admissions or self._active_processes or self._active_btw:
                raise OperationError(
                    "PROVIDER_SWITCH_BUSY",
                    "Wait for the active main process/sidecar to reach a safe boundary before switching provider.",
                    details={
                        "active_projects": sorted(self._root_admissions),
                        "active_requests": len(self._active_processes),
                        "active_sidecars": len(self._active_btw),
                    },
                )
            result = self.services.provider_switch(provider_id)
            clients = list(self._daemons.items())
            self._daemons.clear()
        stopped_projects: list[str] = []
        stop_errors: list[dict[str, str]] = []
        for project, client in clients:
            try:
                client.stop()
                stopped_projects.append(project)
            except Exception as exc:
                stop_errors.append({"project": project, "message": str(exc)[:500]})
        self._emit({
            "type": "event", "project": "", "payload": {
                "event": "provider_switched",
                "provider_id": provider_id,
                "runtime_reload": True,
                "retired_projects": stopped_projects,
                "stop_errors": stop_errors,
            },
        })
        return {
            **result,
            "runtime_reload": True,
            "retired_projects": stopped_projects,
            "stop_errors": stop_errors,
        }

    def _on_daemon_event(self, project: str, event: dict) -> None:
        task_id = str(event.get("task_id") or event.get("process_id") or "")
        request_id = ""
        sidecar_id = str(event.get("sidecar_id") or "")
        if sidecar_id:
            request_id = self._btw_requests.get(sidecar_id, ("", ""))[0]
        if task_id:
            request_id = request_id or self._task_requests.get(task_id, ("", ""))[0]
        self._emit({"type": "event", "project": project,
                    "request_id": request_id, "payload": event})

    def _storage_projection(self, project: str, workspace: str):
        """Return a ready full projection or start exactly one background open."""
        with self._projection_lock:
            ready = self._storage_projections.get(project)
            if ready is not None:
                return ready, "", False
            error = self._storage_projection_errors.get(project, "")
            if error:
                return None, error, False
            future = self._storage_projection_inflight.get(project)
            if future is None:
                from backend.core.storage import get_storage
                future = self._summary_pool.submit(get_storage, workspace)
                self._storage_projection_inflight[project] = future

                def completed(done) -> None:
                    try:
                        storage = done.result()
                        failure = ""
                    except Exception as exc:
                        storage = None
                        failure = str(exc)
                    with self._projection_lock:
                        if self._storage_projection_inflight.get(project) is done:
                            self._storage_projection_inflight.pop(project, None)
                        if storage is not None:
                            self._storage_projections[project] = storage
                            self._storage_projection_errors.pop(project, None)
                        else:
                            self._storage_projection_errors[project] = failure
                    self._emit({"type": "event", "project": project, "payload": {
                        "event": "runtime_projection_ready" if storage is not None
                        else "storage_health",
                        "state_available": storage is not None,
                        "message": failure,
                    }})

                future.add_done_callback(completed)
            return None, "", True

    def _runtime_status(self, project: str) -> dict:
        if not project:
            raise OperationError("INVALID_ARGUMENTS", "project is required")
        project_info = next(
            (item for item in self.services.project_list()
             if str(item.get("name", "")) == project),
            None,
        )
        if project_info is None or not project_info.get("workspace"):
            raise OperationError("PROJECT_NOT_FOUND", f"Project '{project}' was not found")
        empty_conversations = {
            "main_conversation": [], "agent_conversations": {},
            "conversation_process_id": "", "session_id": "",
        }
        durable = dict(empty_conversations)
        durable_processes: dict[str, dict] = {}
        storage_projection_errors: list[dict[str, str]] = []
        storage, projection_error, projection_pending = self._storage_projection(
            project, str(project_info["workspace"]),
        )
        if projection_error:
            storage_projection_errors.append({
                "surface": "storage_open", "message": projection_error,
            })

        def read_projection(surface: str, reader, fallback):
            if storage is None:
                return fallback
            try:
                return reader()
            except Exception as exc:
                storage_projection_errors.append({
                    "surface": surface, "message": str(exc),
                })
                return fallback

        if storage is not None:
            durable = read_projection(
                "latest_conversations", storage.read_latest_conversations,
                dict(empty_conversations),
            )
            all_b_conversations = read_projection(
                "project_b_conversations", storage.read_project_b_conversations, {},
            )
            durable["agent_conversations"] = {
                **all_b_conversations,
                **dict(durable.get("agent_conversations") or {}),
            }
            durable_processes = read_projection(
                "project_b_processes", storage.read_project_b_processes, {},
            )
            durable_root = read_projection(
                "latest_root_process", lambda: storage.read_latest_root_process(), None,
            )
            if isinstance(durable_root, dict) and durable_root.get("process_id"):
                durable_processes[str(durable_root["process_id"])] = durable_root
        if durable.get("session_id"):
            self._sessions[project] = str(durable["session_id"])
        if durable.get("conversation_process_id"):
            self._conversation_processes[project] = str(durable["conversation_process_id"])
        client = self._get_daemon(project, start=False)
        if client is None:
            result: dict = {"daemon_online": False, "processes": durable_processes,
                            "recent_tool_executed": [],
                            **durable}
        else:
            try:
                result = client.send_command({"cmd": "loop_status"})
                result["daemon_online"] = True
            except Exception as exc:
                storage_projection_errors.append({
                    "surface": "daemon_loop_status", "message": str(exc),
                })
                result = {
                    "daemon_online": True,
                    "processes": {},
                    "recent_tool_executed": [],
                }
            result["processes"] = {
                **durable_processes,
                **dict(result.get("processes") or {}),
            }
            # SQLite/CAS is the transcript authority.  The daemon status payload
            # supplies live process state only; its in-memory provider transcript
            # must never replace or duplicate the public conversation.
            if storage is not None:
                live_agent_conversations = dict(result.get("agent_conversations") or {})
                result.update(durable)
                result["agent_conversations"] = {
                    **live_agent_conversations,
                    **dict(durable.get("agent_conversations") or {}),
                }
        if storage_projection_errors:
            current_storage = dict(result.get("storage") or {})
            current_reasons = list(current_storage.get("reasons") or [])
            result["storage"] = {
                **current_storage,
                "level": "blocked",
                "reasons": list(dict.fromkeys([
                    *current_reasons, "storage_projection_failed",
                ])),
                "message": "; ".join(
                    f"{item['surface']}: {item['message']}"
                    for item in storage_projection_errors
                ),
                "projection_errors": storage_projection_errors,
            }
        elif projection_pending:
            result["storage"] = {
                "level": "initializing",
                "reasons": ["storage_projection_initializing"],
                "message": "Loading durable conversation and process state",
            }
        provider_state = self.services.provider_status()
        result["providers"] = [
            {"id": provider["id"], "breaker_state": "unsupported",
             "failures": 0, "available": provider.get("api_key_present", False),
             "health_supported": False}
            for provider in provider_state["providers"]
        ]
        result["active_provider"] = provider_state["active_provider"]
        return result

    @staticmethod
    def _counts_from_statuses(statuses) -> dict:
        values = [str(value or "") for value in statuses]
        return {
            "active_process_count": sum(
                value in {"running", "cancelling"} for value in values
            ),
            "waiting_process_count": sum(
                value in {"waiting", "awaiting_user", "recovering", "resume_available"}
                for value in values
            ),
            "finished_process_count": sum(
                value in {"completed", "failed", "timed_out", "cancelled"}
                for value in values
            ),
        }

    def _unavailable_summary(self, name: str, message: str) -> dict:
        with self._summary_lock:
            previous = dict(self._summary_cache.get(name) or {})
        observed_at = str(previous.get("observed_at") or "")
        return {
            **{
                "active_process_count": 0, "waiting_process_count": 0,
                "finished_process_count": 0, "durable_status": "",
            },
            **previous,
            "daemon_online": False,
            "state_available": False,
            "summary_error": str(message)[:500],
            "last_known_at": observed_at,
            "stale": bool(previous),
        }

    def _durable_project_summary(
        self, item: dict, observed_at: float, *, live_error: str = "",
    ) -> tuple[str, dict, bool]:
        """Project-list fallback backed by the authoritative SQLite projection.

        A warm daemon is useful for fresher in-flight state, but it must not be
        a single point of failure for the project table.  A busy/restarting
        daemon can miss the short list deadline while its durable terminal or
        waiting state remains perfectly readable.  Preserve that fact and mark
        it stale instead of turning the whole project into ``Unavailable``.
        """
        name = str(item.get("name") or "")
        try:
            workspace = str(item.get("workspace") or "")
            if not workspace:
                raise ValueError("project workspace is not configured")
            from backend.core.storage import read_project_list_status
            latest = read_project_list_status(workspace)
            status = str((latest or {}).get("status") or "")
            return name, {
                "daemon_online": False,
                "state_available": True,
                "summary_error": str(live_error)[:500],
                "durable_status": status,
                **self._counts_from_statuses([status]),
                "observed_at": observed_at,
                "last_known_at": str((latest or {}).get("updated_at") or ""),
                "stale": bool(live_error),
            }, True
        except Exception as exc:
            message = str(exc)
            if live_error:
                message = f"daemon: {str(live_error)[:240]}; storage: {message}"
            return name, self._unavailable_summary(name, message), False

    def _read_project_summary(self, item: dict) -> tuple[str, dict, bool]:
        """Read one independent projection without ever starting a daemon."""
        name = str(item.get("name") or "")
        observed_at = time.time()
        client = self._get_daemon(name, start=False)
        if client is None:
            return self._durable_project_summary(item, observed_at)
        try:
            status = client.send_command({"cmd": "loop_status"}, timeout=0.75)
            processes = dict(status.get("processes") or {})
            return name, {
                "daemon_online": True, "state_available": True,
                "summary_error": "", "durable_status": "",
                **self._counts_from_statuses(
                    value.get("status") for value in processes.values()
                ),
                "observed_at": observed_at, "last_known_at": "", "stale": False,
            }, True
        except Exception as exc:
            return self._durable_project_summary(
                item, observed_at, live_error=str(exc),
            )

    def _runtime_summaries(self, projects: list[dict] | None = None) -> dict:
        """Return the project-list read model without putting warm data on RPC.

        A cached projection is returned immediately and refreshed in the
        background.  Only projects which have never produced a projection are
        allowed to consume the aggregate cold-start deadline.  Completion of a
        background refresh emits ``project_summary_updated`` when the semantic
        state changed, so the Dashboard remains event-driven without making a
        slow or temporarily unreachable daemon part of the list's critical
        path.
        """
        projects = list(projects if projects is not None else self.services.project_list())
        started = time.perf_counter()
        # A little above one second covers first-open SQLite/schema setup on a
        # normal Windows disk while remaining one aggregate deadline. Twenty
        # slow daemons still cost 1.25s total, never 20 individual timeouts.
        deadline_seconds = 1.25
        if not self._summary_refresh_lock.acquire(blocking=False):
            summaries = {}
            for item in projects:
                name = str(item.get("name") or "")
                with self._summary_lock:
                    cached = dict(self._summary_cache.get(name) or {})
                summaries[name] = cached or self._unavailable_summary(
                    name, "project summary refresh is already running",
                )
            return {"projects": summaries, "refresh_coalesced": True,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
        try:
            def remember(future) -> None:
                try:
                    name, summary, cacheable = future.result()
                except Exception:
                    return
                semantic_keys = (
                    "daemon_online", "state_available", "summary_error",
                    "durable_status", "active_process_count",
                    "waiting_process_count", "finished_process_count", "stale",
                )
                with self._summary_lock:
                    previous = dict(self._summary_cache.get(name) or {})
                    if self._summary_inflight.get(name) is future:
                        self._summary_inflight.pop(name, None)
                    if cacheable:
                        self._summary_cache[name] = dict(summary)
                # Cold/offline SQLite projections may finish after the bounded
                # overview response. Publish the completed read-model fact so
                # the Dashboard refreshes once, instead of waiting for a poll.
                changed = not previous or any(
                    previous.get(key) != summary.get(key) for key in semantic_keys
                )
                if changed:
                    self._emit({
                        "type": "event",
                        "payload": {
                            "event": "project_summary_updated",
                            "project": name,
                            "state_available": bool(summary.get("state_available", False)),
                            # This is the same safe read-model projection the
                            # overview endpoint returns, not a second status
                            # channel. Consumers can update one row directly
                            # instead of querying the whole table again.
                            "summary": dict(summary),
                        },
                    })

            futures = {}
            cold_futures = {}
            summaries: dict[str, dict] = {}
            for item in projects:
                name = str(item.get("name") or "")
                with self._summary_lock:
                    cached = dict(self._summary_cache.get(name) or {})
                    future = self._summary_inflight.get(name)
                    if future is None or future.done():
                        future = self._summary_pool.submit(
                            self._read_project_summary, item,
                        )
                        self._summary_inflight[name] = future
                        future.add_done_callback(remember)
                futures[future] = name
                if cached:
                    summaries[name] = cached
                else:
                    cold_futures[future] = name

            # Warm projections never wait on a daemon.  For first display only,
            # share one small deadline across all missing projects.
            done, pending = wait(tuple(cold_futures), timeout=deadline_seconds)
            for future in done:
                name = cold_futures[future]
                try:
                    _name, summary, _cacheable = future.result()
                    summaries[name] = summary
                except Exception as exc:
                    summaries[name] = self._unavailable_summary(name, str(exc))
            for future in pending:
                name = cold_futures[future]
                summaries[name] = self._unavailable_summary(
                    name, f"project summary exceeded the aggregate {deadline_seconds:g}s deadline",
                )
            return {
                "projects": summaries, "refresh_coalesced": False,
                "deadline_ms": int(deadline_seconds * 1000),
                "timed_out_projects": len(pending),
                "warm_projects": len(projects) - len(cold_futures),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        finally:
            self._summary_refresh_lock.release()

    def _project_overview(self) -> dict:
        """One native round trip for project metadata and status projection."""
        projects = self.services.project_list()
        summary_result = self._runtime_summaries(projects)
        summaries = dict(summary_result.get("projects") or {})
        return {
            **summary_result,
            "projects": [
                {**item, "runtime_summary": dict(
                    summaries.get(str(item.get("name") or "")) or {}
                )}
                for item in projects
            ],
        }

    def _runtime_trace(self, project: str, arguments: dict) -> dict:
        """Read a task trace through the native data plane (never MCP)."""
        if not project:
            raise OperationError("INVALID_ARGUMENTS", "project is required")
        project_info = next(
            (item for item in self.services.project_list()
             if str(item.get("name", "")) == project),
            None,
        )
        if project_info is None or not project_info.get("workspace"):
            raise OperationError("PROJECT_NOT_FOUND", f"Project '{project}' was not found")
        from backend.core.loop.trace import list_traces, read_trace, read_trace_detail
        from backend.core.storage import get_storage
        from pathlib import Path
        workspace = Path(str(project_info["workspace"])).resolve()
        action = str(arguments.get("action") or "read")
        if action == "detail":
            ref = str(arguments.get("ref", ""))
            result = {"ref": ref, "detail": read_trace_detail(workspace, ref)}
        elif action == "list":
            result = {"traces": list_traces(workspace, limit=100)}
        elif action == "summary":
            trace_id = str(arguments.get("trace_id") or arguments.get("task_id") or "")
            if not trace_id:
                raise OperationError("INVALID_ARGUMENTS", "trace_id is required")
            result = get_storage(workspace).read_trace_summary(
                trace_id, process_id=str(arguments.get("process_id") or ""),
            )
        else:
            trace_id = str(arguments.get("trace_id") or arguments.get("task_id") or "")
            if not trace_id:
                raise OperationError("INVALID_ARGUMENTS", "trace_id is required")
            result = read_trace(
                workspace,
                trace_id,
                after_seq=int(arguments.get("after_seq", 0) or 0),
                limit=int(arguments.get("limit", 500) or 500),
                process_id=str(arguments.get("process_id") or ""),
                include_deltas=arguments.get("include_deltas", True) is not False,
            )
        return {"project": project, **result}

    def _runtime_usage(
        self, project: str = "", *, limit: int = 100, cursor: str = "",
    ) -> dict:
        """Read SQLite task usage without starting project daemons."""
        from backend.core.storage import get_storage

        projects = self.services.project_list()
        if project:
            projects = [
                item for item in projects
                if str(item.get("name") or "") == project
            ]
            if not projects:
                raise OperationError("PROJECT_NOT_FOUND", f"Unknown project: {project}")

        aggregate = {
            "task_count": 0, "provider_calls": 0,
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "duration_ms": 0.0, "tool_calls": 0,
        }
        project_rows: list[dict] = []
        detail_tasks: list[dict] = []
        detail_page: dict = {
            "limit": max(1, min(int(limit), 1000)), "cursor": "",
            "next_cursor": None, "has_more": False,
            "scope": "overview_has_no_task_page",
            "summary_scope": "all_configured_projects",
        }
        for item in projects:
            name = str(item.get("name") or "")
            workspace = str(item.get("workspace") or "")
            if not name or not workspace:
                continue
            try:
                usage = get_storage(workspace).read_task_usage(
                    limit=limit, cursor=cursor if project else "",
                )
                summary = dict(usage.get("summary") or {})
                for key in aggregate:
                    aggregate[key] += summary.get(key, 0) or 0
                project_rows.append({"project": name, **summary})
                if project:
                    detail_tasks = list(usage.get("tasks") or [])
                    detail_page = dict(usage.get("page") or detail_page)
            except (OSError, ValueError) as exc:
                project_rows.append({"project": name, "error": str(exc)})
        input_tokens = int(aggregate["input_tokens"])
        aggregate["cache_hit_ratio"] = (
            round(int(aggregate["cache_read_tokens"]) / input_tokens, 4)
            if input_tokens else 0.0
        )
        return {
            "scope": "project" if project else "overview",
            "project": project,
            "summary": aggregate,
            "projects": project_rows,
            "tasks": detail_tasks,
            "page": detail_page,
        }

    def _runtime_chat(
        self, request_id: str, arguments: dict, *, action: str = "chat",
    ) -> dict:
        request_started_monotonic = time.monotonic()
        request_started_at = datetime.now(timezone.utc).isoformat()
        project = str(arguments.get("project", ""))
        # Ordinary chat/recovery owns the one-root admission lease.  A decision
        # is a control message for an already parked process inside that tree,
        # not a competing root task.  It must be allowed through while A is
        # awaiting B/user input; the daemon still verifies the exact
        # process_id + decision_id before resuming anything.
        owns_root_admission = action != "decision"
        if owns_root_admission:
            with self._daemon_lock:
                if project in self._root_admissions:
                    from backend.core.errors import error_payload
                    info = error_payload("SUPERVISOR_BUSY", next_actions=[
                        {"action": "wait_for_current_A_then_retry"},
                        {"action": "interrupt_current_A_with_confirmation"},
                    ])["error_info"]
                    raise OperationError("SUPERVISOR_BUSY", info["message"], details=info)
                self._root_admissions.add(project)
        try:
            return self._runtime_chat_admitted(
                request_id, arguments, action=action,
                request_started_monotonic=request_started_monotonic,
                request_started_at=request_started_at,
            )
        finally:
            with self._daemon_lock:
                if owns_root_admission:
                    self._root_admissions.discard(project)
                admission = self._admission_requests.pop(request_id, None)
                self._cancelled_requests.discard(request_id)
            if admission is not None:
                self._task_requests.pop(admission[1], None)
            self._active_processes.pop(request_id, None)

    def _runtime_chat_admitted(
        self, request_id: str, arguments: dict, *, action: str = "chat",
        request_started_monotonic: float | None = None,
        request_started_at: str = "",
    ) -> dict:
        request_started_monotonic = (
            request_started_monotonic
            if request_started_monotonic is not None else time.monotonic()
        )
        request_started_at = request_started_at or datetime.now(timezone.utc).isoformat()
        project = str(arguments.get("project", ""))
        raw_message = str(arguments.get("message", ""))
        integrity_error = unicode_integrity_error(raw_message)
        if integrity_error:
            raise OperationError(
                "INPUT_ENCODING_CORRUPTED",
                integrity_error + "; the prompt was not sent to the provider",
            )
        message = raw_message
        process_id = str(arguments.get("process_id", ""))
        if not project or (action != "resume" and not message):
            raise OperationError("INVALID_ARGUMENTS", "project and message are required")
        decision_id = str(arguments.get("decision_id", ""))
        session_mode = str(arguments.get("session_mode") or "continue")
        if session_mode not in {"continue", "fresh"}:
            raise OperationError(
                "INVALID_ARGUMENTS", "session_mode must be continue or fresh",
            )
        if session_mode == "fresh" and action != "chat":
            raise OperationError(
                "INVALID_ARGUMENTS", "session_mode=fresh is only valid for runtime.chat",
            )
        if action == "decision" and (
            not process_id or not decision_id or not str(arguments.get("task_id") or "")
        ):
            raise OperationError(
                "INVALID_ARGUMENTS",
                "runtime.decision requires task_id, process_id, and decision_id",
            )
        if action == "resume" and not process_id:
            raise OperationError(
                "INVALID_ARGUMENTS", "runtime.recovery.resume requires process_id",
            )
        supplied_digest = str(arguments.get("message_utf8_sha256") or "").lower()
        actual_digest = hashlib.sha256(message.encode("utf-8", "strict")).hexdigest()
        if supplied_digest and supplied_digest != actual_digest:
            raise OperationError(
                "INPUT_INTEGRITY_MISMATCH",
                "The prompt changed between the terminal and Native Host; it was not sent to the provider",
                details={"expected_sha256": supplied_digest, "actual_sha256": actual_digest},
            )

        project_info = next(
            (item for item in self.services.project_list()
             if str(item.get("name", "")) == project),
            None,
        )
        expected_project_id = str(arguments.get("expected_project_id") or "")
        expected_workspace = str(arguments.get("expected_workspace") or "")
        if (project_info is None or not project_info.get("workspace")) and (
            expected_project_id or expected_workspace
        ):
            raise OperationError("PROJECT_NOT_FOUND", f"Project '{project}' was not found")
        workspace = (
            os.path.normcase(os.path.realpath(str(project_info["workspace"])))
            if project_info and project_info.get("workspace") else ""
        )
        existing_paths = None
        if workspace:
            from backend.core.storage import resolve_existing_storage_paths
            try:
                existing_paths = resolve_existing_storage_paths(workspace)
            except (OSError, ValueError):
                existing_paths = None
        project_id = str(existing_paths.project_id if existing_paths is not None else "")
        if expected_project_id and expected_project_id != project_id:
            raise OperationError(
                "PROJECT_IDENTITY_MISMATCH",
                "The active Dashboard project no longer matches the Host project identity",
                details={"project": project, "expected_project_id": expected_project_id,
                         "actual_project_id": project_id},
            )
        if expected_workspace and os.path.normcase(os.path.realpath(expected_workspace)) != workspace:
            raise OperationError(
                "PROJECT_WORKSPACE_MISMATCH",
                "The active Dashboard workspace no longer matches the Host project workspace",
                details={"project": project, "expected_workspace": expected_workspace,
                         "actual_workspace": workspace},
            )

        task_id = str(arguments.get("task_id", "")) or str(uuid.uuid4())
        hard_seconds = max(self.limits.max_task_seconds, self.limits.max_task_hard_seconds)
        self._task_requests[task_id] = (request_id, project)
        with self._daemon_lock:
            self._admission_requests[request_id] = (project, task_id)
        # Transport acceptance is deliberately earlier than project recovery,
        # governance compilation and daemon process admission.  The Dashboard
        # can now distinguish a live accepted request from an unresponsive Host.
        self._emit({
            "type": "event", "project": project, "request_id": request_id,
            "payload": {
                "event": "runtime_ack", "stage": "accepted",
                "task_id": task_id, "process_id": "",
                "project_id": project_id, "workspace": workspace,
                "wait_timeout_seconds": hard_seconds + 30,
            },
        })
        if session_mode == "fresh":
            # Explicit fresh admission is used by isolated evaluations and can
            # later back a user-visible New Session command.  Never let an old
            # process owner, pending decision or B coordination leak into it.
            self._sessions.pop(project, None)
            self._conversation_processes.pop(project, None)
        if session_mode == "continue" and project not in self._sessions:
            if project_info and project_info.get("workspace"):
                from backend.core.storage import get_storage
                durable = get_storage(
                    str(project_info["workspace"])
                ).read_latest_conversations()
                if durable.get("session_id"):
                    self._sessions[project] = str(durable["session_id"])
                if durable.get("conversation_process_id"):
                    self._conversation_processes[project] = str(
                        durable["conversation_process_id"]
                    )
        client = self._get_daemon(project, start=True)
        assert client is not None
        from backend.core.application.chat_admission import classify_chat_admission
        admission = classify_chat_admission(
            message,
            explicit_task_kind=(
                str(arguments.get("task_kind") or "")
                if action == "chat" and not arguments.get("manual_delegation") else "supervisor"
            ),
        )
        max_steps = max(1, min(int(arguments.get("max_steps", self.limits.max_agent_steps)),
                               self.limits.max_agent_steps))
        host_config = self.services.config_get()
        command: dict[str, Any] = {
            "cmd": "task", "action": action, "task_id": task_id,
            "instruction": message, "role": "supervisor",
            "actor_kind": "supervisor",
            "capability_profile_id": admission.capability_profile_id,
            "task_kind": admission.task_kind,
            "admission_reason": admission.reason,
            "max_steps": max_steps,
            "task_description": message[:200],
            "runtime_preferences": {
                "frontend_origin": dict(self.frontend_origin),
                "auto_compact": bool(host_config.get("auto_compact", True)),
                "agent_routing": host_config.get("agent_routing", "owner"),
                "web_search_mode": str(host_config.get("web_search_mode") or "auto"),
                "web_search_endpoint": str(host_config.get("web_search_endpoint") or ""),
                "web_search_engine": str(host_config.get("web_search_engine") or "duckduckgo"),
                # The daemon persists the authoritative outcome.  Carry the
                # Native Host admission boundary into that process so the
                # durable duration matches what the user actually waited for,
                # including cold daemon/session recovery before agent_step.
                "request_started_at_utc": request_started_at,
            },
            "task_budget": {
                "max_agents": self.limits.max_tree_agents,
                "max_provider_calls": self.limits.max_provider_calls,
                "max_output_tokens": self.limits.max_output_tokens,
                "max_seconds": hard_seconds,
                "initial_seconds": self.limits.max_task_seconds if hard_seconds > self.limits.max_task_seconds else 0,
            },
        }
        if "manual_delegation" in arguments:
            if not isinstance(arguments["manual_delegation"], bool) or action != "chat":
                raise OperationError("INVALID_ARGUMENTS", "manual_delegation is a chat-only boolean")
            if arguments["manual_delegation"]:
                command["manual_delegation"] = True
                command["task_kind"] = "supervisor"
                command["admission_reason"] = "explicit_manual_B_creation"
                command["runtime_preferences"]["agent_routing"] = "fresh"
        if action == "decision":
            command["process_id"] = process_id
            command["decision_id"] = decision_id
        elif action == "resume":
            command["process_id"] = process_id
            command["manually_verified"] = bool(arguments.get("manually_verified", False))
            command["verification_note"] = str(arguments.get("verification_note") or "")
        session_id = self._sessions.get(project) if session_mode == "continue" else None
        if session_id:
            command["session_id"] = session_id
            command["conversation_process_id"] = self._conversation_processes.get(project, "")

        def on_ack(ack: dict) -> None:
            process_id = str(ack.get("process_id", ""))
            if process_id:
                self._active_processes[request_id] = (project, process_id)
            self._emit({"type": "event", "project": project,
                        "request_id": request_id,
                        "payload": {"event": "runtime_ack", "stage": "admitted", **ack,
                                    "project_id": project_id, "workspace": workspace,
                                    "wait_timeout_seconds": hard_seconds + 30}})
            if request_id in self._cancelled_requests and process_id:
                self._runtime_stop(
                    project, process_id, reason="cancelled_during_admission", wait_timeout=0,
                )

        try:
            if request_id in self._cancelled_requests:
                raise OperationError(
                    "REQUEST_CANCELLED", "Request was cancelled before daemon admission",
                )
            try:
                complete = client.send_task(
                    command, timeout=hard_seconds + 15, on_ack=on_ack,
                )
            except RuntimeError as exc:
                if session_id and "Session not found" in str(exc):
                    self._sessions.pop(project, None)
                    command.pop("session_id", None)
                    complete = client.send_task(
                        command, timeout=hard_seconds + 15, on_ack=on_ack,
                    )
                elif "timed out" in str(exc).lower():
                    active = self._active_processes.get(request_id)
                    cancellation = None
                    if active is not None:
                        try:
                            cancellation = self._runtime_stop(
                                active[0], active[1], reason="native_host_timeout",
                                wait_timeout=10.0,
                            )
                        except Exception as cancel_exc:
                            # Preserve the primary task timeout.  A daemon whose
                            # event loop is occupied may also fail to acknowledge
                            # cancellation; that is diagnostic detail, not a
                            # replacement error that hides the original cause.
                            cancellation = {
                                "requested": False,
                                "error_code": "CANCELLATION_COMMAND_FAILED",
                                "message": str(cancel_exc),
                            }
                    raise OperationError(
                        "AGENT_TASK_TIMEOUT",
                        "Native Host task wait expired; recursive cancellation was requested",
                        details={"cancellation": cancellation or {}},
                    ) from exc
                elif isinstance(exc, DaemonCommandError):
                    raise OperationError(
                        exc.code, exc.message,
                        details={**exc.details, "error_info": exc.error_info},
                    ) from exc
                else:
                    raise
            outcome = complete.get("outcome")
            if not isinstance(outcome, dict):
                raise OperationError("INVALID_RUNTIME_OUTCOME", "Agent completion omitted outcome")
            new_session = str(complete.get("session_id", ""))
            if not outcome.get("task_id") or not outcome.get("process_id") or not new_session:
                raise OperationError(
                    "INVALID_RUNTIME_OUTCOME",
                    "Agent completion omitted task/process/session identity",
                )
            if action == "resume" and str(outcome.get("process_id")) != process_id:
                raise OperationError(
                    "INVALID_RUNTIME_OUTCOME",
                    "Recovery completion did not belong to the claimed process",
                )
            if outcome.get("status") == "completed" and not outcome.get("llm_used", False):
                raise OperationError(
                    "INVALID_RUNTIME_OUTCOME",
                    "A completed real Agent task must prove that the configured LLM was used",
                )
            # The daemon normally derives this from request_started_at_utc.
            # Keep a Host-side monotonic lower bound as a final guard against
            # wall-clock adjustment or an older daemon that omitted it.
            host_elapsed_ms = max(
                # Even an in-memory/test daemon completes a real admitted
                # request. Preserve a positive lower bound so the frontend
                # never renders a completed turn as zero-time work on coarse
                # Windows monotonic clocks.
                0.01, (time.monotonic() - request_started_monotonic) * 1000,
            )
            agent_elapsed_ms = float(outcome.get("duration_ms", 0.0) or 0.0)
            outcome["duration_ms"] = max(agent_elapsed_ms, host_elapsed_ms)
            timing = dict(dict(outcome.get("metadata") or {}).get("timing") or {})
            timing.update({
                "request_started_at": request_started_at,
                "host_elapsed_ms": round(host_elapsed_ms, 3),
                "agent_elapsed_ms": round(agent_elapsed_ms, 3),
            })
            outcome.setdefault("metadata", {})["timing"] = timing
            if new_session:
                self._sessions[project] = new_session
                self._conversation_processes[project] = str(
                    outcome.get("process_id", "")
                )
            return {"project": project, "task_id": outcome.get("task_id", task_id),
                    "process_id": outcome.get("process_id", ""),
                    "session_id": new_session, "response": outcome.get("response", ""),
                    "status": outcome.get("status", ""), "error": outcome.get("error"),
                    "steps_used": outcome.get("steps_used", 0),
                    "llm_used": bool(outcome.get("llm_used", False)), "outcome": outcome}
        finally:
            self._task_requests.pop(task_id, None)
            self._active_processes.pop(request_id, None)
            with self._daemon_lock:
                self._admission_requests.pop(request_id, None)
                self._cancelled_requests.discard(request_id)

    def _runtime_stop(self, project: str, process_id: str, *,
                      reason: str = "user_cancelled", wait_timeout: float = 0.0) -> dict:
        client = self._get_daemon(project, start=False)
        if client is None:
            raise OperationError("RUNTIME_OFFLINE", f"Project '{project}' runtime is offline")
        result = client.send_command({"cmd": "task", "action": "kill",
                                      "process_id": process_id,
                                      "reason": reason,
                                      "wait_timeout": wait_timeout})
        return {"project": project, "process_id": process_id, **result}

    def _runtime_undo(
        self, project: str, process_id: str = "", *,
        checkpoint_id: str = "", preview_only: bool,
    ) -> dict:
        """Preview or commit an immutable, session-only history rewind."""
        if not project:
            raise OperationError("INVALID_ARGUMENTS", "project is required")
        with self._daemon_lock:
            active = (
                project in self._root_admissions
                or any(p == project for p, _ in self._active_processes.values())
            )
        if active:
            raise OperationError(
                "SESSION_UNDO_PROCESS_TREE_ACTIVE",
                "Stop or finish the active A/B tree before rewinding its session.",
            )
        client = self._get_daemon(project, start=True)
        assert client is not None
        command = {
            "cmd": "task",
            "action": "undo_preview" if preview_only else "undo",
            "process_id": process_id or self._conversation_processes.get(project, ""),
        }
        target_process_id = str(command["process_id"] or "")
        if not preview_only:
            if not checkpoint_id:
                raise OperationError(
                    "SESSION_UNDO_CONFIRMATION_REQUIRED",
                    "Preview the rewind and confirm its checkpoint_id first.",
                )
            command["checkpoint_id"] = checkpoint_id
        try:
            result = client.send_command(command, timeout=30)
        except RuntimeError as exc:
            code = str(exc).split(":", 1)[0] or "SESSION_UNDO_FAILED"
            raise OperationError(code, str(exc)) from exc
        if not preview_only:
            current_root = self._conversation_processes.get(project, "")
            updates_root_conversation = (
                not target_process_id or target_process_id == current_root
            )
            if updates_root_conversation and result.get("session_id"):
                self._sessions[project] = str(result["session_id"])
            if updates_root_conversation and result.get("process_id"):
                self._conversation_processes[project] = str(result["process_id"])
        return {"project": project, **result}

    def _runtime_compact(self, project: str, process_id: str = "", *,
                         decision_id: str = "", choice: str = "") -> dict:
        if not project:
            raise OperationError("INVALID_ARGUMENTS", "project is required")
        # A completed conversation may only exist in SQLite after a Dashboard or
        # Daemon restart.  Starting the project daemon here lets its compact
        # command restore that durable root session instead of making the UI
        # manufacture a throw-away live task first.
        client = self._get_daemon(project, start=True)
        assert client is not None
        result = client.send_command({
            "cmd": "task", "action": "compact", "process_id": process_id,
            **({"decision_id": decision_id, "choice": choice} if decision_id or choice else {}),
        }, timeout=135)
        return {"project": project, **result}

    def _runtime_btw(self, project: str, arguments: dict, request_id: str = "") -> dict:
        if not project or not str(arguments.get("question") or "").strip():
            raise OperationError(
                "INVALID_ARGUMENTS", "runtime.btw requires project and question",
            )
        client = self._get_daemon(project, start=True)
        assert client is not None
        process_ids = list(dict.fromkeys(
            str(item) for item in list(arguments.get("process_ids") or [])
            if str(item)
        ))[:8]
        process_id = str(
            arguments.get("process_id")
            or self._conversation_processes.get(project, "")
        )
        if not process_ids and process_id:
            process_ids = [process_id]
        sidecar_id = str(arguments.get("sidecar_id") or uuid.uuid4())
        self._btw_requests[sidecar_id] = (request_id, project)
        self._active_btw[request_id] = (project, sidecar_id)
        try:
            result = client.send_btw({
                "cmd": "task", "action": "btw",
                "question": str(arguments.get("question") or ""),
                "sidecar_id": sidecar_id,
                "history": list(arguments.get("history") or []),
                "process_id": process_id,
                "process_ids": process_ids,
            }, timeout=135)
            return {"project": project, **result}
        finally:
            self._btw_requests.pop(sidecar_id, None)
            self._active_btw.pop(request_id, None)

    def _runtime_btw_cancel(self, project: str, sidecar_id: str) -> dict:
        client = self._get_daemon(project, start=False)
        if client is None:
            return {"project": project, "sidecar_id": sidecar_id,
                    "cancelled": False, "reason": "runtime_offline"}
        result = client.send_command({
            "cmd": "task", "action": "btw_cancel", "sidecar_id": sidecar_id,
        }, timeout=10)
        return {"project": project, **result}

    def _runtime_btw_note(self, project: str, arguments: dict) -> dict:
        if not project or not str(arguments.get("note") or "").strip():
            raise OperationError(
                "INVALID_ARGUMENTS", "runtime.btw.note requires project and note",
            )
        client = self._get_daemon(project, start=True)
        assert client is not None
        result = client.send_command({
            "cmd": "task", "action": "btw_note",
            "note": str(arguments.get("note") or ""),
            "sidecar_id": str(arguments.get("sidecar_id") or ""),
        })
        return {"project": project, **result}

    def _runtime_feedback(
        self, project: str, process_id: str, message: str, *, request_id: str = "",
    ) -> dict:
        """Instruct a live B or continue its durable session after termination."""
        if not project or not process_id or not message.strip():
            raise OperationError(
                "INVALID_ARGUMENTS",
                "runtime.feedback requires project, process_id, and message",
            )
        client = self._get_daemon(project, start=False)
        if client is None:
            raise OperationError("RUNTIME_OFFLINE", f"Project '{project}' runtime is offline")
        status = client.send_command({"cmd": "loop_status"})
        process = dict((status.get("processes") or {}).get(process_id) or {})
        if not process:
            raise OperationError("PROCESS_NOT_FOUND", f"Process '{process_id}' was not found")
        if str(process.get("status") or "") in {
            "running", "waiting", "awaiting_user", "cancelling", "recovering",
        }:
            result = client.send_command({
                "cmd": "task",
                "action": "instruct",
                "process_id": process_id,
                "instruction": message,
                "actor_kind": "user",
            })
            return {"project": project, "process_id": process_id, **result}

        task_id = str(uuid.uuid4())
        host_config = self.services.config_get()
        hard_seconds = max(self.limits.max_task_seconds, self.limits.max_task_hard_seconds)
        command = {
            "cmd": "task", "action": "chat", "task_id": task_id,
            "process_id": process_id,
            "parent_id": process.get("parent_id"),
            "instruction": message,
            "task_description": message[:200],
            "role": process.get("role") or "worker",
            "actor_kind": process.get("actor_kind") or "worker",
            "capability_profile_id": process.get("capability_profile_id") or "development.workspace",
            "task_kind": process.get("task_kind") or "action",
            "max_steps": max(1, min(int(process.get("max_steps") or self.limits.max_agent_steps), self.limits.max_agent_steps)),
            "fresh_task_budget": True,
            "runtime_preferences": {
                "auto_compact": bool(host_config.get("auto_compact", True)),
                "agent_routing": host_config.get("agent_routing", "owner"),
                "web_search_mode": str(host_config.get("web_search_mode") or "auto"),
                "web_search_endpoint": str(host_config.get("web_search_endpoint") or ""),
                "web_search_engine": str(host_config.get("web_search_engine") or "duckduckgo"),
                "user_direct_continuation": True,
                "frontend_origin": dict(self.frontend_origin),
                "predecessor_process_id": process_id,
            },
            "task_budget": {
                "max_agents": self.limits.max_tree_agents,
                "max_provider_calls": self.limits.max_provider_calls,
                "max_output_tokens": self.limits.max_output_tokens,
                "max_seconds": hard_seconds,
                "initial_seconds": self.limits.max_task_seconds if hard_seconds > self.limits.max_task_seconds else 0,
            },
        }

        def on_ack(ack: dict) -> None:
            self._emit({
                "type": "event", "project": project, "request_id": request_id,
                "payload": {
                    "event": "runtime_ack", "stage": "admitted",
                    **ack, "continued_from_process_id": process_id,
                    "wait_timeout_seconds": hard_seconds + 30,
                },
            })

        complete = client.send_task(command, timeout=hard_seconds + 15, on_ack=on_ack)
        return {
            "project": project,
            "continued_from_process_id": process_id,
            "process_id": str(complete.get("process_id") or ""),
            "outcome": complete.get("outcome"),
        }

    def _runtime_recovery(self, request_id: str, arguments: dict, *, action: str) -> dict:
        """Perform an explicit recovery action; automatic replay is forbidden."""
        project = str(arguments.get("project", ""))
        process_id = str(arguments.get("process_id", ""))
        if not project or not process_id:
            raise OperationError(
                "INVALID_ARGUMENTS", "recovery requires project and process_id",
            )
        client = self._get_daemon(project, start=True)
        assert client is not None
        if action == "discard":
            result = client.send_command({
                "cmd": "task",
                "action": "discard",
                "process_id": process_id,
                "reason": str(arguments.get("reason") or "user_discarded_recovery"),
            })
            return {"project": project, "process_id": process_id, **result}

        status = client.send_command({"cmd": "loop_status"})
        candidate = next(
            (
                item for item in status.get("recovery_candidates", [])
                if isinstance(item, dict) and str(item.get("process_id")) == process_id
            ),
            None,
        )
        if candidate is None:
            raise OperationError(
                "RECOVERY_NOT_AVAILABLE",
                f"Process '{process_id}' is not an explicit recovery candidate",
            )
        return self._runtime_chat(request_id, {
            **arguments,
            "task_id": str(candidate.get("task_id") or arguments.get("task_id") or ""),
            "message": "",
        }, action="resume")

    def _cancel(self, target_request_id: str) -> dict:
        active = self._active_processes.get(target_request_id)
        if active is not None:
            project, process_id = active
            return {"cancelled": True, **self._runtime_stop(
                project, process_id, reason="request_cancelled", wait_timeout=5.0,
            )}
        btw = self._active_btw.get(target_request_id)
        if btw is not None:
            project, sidecar_id = btw
            return self._runtime_btw_cancel(project, sidecar_id)
        with self._daemon_lock:
            admission = self._admission_requests.get(target_request_id)
            if admission is not None:
                self._cancelled_requests.add(target_request_id)
                return {
                    "cancelled": True,
                    "pending_admission": True,
                    "project": admission[0],
                    "task_id": admission[1],
                }
        return {"cancelled": False, "reason": "request_not_active"}

    def _dispatch(self, request: dict) -> Any:
        operation = str(request.get("operation", ""))
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise OperationError("INVALID_ARGUMENTS", "arguments must be an object")
        request_id = str(request.get("request_id", ""))
        if operation == "host.capabilities":
            return {"host_version": HOST_VERSION, "protocol_version": PROTOCOL_VERSION,
                    "operations": list(self.services.operations) + [
                        "host.capabilities", "host.cancel", "runtime.status",
                        "project.overview", "runtime.summaries",
                        "runtime.usage",
                        "runtime.chat", "runtime.decision", "runtime.feedback", "runtime.stop",
                         "runtime.trace", "runtime.compact", "runtime.btw",
                         "runtime.btw.cancel", "runtime.btw.note",
                        "runtime.undo.preview", "runtime.undo",
                        "runtime.recovery.resume",
                        "runtime.recovery.discard"],
                    "provider_protocols": [
                        "openai_chat", "openai_responses", "anthropic_messages",
                    ],
                    "context_model": {
                        "prompt_schema_version": 3,
                        "addressable_objects": True,
                        "append_only_steering": True,
                    },
                    "limits": {"max_agent_steps": self.limits.max_agent_steps,
                               "max_daemon_start_seconds": self.limits.max_daemon_start_seconds,
                               "max_task_seconds": self.limits.max_task_seconds,
                               "max_tree_agents": self.limits.max_tree_agents,
                               "max_provider_calls": self.limits.max_provider_calls,
                               "max_output_tokens": self.limits.max_output_tokens}}
        if operation == "host.cancel":
            return self._cancel(str(arguments.get("request_id", "")))
        if operation == "runtime.status":
            return self._runtime_status(str(arguments.get("project", "")))
        if operation == "runtime.summaries":
            return self._runtime_summaries()
        if operation == "project.overview":
            return self._project_overview()
        if operation == "runtime.usage":
            return self._runtime_usage(
                str(arguments.get("project", "")),
                limit=int(arguments.get("limit", 100) or 100),
                cursor=str(arguments.get("cursor", "") or ""),
            )
        if operation == "runtime.chat":
            return self._runtime_chat(request_id, arguments)
        if operation == "runtime.decision":
            return self._runtime_chat(request_id, arguments, action="decision")
        if operation == "runtime.feedback":
            return self._runtime_feedback(
                str(arguments.get("project", "")),
                str(arguments.get("process_id", "")),
                str(arguments.get("message", "")),
                request_id=request_id,
            )
        if operation == "runtime.recovery.resume":
            return self._runtime_recovery(request_id, arguments, action="resume")
        if operation == "runtime.recovery.discard":
            return self._runtime_recovery(request_id, arguments, action="discard")
        if operation == "runtime.stop":
            return self._runtime_stop(str(arguments.get("project", "")),
                                      str(arguments.get("process_id", "")))
        if operation == "runtime.compact":
            return self._runtime_compact(
                str(arguments.get("project", "")),
                str(arguments.get("process_id", "")),
                decision_id=str(arguments.get("decision_id") or ""),
                choice=str(arguments.get("choice") or ""),
            )
        if operation == "runtime.undo.preview":
            return self._runtime_undo(
                str(arguments.get("project", "")),
                str(arguments.get("process_id", "")),
                preview_only=True,
            )
        if operation == "runtime.undo":
            return self._runtime_undo(
                str(arguments.get("project", "")),
                str(arguments.get("process_id", "")),
                checkpoint_id=str(arguments.get("checkpoint_id", "")),
                preview_only=False,
            )
        if operation == "runtime.btw":
            return self._runtime_btw(
                str(arguments.get("project", "")), arguments, request_id=request_id,
            )
        if operation == "runtime.btw.cancel":
            return self._runtime_btw_cancel(
                str(arguments.get("project", "")),
                str(arguments.get("sidecar_id", "")),
            )
        if operation == "runtime.btw.note":
            return self._runtime_btw_note(
                str(arguments.get("project", "")), arguments,
            )
        if operation == "runtime.trace":
            return self._runtime_trace(
                str(arguments.get("project", "")), arguments,
            )
        if operation == "provider.switch":
            return self._provider_switch(str(arguments.get("provider_id", "")))
        if operation == "lesson.harvest.resolve":
            result = self.services.invoke(operation, arguments)
            if str(result.get("status") or "") == "accepted" and int(result.get("count", 0) or 0) > 0:
                project = str(arguments.get("project") or "")
                self._emit({
                    "type": "event", "project": project, "payload": {
                        "event": "lessons_harvested",
                        "harvest_id": str(result.get("proposal_id") or uuid.uuid4()),
                        "time": datetime.now(timezone.utc).isoformat(),
                        "count": int(result.get("count", 0) or 0),
                        "signal_type": "manual_explicit",
                    },
                })
            return result
        return self.services.invoke(operation, arguments)

    def _handle_request(self, request: dict) -> None:
        request_id = str(request.get("request_id", ""))
        try:
            result = self._dispatch(request)
            self._response(request_id, result=result)
        except OperationError as exc:
            self._response(request_id, error=exc.to_dict())
        except DaemonCommandError as exc:
            self._response(request_id, error={
                "code": exc.code,
                "message": exc.message,
                "details": {**exc.details, "error_info": exc.error_info},
            })
        except Exception as exc:
            from backend.core.storage import StorageCorruptionDetected, StorageRuntimeUnsupported, StorageBlocked
            if isinstance(exc, (StorageBlocked, StorageCorruptionDetected)):
                from backend.core.errors import error_payload
                code = ("STORAGE_CORRUPTION" if isinstance(exc, StorageCorruptionDetected)
                        else getattr(exc, "code", "STORAGE_BLOCKED"))
                details = {"automatic_replacement": False}
                if isinstance(exc, StorageCorruptionDetected):
                    details.update(database=exc.database, reason=exc.reason)
                if isinstance(exc, StorageRuntimeUnsupported):
                    details["sqlite_version"] = exc.version
                payload = error_payload(code, message=str(exc), details=details)
                self._response(request_id, error={
                    "code": code, "message": str(exc), "details": details,
                    "error_info": payload["error_info"],
                })
            else:
                self._response(request_id, error={"code": "INTERNAL_ERROR",
                                                   "message": str(exc), "details": {}})

    def serve_forever(self) -> int:
        self._emit({"type": "host_started", "host_version": HOST_VERSION,
                    "capabilities": ["application-services", "multi-project-runtime",
                                     "stream-events", "request-cancellation"]})
        self._maintenance_thread = threading.Thread(target=self._maintenance_loop, daemon=True,
                                                    name="deletion-maintenance")
        self._maintenance_thread.start()
        try:
            for line in self.stdin:
                if self._stopping.is_set():
                    break
                if not line.strip():
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._emit({"type": "protocol_error", "error": {
                        "code": "MALFORMED_JSON", "message": str(exc)}})
                    continue
                request_id = request.get("request_id")
                if request.get("protocol_version") != PROTOCOL_VERSION:
                    if request_id:
                        self._response(str(request_id), error={
                            "code": "PROTOCOL_VERSION_MISMATCH",
                            "message": f"Expected protocol_version={PROTOCOL_VERSION}",
                            "details": {"supported": [PROTOCOL_VERSION]},
                        })
                    continue
                if request.get("type") != "request" or not request_id:
                    self._emit({"type": "protocol_error", "error": {
                        "code": "INVALID_ENVELOPE", "message": "request_id and type=request are required"}})
                    continue
                self._pool.submit(self._handle_request, request)
        except KeyboardInterrupt:
            return 130
        finally:
            self.close()
        return 0

    def close(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        if self._maintenance_thread is not None:
            self._maintenance_thread.join(timeout=15)
        # Explicitly request cancellation before daemon shutdown. This shortens
        # the window in which closing the terminal could leave paid provider
        # calls alive, while daemon EOF/shutdown remains the final backstop.
        for request_id, (project, process_id) in list(self._active_processes.items()):
            try:
                self._runtime_stop(
                    project, process_id, reason="native_host_closed", wait_timeout=2.0,
                )
            except Exception:
                pass
        for request_id, (project, sidecar_id) in list(self._active_btw.items()):
            try:
                self._runtime_btw_cancel(project, sidecar_id)
            except Exception:
                pass
        with self._daemon_lock:
            clients = list(self._daemons.values())
            self._daemons.clear()
        for client in clients:
            try:
                client.stop()
            except Exception:
                pass
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._summary_pool.shutdown(wait=True, cancel_futures=True)


def main() -> int:
    from backend.core.host_profile_lease import HostProfileLease
    from backend.core.storage.paths import _default_state_home
    try:
        with HostProfileLease(_default_state_home()):
            return NativeHost().serve_forever()
    except (RuntimeError, ValueError, OSError) as error:
        write_utf8_line(sys.stdout, dump_protocol_json({"protocol_version": PROTOCOL_VERSION,
                        "type": "host_rejected", "message": str(error)}))
        print(str(error), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
