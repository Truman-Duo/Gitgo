"""Runtime-owned Publish continuations. Saved observations never confer authority.

Only a live caller supplies the per-step dispatcher. Startup and reads do not
restart saved work; an interrupted continuation must be reconciled explicitly.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
import threading

from backend.core.publish.state import Repository, PublishError, find_item, projection, digest
from backend.core.publish.service import now
from backend.core.storage import get_storage
from .publish_recovery import item_lease

ACTIVE = {"queued", "running", "stopping"}
_STEP = ContextVar("publish_queue_step", default=None)


def owns_step(workspace, item):
    job = item.get("run", {})
    return _STEP.get() == (workspace, item["id"], job.get("id"))


@contextmanager
def step_scope(workspace, item_id, run_id):
    token = _STEP.set((workspace, item_id, run_id))
    try:
        yield
    finally:
        _STEP.reset(token)


def run_lease(storage, item_id):
    return item_lease(storage, "run-" + item_id)


class PublishQueue:
    """Bounded local-operator worker; Host only assembles and closes it."""
    def __init__(self):
        self._lock = threading.Lock()
        self._pool = None
        self._pending = {}
        self._closed = threading.Event()

    def submit(self, project, arguments, admit):
        # Admission and enqueue are serialized. No durable run is created when
        # this Runtime cannot accept it, and retries never launch a second job.
        key = (project.workspace_path, arguments["request_id"])
        with self._lock:
            if self._closed.is_set():
                raise PublishError("PUBLISH_RUNTIME_CLOSED")
            if key not in self._pending and len(self._pending) >= 16:
                raise PublishError("PUBLISH_QUEUE_FULL")
            result = admit()
            new_run = result.pop("_new_run", False)
            item = result.get("item", {})
            job = item.get("run", {})
            if result.get("error") or job.get("state") != "queued" or key in self._pending:
                return result
            # A replayed command receipt is an old observation. Inspect current
            # state before considering a launch, including after process restart.
            repo = Repository(project.workspace_path, get_storage(project.workspace_path))
            current, _ = repo.load()
            fresh = find_item(current, item["id"]).get("run", {})
            if fresh != job or new_run is not True:
                return result
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="publish-runtime")
            future = self._pool.submit(self._local_drive, project, item["id"], job["id"])
            self._pending[key] = future
        future.add_done_callback(lambda _: self._forget(key))
        return result

    def _forget(self, key):
        with self._lock:
            self._pending.pop(key, None)

    def _local_drive(self, project, item_id, run_id):
        from backend.core.application.services import ApplicationServices
        from backend.core.tools.publish_service import invoke_local_publish

        def dispatch(operation, args):
            current = ApplicationServices._publish_project(project.name)
            if current.workspace_path != project.workspace_path:
                raise PublishError("PUBLISH_CONTEXT_UNAVAILABLE")
            return invoke_local_publish(current, operation, args, cancelled=self._closed.is_set)
        return drive(project.workspace_path, item_id, run_id, dispatch, self._closed.is_set)

    def close(self):
        with self._lock:
            self._closed.set()
            pool = self._pool
        if pool is not None:
            # Each queued job records its stop; a live native check receives
            # cancellation and completes retirement before the worker exits.
            pool.shutdown(wait=True)


def update(repo, item_id, run_id, change):
    for attempt in range(8):
        ledger, ref = repo.load()
        item = find_item(ledger, item_id)
        if item.get("run", {}).get("id") != run_id:
            raise PublishError("PUBLISH_RUN_CHANGED")
        change(item, item["run"])
        item["revision"] += 1
        item["updated_at"] = now()
        try:
            saved = repo.save(ledger, ref)
            return projection(item, saved)
        except PublishError as exc:
            if exc.code != "PUBLISH_REVISION_CONFLICT" or attempt == 7:
                raise


def drive(workspace, item_id, run_id, dispatch, cancelled=lambda: False):
    """Advance at most two reviewed steps through a freshly authorized caller."""
    repo = Repository(workspace, get_storage(workspace))
    with run_lease(repo.storage, item_id):
        try:
            while True:
                ledger, ref = repo.load()
                item = find_item(ledger, item_id)
                job = item.get("run", {})
                if job.get("id") != run_id or job.get("state") not in ACTIVE:
                    return {"item": projection(item, ref), "effect_state": "committed"}
                reason = ""
                if cancelled():
                    reason = "PUBLISH_RUNTIME_STOPPED"
                elif job["state"] == "stopping" or item["status"] == "paused":
                    reason = "PUBLISH_PAUSED"
                elif item["binding"] != job["binding"]:
                    reason = "PUBLISH_PLAN_REVIEW_REQUIRED"
                elif job.get("current"):
                    reason = "PUBLISH_RUN_RECONCILIATION_REQUIRED"
                elif job["next"] == len(job["steps"]):
                    return finish(repo, item_id, run_id, "completed", "")
                elif item["status"] != "prepared":
                    reason = "PUBLISH_RUN_RECONCILIATION_REQUIRED"
                if reason:
                    return finish(repo, item_id, run_id, "blocked", reason)
                index, operation = job["next"], job["steps"][job["next"]]

                def begin(current, run):
                    if current["revision"] != item["revision"] or run["state"] not in {"queued", "running"}:
                        raise PublishError("PUBLISH_REVISION_CONFLICT")
                    run.update(state="running", current={"operation": operation,
                        "request_id": digest({"run": run_id, "step": index})[:32], "started_at": now()})
                item = update(repo, item_id, run_id, begin)
                step = item["run"]["current"]
                args = {"item_id": item_id, "expected_revision": item["revision"], "request_id": step["request_id"]}
                with step_scope(workspace, item_id, run_id):
                    result = dispatch(operation, args)

                def observed(current, run):
                    run.setdefault("observations", []).append({**step,
                        "effect_state": result.get("effect_state", "ambiguous"),
                        "operation_id": result.get("operation_id", ""),
                        "error": result.get("error") or result.get("reason", ""), "observed_at": now()})
                    if result.get("effect_state") == "committed" or result.get("effect_state") == "not_committed":
                        run.update(current=None, next=index + 1)
                item = update(repo, item_id, run_id, observed)
                if result.get("error") or result.get("completion_failed") or result.get("effect_state") != "committed":
                    return finish(repo, item_id, run_id, "blocked", result.get("error") or result.get("reason") or "PUBLISH_RUN_RECONCILIATION_REQUIRED")
                if operation == "verify":
                    from backend.core.publish.state import formal_ready
                    if not formal_ready(item):
                        return finish(repo, item_id, run_id, "blocked", "PUBLISH_CHECKS_REQUIRED")
        except (OSError, RuntimeError, ValueError) as exc:
            # Do not re-dispatch even if a receipt write failed after the effect.
            return finish(repo, item_id, run_id, "blocked", getattr(exc, "code", "PUBLISH_OPERATION_UNCERTAIN"))


def finish(repo, item_id, run_id, state, reason):
    item = update(repo, item_id, run_id, lambda item, job: job.update(state=state, reason=reason, ended_at=now()))
    return {"item": item, "effect_state": "committed"}


def drive_agent(pipeline, ctx, result, execution_id, runtime_metadata):
    """Agent continuations keep the original task; every child reenters the gate."""
    item = result.get("item", {})
    if not result.pop("_new_run", False) or not item.get("run"):
        return result
    owner = (ctx.process.process_id, ctx.process.active_task_id)

    def dispatch(operation, arguments):
        if owner != (ctx.process.process_id, ctx.process.active_task_id):
            raise PublishError("PUBLISH_CONTEXT_UNAVAILABLE")
        name = "publish_" + operation
        tool = (ctx.artifacts.get("tool_catalog") or {}).get(name)
        if tool is None:
            raise PublishError("PUBLISH_AUTHORIZATION_REQUIRED")
        child = pipeline.execute({"name": name, "args": arguments}, tool, ctx,
            execution_id + "__publish_" + item["run"]["id"], item["run"]["steps"].index(operation))
        receipt = dict(child.receipt or {})
        runtime_metadata.setdefault("child_receipts", []).append(receipt)
        if receipt.get("receipt_id") and not any(r.get("receipt_id") == receipt["receipt_id"] for r in ctx.process.tool_receipts):
            ctx.process.tool_receipts.append(receipt)
        cleanup = receipt.get("runtime_cleanup")
        if cleanup and cleanup.get("complete") is not True:
            runtime_metadata["cleanup"] = cleanup
        if child.suspended or child.is_error:
            return {"error": "PUBLISH_AUTHORIZATION_REQUIRED" if child.suspended else child.diagnostics.get("code", "PUBLISH_STEP_FAILED"),
                    "effect_state": receipt.get("effect_state", "ambiguous")}
        return child.data or {"error": "PUBLISH_STEP_RESULT_MISSING", "effect_state": "ambiguous"}

    final = drive(str(ctx.workspace_path), item["id"], item["run"]["id"], dispatch, ctx.is_cancelled)
    return {**result, **final}
