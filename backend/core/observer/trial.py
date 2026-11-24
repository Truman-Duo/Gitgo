"""Explicit, optional trial admission budget; independent of Agent step budgets."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading

from backend.core.application import OperationError
from .files import atomic_json, private_directory

USER_INPUT_OPERATIONS = frozenset({"runtime.chat", "runtime.decision", "runtime.feedback", "runtime.btw"})


class Trial:
    def __init__(self, plan_path: Path, directory: Path, host):
        if plan_path.stat().st_size > 65536:
            raise ValueError("OBSERVER_TRIAL_PLAN_TOO_LARGE")
        self.plan = json.loads(plan_path.read_text("utf-8"))
        self.project = str(self.plan.get("project") or "")
        self.workspace = Path(str(self.plan.get("workspace") or ""))
        phases = self.plan.get("phases")
        if (not self.project or not self.workspace.is_absolute() or not isinstance(phases, list)
                or not 1 <= len(phases) <= 8 or any(not isinstance(p, dict) or not isinstance(p.get("provider_id"), str) or not p["provider_id"]
                or type(p.get("max_user_turns")) is not int or not 1 <= p["max_user_turns"] <= 1000 for p in phases)):
            raise ValueError("OBSERVER_TRIAL_PLAN_INVALID")
        if directory.resolve().is_relative_to(self.workspace.resolve()):
            raise ValueError("OBSERVER_TRIAL_CAPTURE_PATH_INVALID: keep developer observations outside the trial workspace")
        self.path = Path(str(self.plan.get("state_file") or ""))
        if not self.path.is_absolute() or self.path.resolve().is_relative_to(self.workspace.resolve()):
            raise ValueError("OBSERVER_TRIAL_STATE_PATH_INVALID: use a stable developer-owned file outside the trial workspace")
        private_directory(self.path.parent)
        self.host = host
        self._lock = threading.RLock()
        self._fault = ""
        fingerprint = hashlib.sha256(json.dumps(self.plan, sort_keys=True).encode()).hexdigest()
        self.state = {"schema_version": 1, "plan_sha256": fingerprint, "project": self.project,
                      "counts": [0 for _ in phases], "accepted": {}, "pending": {}}
        if self.path.exists():
            old = json.loads(self.path.read_text("utf-8"))
            if old.get("plan_sha256") != fingerprint:
                raise ValueError("OBSERVER_TRIAL_PLAN_CHANGED: use an explicitly new capture run")
            if old.get("schema_version") != 1 or not isinstance(old.get("pending"), dict):
                raise ValueError("OBSERVER_TRIAL_STATE_FORMAT_UNSUPPORTED")
            if old.get("pending"):
                raise ValueError("OBSERVER_TRIAL_ADMISSION_UNCERTAIN: reconcile the previous request before continuing")
            self.state = old
        atomic_json(self.path, self.state)

    def status(self):
        with self._lock:
            return {**self.state, "counts": list(self.state["counts"]), "count_unit": "admitted_user_input",
                    "pending": {k: dict(v) for k,v in self.state["pending"].items()},
                    "accepted": {k: dict(v) for k,v in self.state["accepted"].items()},
                    "phases": self.plan["phases"], "interventions_counted": True, "error": self._fault,
                    "counted_operations": sorted(USER_INPUT_OPERATIONS)}

    def before(self, request_id: str, arguments: dict, origin: str, operation="runtime.chat"):
        if str(arguments.get("project") or "") != self.project or operation not in USER_INPUT_OPERATIONS:
            return
        with self._lock:
            if self._fault:
                raise OperationError("OBSERVER_TRIAL_STATE_UNAVAILABLE", "Trial counters could not be saved; reconcile evidence before another round.")
            if request_id in self.state["accepted"]:
                raise OperationError("OBSERVER_TRIAL_DUPLICATE_TURN", "This user round was already admitted; inspect its result before retrying.")
            reservations = [sum(p["phase"] == i and not p["admitted"] for p in self.state["pending"].values())
                            for i in range(len(self.plan["phases"]))]
            phase = next((i for i, p in enumerate(self.plan["phases"])
                          if self.state["counts"][i] + reservations[i] < p["max_user_turns"]), None)
            if phase is None:
                raise OperationError("OBSERVER_TRIAL_TURN_LIMIT", "The authorized trial conversation budget is complete; no new request was sent.",
                                     details={"counts": self.state["counts"]})
            info = next((p for p in self.host.services.project_list() if p.get("name") == self.project), None)
            if not info or Path(str(info.get("workspace") or "")).resolve() != self.workspace.resolve():
                raise OperationError("OBSERVER_TRIAL_PROJECT_MISMATCH", "The trial project no longer points to its isolated workspace.")
            if arguments.get("session_mode") == "fresh":
                raise OperationError("OBSERVER_TRIAL_SESSION_RESET_DENIED", "Continue the existing conversation during a model comparison trial.")
            target = self.plan["phases"][phase]["provider_id"]
            # Concurrent BTW/feedback is valid within one phase. Do not change the
            # route while an earlier-phase admission/run is still outstanding.
            if any(p["phase"] < phase for p in self.state["pending"].values()):
                raise OperationError("OBSERVER_TRIAL_BOUNDARY_BUSY", "Wait for the current request to return before switching phase; it was not cancelled.")
            # The original Host owns the safe boundary. A parked decision has
            # no in-flight model request and can resume on the next route.
            self.host._provider_switch(target)
            self.state["pending"][request_id] = {"phase": phase, "admitted": False, "operation": operation, "origin": origin}
            try:
                atomic_json(self.path, self.state)
            except OSError as exc:
                self._fault = "OBSERVER_TRIAL_STATE_WRITE_FAILED"
                raise OperationError(self._fault, "The trial admission intent could not be saved; the request was not sent.") from exc

    def admitted(self, request_id: str, event: dict):
        with self._lock:
            pending = self.state["pending"].get(request_id)
            if not pending or pending["admitted"]:
                return
            phase = pending["phase"]
            pending["admitted"] = True
            self.state["counts"][phase] += 1
            self.state["accepted"][request_id] = {"phase": phase, "operation": pending["operation"], "origin": pending["origin"],
                "outcome": "pending", **{k: event[k] for k in ("task_id", "process_id", "session_id", "sidecar_id") if k in event}}
            self._save_observed()

    def finished(self, request_id: str, reply: dict):
        with self._lock:
            pending = self.state["pending"].get(request_id)
            if not pending:
                return
            if request_id in self.state["accepted"]:
                self.state["accepted"][request_id]["outcome"] = "error" if reply.get("error") else "returned"
                self.state["accepted"][request_id]["result_status"] = (reply.get("result") or {}).get("status")
                if reply.get("error"):
                    self.state["accepted"][request_id]["error_code"] = reply["error"].get("code")
            # A rejection before daemon admission is an attempt, not a consumed round.
            self.state["pending"].pop(request_id, None)
            self._save_observed()

    def _save_observed(self):
        try:
            atomic_json(self.path, self.state)
        except OSError as exc:
            # The runtime has already acted. Do not turn a reporting failure into a replay.
            self._fault = f"OBSERVER_TRIAL_STATE_WRITE_FAILED: {exc}"

