"""Native launch facts in the existing project SQLite/CAS authority.

These records are evidence, never grants or permission to replay an operation.
Pending heads survive checkpoints; completed records leave the pending index
only after their terminal facts and history reference commit together.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

from backend.core.sandbox import SandboxDenied
from .invocation_journal import invocation_journal_root

NAMESPACE = "native_invocations.v1"
_HOST = (os.getpid(), uuid.uuid4().hex)
_ACTIVE: set[tuple[int, str]] = set()
_LOCK = threading.RLock()
_MAX_PENDING = 4096


def _host_identity():
    global _HOST
    if _HOST[0] != os.getpid():
        _HOST = (os.getpid(), uuid.uuid4().hex)
    return _HOST


def pending_native_invocations(storage, *, include_active=False):
    """Read unresolved native facts, independent of business effect/checkpoint.

    No PID probing, task execution, record repair or authority release happens
    here. A process-local live owner can be ignored by concurrent same-Host
    calls; another Host cannot use a PID or a timeout as retirement proof.
    """
    findings, after, count = [], "", 0
    try:
        while True:
            rows = storage.list_state_refs(NAMESPACE, prefix="pending/", after=after, limit=100)
            for row in rows:
                count += 1
                if count > _MAX_PENDING:
                    raise ValueError("native pending index exceeds inspection limit")
                item = json.loads(storage.read_blob(row["value_ref"], repair=False))
                if (type(item) is not dict or item.get("schema_version") != 1
                        or row["key"] != "pending/" + str(item.get("invocation_id", ""))
                        or item.get("project_id") != storage.paths.project_id
                        or item.get("phase") not in {"launching", "started", "finished"}
                        or item.get("launch_state") not in {"unknown", "started", "not_started"}):
                    raise ValueError("native invocation evidence is invalid")
                cleanup = item.get("runtime_cleanup")
                # Final facts can commit just before an interrupted index retirement.
                if item["phase"] == "finished" and (
                        item["launch_state"] == "not_started" or
                        isinstance(cleanup, dict) and cleanup.get("complete") is True
                        and not cleanup.get("issues")):
                    continue
                with _LOCK:
                    active = (os.getpid(), item["invocation_id"]) in _ACTIVE
                if not include_active and active and item.get("host_instance") == _host_identity()[1]:
                    continue
                findings.append({
                    "code": "NATIVE_LAUNCH_UNCONFIRMED" if item["phase"] == "launching" else "NATIVE_CLEANUP_UNCONFIRMED",
                    "invocation_id": item["invocation_id"], "execution_id": item["execution_id"],
                    "call_index": item["call_index"], "process_id": item["process_id"],
                    "task_id": item["task_id"], "tool_name": item["tool_name"],
                    "launch_state": item["launch_state"], "runtime_cleanup": cleanup,
                    "effect_state": "ambiguous", "state": item["phase"],
                    "journal": "native_invocations.v1/" + item["invocation_id"],
                    "updated_at": item["updated_at"],
                })
            if len(rows) < 100:
                return findings
            after = rows[-1]["key"]
    except Exception as exc:
        return findings + [{"code": "INVOCATION_EVIDENCE_UNAVAILABLE", "effect_state": "ambiguous",
                             "state": "unknown", "detail": type(exc).__name__, "updated_at": 0.0}]


class NativeInvocationRecord:
    def __init__(self, storage, workspace, invocation, *, execution_id="", call_index=0, session_id=""):
        invocation_journal_root(workspace, paths=storage.paths)
        self.storage, self.ref = storage, None
        identifier = uuid.uuid4().hex
        self.key = "pending/" + identifier
        self.record = {
            "schema_version": 1, "invocation_id": identifier,
            "project_id": storage.paths.project_id, "execution_id": execution_id,
            "call_index": call_index, "process_id": invocation.owner[0],
            "task_id": invocation.owner[1], "session_id": session_id,
            "host_instance": _host_identity()[1], "host_pid": os.getpid(),
            "tool_name": invocation.handler, "registration": invocation.registration,
            "execution_basis": invocation.digest,
            # Paths/arguments/credentials are deliberately not duplicated here.
            "phase": "launching", "launch_state": "unknown", "runtime_cleanup": None,
            "created_at": time.time(), "updated_at": time.time(),
        }
        self.failed = False
        self.retirement_pending = False

    def _write(self, *, terminal=False):
        self.record["updated_at"] = time.time()
        ref = self.storage.put_blob(json.dumps(self.record, sort_keys=True, allow_nan=False).encode("utf8"),
                                    media_type="application/json")
        if terminal:
            committed = self.storage.commit_state_revision(NAMESPACE, self.key, ref,
                expected_ref=self.ref, revision_key="history/" + self.record["invocation_id"])
        else:
            committed = self.storage.compare_state_ref(NAMESPACE, self.key, ref, expected_ref=self.ref)
        if not committed:
            raise RuntimeError("native invocation evidence owner changed")
        self.ref = ref

    def prepare(self):
        with _LOCK:
            findings = pending_native_invocations(self.storage)
            if findings:
                denial = SandboxDenied("SANDBOX_CLEANUP_PENDING",
                    "Unsettled native invocation evidence requires inspection; no process was started.")
                denial.findings = findings
                raise denial
            self._write()
            _ACTIVE.add((os.getpid(), self.record["invocation_id"]))

    def started(self, pid):
        self.record.update(phase="started", launch_state="started", pid=int(pid))
        self._write()

    def finish(self, observation):
        try:
            launch = "started" if observation.get("started") else observation.get("launch_state", "unknown")
            cleanup = observation.get("cleanup")
            self.record.update(phase="finished", launch_state=launch,
                               runtime_cleanup=cleanup, exit_code=observation.get("exit_code"))
            settled = launch == "not_started" or isinstance(cleanup, dict) and cleanup.get("complete") is True and not cleanup.get("issues")
            self._write(terminal=settled)
            if settled:
                try:
                    if not self.storage.release_state_ref(NAMESPACE, self.key, expected_ref=self.ref):
                        raise RuntimeError("native pending evidence owner changed")
                except Exception:
                    # Terminal facts already committed atomically with history.
                    # Failed index housekeeping cannot turn them into ambiguity.
                    self.retirement_pending = True
        except Exception:
            self.failed = True
        finally:
            with _LOCK:
                _ACTIVE.discard((os.getpid(), self.record["invocation_id"]))
