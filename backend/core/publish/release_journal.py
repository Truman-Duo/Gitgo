"""Release observations in the existing project ledger and owned CAS.

Only small references/status summaries enter the ledger. No file receipt is
adopted as authority, and reading a saved job never launches it.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import re
import time
import threading
from datetime import datetime

from .state import PublishError, encoded, find_item, projection

ACTIVE_RELEASE = {"queued", "running", "stopping"}
MAX_RECORDS = 4096


def control_projection(control):
    """Small saved view; event bodies and reference inventories stay lazy."""
    result = {"schema_version": control.get("schema_version", 1)}
    if control.get("plan"):
        result["plan"] = {key: value for key, value in control["plan"].items() if key != "references"}
    if control.get("job"):
        result["job"] = {key: value for key, value in control["job"].items()
                         if key not in {"records", "latest", "admission", "remote", "result", "acquisitions", "native_prepared"}}
        result["job"]["record_count"] = len(control["job"].get("records", []))
    result["history"] = [{key: job.get(key) for key in ("id", "state", "through", "observe_only", "created_at")}
                         for job in control.get("history", [])]
    return result


def owned_json(storage, reference):
    raw = storage.read_blob(reference)
    if reference != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise PublishError("PUBLISH_RELEASE_STATE_UNAVAILABLE")
    return json.loads(raw)


def plan_for(repo, item):
    from .state import digest
    descriptor = item.get("release_controller", {}).get("plan", {})
    if not descriptor.get("blob_ref"):
        raise PublishError("PUBLISH_RELEASE_PLAN_REQUIRED")
    plan = owned_json(repo.storage, descriptor["blob_ref"])
    if (plan.get("schema_version") != 1 or digest(plan) != descriptor.get("binding")
            or plan.get("item_id") != item["id"] or plan.get("item_binding") != item["binding"]):
        raise PublishError("PUBLISH_RELEASE_PLAN_STALE")
    return plan


class ProjectReleaseJournal:
    def __init__(self, repo, item_id, job_id):
        self.repo, self.item_id, self.job_id = repo, item_id, job_id
        ledger, _ = repo.load()
        item = find_item(ledger, item_id)
        self.plan_binding = item["release_controller"]["plan"]["binding"]
        self._transfer_lock = threading.Lock()
        self._transfer_latest, self._transfer_saved_at = None, float("-inf")
        self._job(item)

    def _job(self, item):
        control = item.get("release_controller", {})
        job = control.get("job", {})
        if (job.get("id") != self.job_id or control.get("plan", {}).get("binding") != self.plan_binding
                or job.get("plan_binding") != self.plan_binding):
            raise PublishError("PUBLISH_RELEASE_JOB_CHANGED")
        return job

    @contextmanager
    def locked(self):
        from backend.core.loop.publish_queue import run_lease
        with run_lease(self.repo.storage, "release-" + self.item_id):
            yield self

    def _update(self, change):
        from backend.core.loop.publish_recovery import item_lease
        from .service import now
        with item_lease(self.repo.storage, self.item_id):
            for attempt in range(8):
                ledger, ref = self.repo.load()
                item = find_item(ledger, self.item_id)
                job = self._job(item)
                with self._transfer_lock:
                    if self._transfer_latest is not None:
                        job["transfer"] = deepcopy(self._transfer_latest)
                change(job)
                job["updated_at"] = item["updated_at"] = now()
                item["revision"] += 1
                try:
                    saved = self.repo.save(ledger, ref)
                    return projection(item, saved)
                except PublishError as exc:
                    if exc.code != "PUBLISH_REVISION_CONFLICT" or attempt == 7:
                        raise

    def current(self):
        ledger, _ = self.repo.load()
        return self._job(find_item(ledger, self.item_id))

    def cancelled(self):
        return self.current().get("state") == "stopping"

    def remote(self):
        pointer = self.current().get("remote")
        return owned_json(self.repo.storage, pointer["blob_ref"]) if pointer else {}

    def save_remote(self, record):
        from backend.core.storage.redaction import redact_for_persistence
        safe = redact_for_persistence(deepcopy(record))
        reference = self.repo.storage.put_blob(encoded(safe), media_type="application/json")
        if self.current().get("remote", {}).get("blob_ref") == reference:
            return
        def update(job):
            job["remote"] = {"blob_ref": reference}
            job["pending_effect"] = safe.get("current")
            job["remote_state"] = safe.get("state", "unverified")
            job["release_url"] = safe.get("url", "")
            job["feed_state"] = safe.get("feed_state", "unverified")
        self._update(update)

    def record(self, name, state, **fields):
        from backend.core.storage.redaction import redact_for_persistence
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", name):
            raise PublishError("PUBLISH_RELEASE_RECORD_INVALID")
        value = redact_for_persistence({"plan": self.plan_binding, "name": name, "state": state,
                                        "time": time.time(), **fields})
        raw = encoded(value)
        if len(raw) > 512 * 1024:
            raise PublishError("PUBLISH_RELEASE_RECORD_LIMIT")
        reference = self.repo.storage.put_blob(raw, media_type="application/json")
        def update(job):
            records = job.setdefault("records", [])
            terminal = name == "run" and state != "running"
            if len(records) >= MAX_RECORDS - (0 if terminal else 1):
                raise PublishError("PUBLISH_RELEASE_RECORD_LIMIT")
            records.append({"blob_ref": reference})
            job.setdefault("latest", {})[name] = {"sequence": len(records), "state": state, "blob_ref": reference}
            if name.startswith("phase-"):
                job.setdefault("phases", {})[name[6:]] = {
                    "state": state, "seconds": fields.get("seconds"), "reason": fields.get("reason", ""),
                }
            if name == "run":
                if job["state"] == "completed" and state != "success":
                    # A later observer failure cannot undo a durable success.
                    return
                if state == "running":
                    if job["state"] != "stopping":
                        job["state"] = "running"
                    if not job.get("started_at"):
                        from .service import now
                        job["started_at"] = now()
                        job["queue_seconds"] = max(0.,(datetime.fromisoformat(job["started_at"])-datetime.fromisoformat(job["created_at"])).total_seconds())
                elif state == "success":
                    job.update(state="completed", reason="", result={"blob_ref": reference})
                else:
                    job.update(state="needs_reconciliation" if job.get("pending_effect") else state,
                               reason=fields.get("reason") or fields.get("code") or
                                      ("PUBLISH_RELEASE_CANCELLED" if state == "cancelled" else "PUBLISH_RELEASE_RECONCILIATION_REQUIRED"))
                if state != "running" and job.get("transfer", {}).get("state") == "running":
                    job["transfer"]["state"] = "stopped"
                if state != "running":
                    from .service import now
                    job["finished_at"] = now()
                    job["elapsed_seconds"] = fields.get("seconds")
        self._update(update)
        return value

    def transfer(self, value):
        # Coalesce across ALL assets, including many short start/end pairs.
        # Each actual journal transition also flushes the newest observation;
        # effect intents and terminal results are never throttled.
        with self._transfer_lock:
            self._transfer_latest = deepcopy(value)
            now = time.monotonic()
            if now-self._transfer_saved_at < 1: return
            self._transfer_saved_at = now
        self._update(lambda job: None)

    def acquisition(self, identity):
        pointer = self.current().get("acquisitions", {}).get(identity)
        return owned_json(self.repo.storage, pointer["blob_ref"]) if pointer else None

    def save_acquisition(self, identity, value, references):
        pointer = self.repo.storage.put_blob(encoded(value), media_type="application/json")
        self._update(lambda job: job.setdefault("acquisitions", {}).update({identity:{
            "blob_ref":pointer, "references":deepcopy(references)}}))

    def native_prepared(self):
        pointer = self.current().get("native_prepared")
        return owned_json(self.repo.storage, pointer["blob_ref"]) if pointer else None

    def save_native_prepared(self, value, references):
        pointer = self.repo.storage.put_blob(encoded(value), media_type="application/json")
        self._update(lambda job: job.update(native_prepared={"blob_ref":pointer, "references":deepcopy(references)}))
