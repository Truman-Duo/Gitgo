"""Shared Publish commands. Callers supply trusted context, never body roles.

The local methods create work branches or conditionally integrate checked
Formal trees. They never push or declare a collaboration issue resolved.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import inspect

from backend.core.storage import get_storage
from backend.core.storage.redaction import redact_for_persistence
from .git import CandidateGit, branch_ref
from .state import Repository, PublishError, digest, identifier, identity, find_item, projection
from .integration import FormalMixin
from .sharing import SharingMixin
from .collaboration import CollaborationMixin
from .distribution import DistributionMixin
from .branches import BranchesMixin


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, maximum, *, required=True):
    if (not isinstance(value, str) or len(value) > maximum or required and not value.strip()
            or any(ord(c) < 32 and c not in "\n\t" for c in value)):
        raise PublishError("PUBLISH_INVALID_ARGUMENTS")
    return value.strip()


class PublishService(FormalMixin, SharingMixin, CollaborationMixin, DistributionMixin, BranchesMixin):
    def __init__(self, project, *, actor, task_id=""):
        self.project = project
        self.workspace = identity(project.workspace_path)
        self.actor, self.task_id = actor, task_id

    def command(self, operation, arguments):
        args = dict(arguments)
        request_id = identifier(args.pop("request_id", None))
        if operation not in {"prepare", "plan", "adopt", "reconcile", "verify", "execute", "pause", "resume", "run", "migrate", "decide", "share", "share_publish", "collaborate", "collaborate_send",
                              "link_task", "package", "export", "tag", "release", "download", "import_package", "distribution_reconcile", "branch", "branch_apply"}:
            raise PublishError("PUBLISH_INVALID_OPERATION")
        if any(str(key).startswith("_") for key in args):
            raise PublishError("PUBLISH_INVALID_ARGUMENTS")
        try:
            inspect.signature(getattr(self, "_" + operation)).bind(**args)
        except TypeError as exc:
            raise PublishError("PUBLISH_INVALID_ARGUMENTS") from exc
        self.effect_started = False
        self.command_kind = operation
        storage = get_storage(self.workspace)
        self.repo = Repository(self.workspace, storage)
        fingerprint = digest({"operation": operation, "arguments": args})
        key = "publish:" + digest({"workspace": self.workspace, "actor": self.actor, "request_id": request_id})
        self.operation_id = digest(key)[:32]
        self.request_key = key
        try:
            claim = storage.claim_command(key, fingerprint, process_id=self.actor if self.actor != "local_operator" else "")
        except ValueError as exc:
            if str(exc) == "COMMAND_IDENTITY_CONFLICT":
                raise PublishError("PUBLISH_REQUEST_CONFLICT") from exc
            raise
        if not claim["admitted"]:
            return claim.get("result") or {"status": "needs_reconciliation", "effect_state": "ambiguous",
                "operation_id": self.operation_id, "request_id": request_id,
                "reason": "PUBLISH_REQUEST_INCOMPLETE"}
        try:
            from contextlib import ExitStack
            from backend.core.loop.publish_recovery import item_lease
            with ExitStack() as guards:
                if operation == "reconcile":
                    from backend.core.loop.publish_queue import run_lease
                    guards.enter_context(run_lease(storage, args["item_id"]))
                if args.get("item_id") and operation != "pause":
                    guards.enter_context(item_lease(storage, args["item_id"]))
                if args.get("item_id"):
                    saved, _ = self.repo.load()
                    pending = find_item(saved, args["item_id"])
                    if pending.get("sharing", {}).get("state") == "pending" and operation not in {"share", "share_publish"}:
                        raise PublishError("PUBLISH_OPERATION_PENDING")
                    if (any(d.get("state") in {"sending", "unknown"} for d in pending.get("collaboration", {}).get("drafts", []))
                            and operation not in {"collaborate", "collaborate_send"}):
                        raise PublishError("PUBLISH_OPERATION_PENDING")
                    if (pending.get("distribution_operation", {}).get("state") in {"intent", "uncertain"}
                            and operation not in {"distribution_reconcile"}
                            and not (operation == "pause" and pending["distribution_operation"].get("kind") == "package")):
                        raise PublishError("PUBLISH_DISTRIBUTION_PENDING")
                    if pending.get("branches", {}).get("plan", {}).get("state") == "pending" and operation not in {"branch", "reconcile"}:
                        raise PublishError("PUBLISH_OPERATION_PENDING")
                result = getattr(self, "_" + operation)(**args)
        except PublishError as exc:
            result = {"error": exc.code, "effect_state": "ambiguous" if self.effect_started else "not_committed"}
        result = {**result, "operation_id": self.operation_id, "request_id": request_id}
        new_run = result.pop("_new_run", False)
        storage.complete_command(key, fingerprint, result)
        return {**result, **({"_new_run": True} if new_run else {})}

    def _source(self, role):
        if role not in {"workspace", "trial"}:
            raise PublishError("PUBLISH_INVALID_SOURCE")
        path = self.project.workspace_path if role == "workspace" else self.project.trial_path
        if not path:
            raise PublishError("PUBLISH_SOURCE_UNCONFIGURED")
        from backend.core.application.services import _external_workspace
        _external_workspace(path)
        return CandidateGit(path)

    def _share_publish(self, *, item_id, expected_revision):
        return self._share(item_id=item_id, expected_revision=expected_revision, action="publish")

    def _collaborate_send(self, *, item_id, expected_revision, draft_id):
        return self._collaborate(item_id=item_id, expected_revision=expected_revision, action="send", draft_id=draft_id)

    def _save_item(self, ledger, item, ref):
        from backend.core.loop.publish_queue import ACTIVE, owns_step
        job = item.get("run", {})
        if (job and (job.get("state") in ACTIVE or job.get("current"))
                and self.command_kind not in {"run", "pause", "reconcile"}
                and not owns_step(self.workspace, item)):
            raise PublishError("PUBLISH_RUN_ACTIVE")
        from .branches import require_no_dependencies
        for owner in ledger["items"].values():
            plan = owner.get("branches", {}).get("plan", {})
            if owner["id"] != item["id"] and plan.get("state") == "pending":
                require_no_dependencies(ledger, owner["id"], self.workspace, plan)
        item["updated_at"] = now()
        item["last_actor"] = self.actor
        item["task_id"] = self.task_id
        if getattr(self, "legacy_source", None):
            source = self.legacy_source
            existing = ledger.setdefault("legacy_migrations", {}).get(source["key"])
            if existing and existing["item_id"] != item["id"]:
                raise PublishError("PUBLISH_LEGACY_ALREADY_MIGRATED")
            item["legacy_source"] = source
            ledger["legacy_migrations"][source["key"]] = {"item_id": item["id"], "record_digest": source["record_digest"]}
        self.effect_started = True
        saved = self.repo.save(ledger, ref)
        return {"item": projection(item, saved), "effect_state": "committed"}

    def _migrate(self, *, legacy_id, source_role, source_ref, target_branch, owner,
                 destination="formal", method="squash", test_target="", checks=None, rework="",
                 contributions=None, selected_commits=None, paths=None):
        from backend.core.application.publish import snapshot_identity
        from backend.core.sync_session.state import read_state_snapshot
        from .legacy import writer, key, record
        from .state import encoded
        with writer(self.workspace):
            state, state_ref = read_state_snapshot(self.workspace)
            ledger, ledger_ref = self.repo.load()
            snapshot = snapshot_identity(self.project, state, state_ref, ledger_ref)
            parts = legacy_id.split(":") if isinstance(legacy_id, str) else []
            if len(parts) != 3 or parts[:2] != ["legacy", snapshot] or not parts[2].isdigit():
                raise PublishError("PUBLISH_LEGACY_SNAPSHOT_CHANGED")
            index = int(parts[2])
            rows = (state or {}).get("formal_commits", [])
            if index >= len(rows):
                raise PublishError("PUBLISH_LEGACY_SNAPSHOT_CHANGED")
            row = rows[index]
            old_key = key(row)
            if old_key in ledger.get("legacy_migrations", {}):
                raise PublishError("PUBLISH_LEGACY_ALREADY_MIGRATED")
            if sum(key(value) == old_key for value in rows) != 1:
                raise PublishError("PUBLISH_LEGACY_IDENTITY_AMBIGUOUS")
            backup = self.repo.storage.put_blob(encoded({"state": state, "reference": state_ref}), media_type="application/json")
            self.legacy_source = {"key": old_key, "snapshot": snapshot, "index": index,
                "backup_ref": backup, "record_digest": digest(record(row)), "record": record(row),
                "attribution": "operator_supplied_candidate", "imported_at": now()}
            return self._prepare(source_role=source_role, source_ref=source_ref, target_branch=target_branch,
                owner=owner, destination=destination, method=method, test_target=test_target, checks=checks,
                rework=rework, title=row["message"][:512], contributions=contributions, selected_commits=selected_commits, paths=paths)

    def _prepare(self, *, source_role, source_ref, target_branch, owner, title="", base_oid="",
                 rework="", item_id=None, expected_revision=None, destination="work", method="squash", test_target="", checks=None,
                 contributions=None, selected_commits=None, paths=None):
        if destination == "formal":
            return self._prepare_formal(source_role=source_role, source_ref=source_ref, target_branch=target_branch,
                owner=owner, title=title, base_oid=base_oid, rework=rework, item_id=item_id,
                expected_revision=expected_revision, method=method, test_target=test_target, checks=checks,
                contributions=contributions, selected_commits=selected_commits, paths=paths)
        if destination != "work":
            raise PublishError("PUBLISH_INVALID_DESTINATION")
        if contributions or selected_commits or paths:
            raise PublishError("PUBLISH_INVALID_SELECTION")
        owner, rework, title = text(owner, 160), text(rework, 4000, required=False), text(title, 512, required=False)
        target = branch_ref(target_branch)
        ledger, ref = self.repo.load()
        if item_id is not None:
            item = find_item(ledger, item_id, expected_revision)
            if expected_revision is None or item["status"] in {"executing", "checking", "pausing", "paused"}:
                raise PublishError("PUBLISH_OPERATION_PENDING")
            if item["plan"].get("destination") == "formal":
                if not rework:
                    raise PublishError("PUBLISH_REWORK_REQUIRED")
                item.setdefault("handoffs", []).append({"binding": item["binding"], "candidate": deepcopy(item["candidate"]),
                    "plan": deepcopy(item["plan"]), "status": item["status"], "recorded_at": now()})
        else:
            if expected_revision is not None:
                raise PublishError("PUBLISH_INVALID_ARGUMENTS")
            item_id = self.operation_id
            item = {"id": item_id, "revision": 0, "created_at": now(), "evidence": [], "operations": []}
        CandidateGit(self.workspace).require_absent(target)
        candidate, check = self._source(source_role).prepare(source_ref, base_oid)
        candidate["role"] = source_role
        plan = {"method": "new_work_branch", "repository": self.workspace, "target_ref": target,
                "expected_target_oid": None, "automatic_steps": [], "stop_after": "adopt"}
        item.update(candidate=candidate, plan=plan, revision=item["revision"] + 1, status="prepared")
        item.update(redact_for_persistence({"title": title or source_ref + " → " + target_branch,
                                          "owner": owner, "rework": rework}))
        # A new revision invalidates all previous evidence, even for the same tree.
        item["binding"] = digest({"candidate": candidate, "plan": plan, "revision": item["revision"]})
        item["evidence"].append(redact_for_persistence({**check, "binding": item["binding"],
            "id": self.operation_id, "recorded_at": now(), "producer": "protected_git"}))
        ledger["items"][item_id] = item
        return self._save_item(ledger, item, ref)

    def _plan(self, *, item_id, expected_revision, target_branch, owner, rework):
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        if item["status"] != "prepared" or item["plan"].get("destination") == "formal":
            raise PublishError("PUBLISH_OPERATION_PENDING")
        target = branch_ref(target_branch)
        CandidateGit(self.workspace).require_absent(target)
        item.update(redact_for_persistence({"owner": text(owner, 160), "rework": text(rework, 4000, required=False)}))
        if target != item["plan"]["target_ref"]:
            item["plan"] = {**item["plan"], "target_ref": target}
            item["binding"] = digest({"candidate": item["candidate"], "plan": item["plan"], "revision": item["revision"] + 1})
        item["revision"] += 1
        return self._save_item(ledger, item, ref)

    def _decide(self, *, item_id, expected_revision, decision, note, owner):
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        if item["status"] not in {"prepared", "adopted", "integrated", "conflicted"}:
            raise PublishError("PUBLISH_ACTION_UNAVAILABLE")
        if decision not in {"accept", "rework", "defer"}:
            raise PublishError("PUBLISH_INVALID_DECISION")
        if len(item.get("reviews", [])) >= 128:
            raise PublishError("PUBLISH_CAPACITY_REACHED")
        owner, note = text(owner, 160), text(note, 4000)
        item.setdefault("reviews", []).append(redact_for_persistence({"id": self.operation_id,
            "binding": item["binding"], "decision": decision, "note": note, "owner": owner,
            "actor": self.actor, "task_id": self.task_id, "recorded_at": now()}))
        item["owner"] = redact_for_persistence({"owner": owner})["owner"]
        if decision == "rework":
            item["rework"] = redact_for_persistence({"note": note})["note"]
        item["revision"] += 1
        return self._save_item(ledger, item, ref)

    def _adopt(self, *, item_id, expected_revision):
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        if item["status"] != "prepared" or item["plan"].get("destination") == "formal":
            raise PublishError("PUBLISH_OPERATION_PENDING")
        reviews = [r for r in item.get("reviews", []) if r["binding"] == item["binding"]]
        if reviews and reviews[-1]["decision"] == "defer":
            raise PublishError("PUBLISH_REVIEW_REQUIRED")
        checks = [e for e in item["evidence"] if e["binding"] == item["binding"]]
        if not checks:
            raise PublishError("PUBLISH_CHECKS_STALE")
        if any(e["outcome"] == "failed" for e in checks) and not item["rework"]:
            raise PublishError("PUBLISH_REWORK_REQUIRED")
        # Configuration is part of the scope: never follow an old saved path
        # after the operator has assigned the role to another repository.
        if self._source(item["candidate"]["role"]).workspace != item["candidate"]["repository"]:
            raise PublishError("PUBLISH_SOURCE_CHANGED")
        git = CandidateGit(self.workspace)
        git.require_absent(item["plan"]["target_ref"])
        intent = {"id": self.operation_id, "request_key": self.request_key,
            "binding": item["binding"], "candidate": deepcopy(item["candidate"]),
            "plan": deepcopy(item["plan"]), "state": "intent", "created_at": now(),
            "actor": self.actor, "task_id": self.task_id}
        item["operations"].append(intent)
        item["status"], item["revision"] = "executing", item["revision"] + 1
        self._save_item(ledger, item, ref)  # Durable intent BEFORE any Git effect.
        try:
            result = git.adopt(intent["candidate"], intent["plan"]["target_ref"])
        except (PublishError, OSError, RuntimeError) as exc:
            # Import/ref/IO failures may follow a committed Git effect. Keep the
            # intent and require observation, never perform an automatic retry.
            return {"item": projection(item, None), "status": "needs_reconciliation", "effect_state": "ambiguous",
                    "reason": getattr(exc, "code", "PUBLISH_GIT_UNAVAILABLE")}
        return self._observe(item_id, intent["id"], result.returncode == 0)

    def _reconcile(self, *, item_id, expected_revision):
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        if item.get("branches", {}).get("plan", {}).get("state") == "pending":
            return self._branch(item_id=item_id, expected_revision=expected_revision, action="reconcile")
        job = item.get("run", {})
        from backend.core.loop.publish_queue import ACTIVE
        pending_run = job.get("state") in ACTIVE or bool(job.get("current"))
        if item["status"] in {"checking", "pausing", "executing"}:
            result = self._reconcile_effect(item_id=item_id, expected_revision=expected_revision)
            if result.get("effect_state") != "committed" or not pending_run:
                return result
            ledger, ref = self.repo.load()
            item = find_item(ledger, item_id)
        elif not pending_run:
            raise PublishError("PUBLISH_NO_PENDING_OPERATION")
        item["run"].update(state="interrupted", reason="PUBLISH_RUN_RECONCILED", ended_at=now(), current=None)
        item["revision"] += 1
        return self._save_item(ledger, item, ref)

    def _reconcile_effect(self, *, item_id, expected_revision):
        ledger, _ = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        if item["status"] in {"checking", "pausing"}:
            from backend.core.loop.publish_recovery import recover_check
            self.effect_started = True  # Recovery may retire native resources.
            record = recover_check(self.repo.storage, item["operations"][-1])
            # An interrupted attempt is not a pass, even after resource retirement.
            result = record.get("result") if record.get("state") == "finished" else None
            if not result or result.get("cleanup_pending"):
                result = {"error": "PUBLISH_CHECK_INTERRUPTED", "check_results": record.get("check_results", [])}
            return self._finish_check(item_id, item["operations"][-1]["id"], result,
                                      record.get("native_producers", []), recovered=True)
        if item["status"] != "executing":
            raise PublishError("PUBLISH_NO_PENDING_OPERATION")
        if item["operations"][-1].get("kind") == "plugin_completion":
            return self._reconcile_plugin_completion(item_id)
        if item["operations"][-1].get("kind") == "formal" and self._formal_git().workspace != item["plan"]["repository"]:
            raise PublishError("PUBLISH_PLAN_STALE")
        observed = self._observe(item_id, item["operations"][-1]["id"], False)
        if (observed.get("effect_state") == "committed" and item["operations"][-1].get("kind") == "formal"
                and item["operations"][-1].get("completion_required")):
            return self._skip_unstarted_plugin_completion(item_id)
        return observed

    def _pause(self, *, item_id, expected_revision):
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        distribution = item.get("distribution_operation", {})
        if distribution.get("kind") == "package" and distribution.get("state") in {"intent", "uncertain"}:
            distribution.update(cancel_requested=True, cancel_requested_at=now(), cancel_requested_by=self.actor)
            item["revision"] += 1
            return self._save_item(ledger, item, ref)
        from backend.core.loop.publish_queue import ACTIVE
        running = item.get("run", {}).get("state") in ACTIVE
        if item["status"] not in {"prepared", "checking"} and not running:
            raise PublishError("PUBLISH_ACTION_UNAVAILABLE")
        if item["status"] in {"prepared", "checking"}:
            item["status"] = "pausing" if item["status"] == "checking" else "paused"
        if running:
            item["run"].update(state="stopping", reason="PUBLISH_PAUSED")
        item["revision"] += 1
        item["pause"] = {"requested_at": now(), "actor": self.actor, "task_id": self.task_id}
        return self._save_item(ledger, item, ref)

    def _run(self, *, item_id, expected_revision, steps):
        from backend.core.loop.publish_queue import ACTIVE
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        job = item.get("run", {})
        if job.get("state") in ACTIVE or job.get("current"):
            raise PublishError("PUBLISH_RUN_ACTIVE")
        if item["status"] != "prepared":
            raise PublishError("PUBLISH_ACTION_UNAVAILABLE")
        choices = [["verify"], ["execute"], ["verify", "execute"]] if item["plan"].get("destination") == "formal" else [["adopt"]]
        if steps not in choices:
            raise PublishError("PUBLISH_INVALID_STEPS")
        if steps == ["execute"]:
            from .state import formal_ready
            if not formal_ready(item):
                raise PublishError("PUBLISH_CHECKS_REQUIRED")
        if job:
            if len(item.get("run_history", [])) >= 32:
                raise PublishError("PUBLISH_CAPACITY_REACHED")
            item.setdefault("run_history", []).append(job)
        item["run"] = {"id": self.operation_id, "state": "queued", "steps": steps,
            "next": 0, "current": None, "binding": item["binding"], "stop_after": steps[-1],
            "actor": self.actor, "task_id": self.task_id, "created_at": now(), "observations": []}
        item["revision"] += 1
        return {**self._save_item(ledger, item, ref), "_new_run": True}

    def _resume(self, *, item_id, expected_revision):
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        if item["status"] != "paused":
            raise PublishError("PUBLISH_ACTION_UNAVAILABLE")
        item["status"], item["revision"] = "prepared", item["revision"] + 1
        item["pause"].update(resumed_at=now(), resumed_by=self.actor)
        # Resume changes scheduling eligibility only. Verification/integration
        # remain explicit commands with fresh revision and authorization checks.
        return self._save_item(ledger, item, ref)

    def _observe(self, item_id, operation_id, acknowledged):
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id)
        operation = next(o for o in item["operations"] if o["id"] == operation_id)
        if operation["state"] == "observed":
            return {"item": projection(item, ref), "effect_state": "committed"}
        target = CandidateGit(operation["plan"]["repository"]).target(operation["plan"]["target_ref"])
        expected_oid = operation.get("result_oid") or operation["candidate"]["oid"]
        if not target or target["symbolic"] or target["oid"] != expected_oid:
            return {"item": projection(item, ref), "status": "needs_reconciliation",
                    "effect_state": "ambiguous", "reason": "PUBLISH_TARGET_RESULT_UNVERIFIED"}
        operation.update(state="observed", result_oid=target["oid"], observed_at=now(),
                         attribution="command_acknowledged" if acknowledged else "matching_ref_observed")
        status = "executing" if operation.get("completion_required") else "integrated"
        item["status"], item["revision"] = (status if operation.get("kind") == "formal" else "adopted"), item["revision"] + 1
        return self._save_item(ledger, item, ref)
