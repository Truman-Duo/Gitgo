"""Product release admission; existing item revision, policy and CAS authority."""
from copy import deepcopy

from .release_journal import ACTIVE_RELEASE, plan_for
from .state import PublishError, digest, encoded, find_item, projection

CONTROLLER_COMMANDS = {"release_plan", "release_run", "release_cancel", "release_reconcile"}


class ReleaseControllerMixin:
    def _release_operator(self):
        # The background continuation is a local UI use case. Agent tools
        # retain their existing, individually gated legacy release entry.
        if self.actor != "local_operator":
            raise PublishError("PUBLISH_RELEASE_LOCAL_OPERATOR_REQUIRED")

    def _release_plan(self, *, item_id, expected_revision, package_id, target, notes="", ci_artifacts=None,
                      delivery_recipe="project", signing_key_id="", upgrade_route="direct"):
        from backend.remote.publish_platform import PlatformTarget, PlatformError
        from backend.core.update.validation import UpdateError
        from .distribution import _package, _blob
        from .package import validate_package
        from .release_transport import release_body
        from .package_release_recipe import PackageReleaseRecipe
        from .ci_release_recipe import CIReleaseRecipe
        from .release_sources import resolve_candidates
        from .native_release_recipe import NativeReleaseRecipe
        from .native_release_plan import native_source, prepare_native_plan
        from . import formal
        from .service import text, now
        self._release_operator()
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        control = item.get("release_controller", {})
        previous = control.get("job", {})
        if previous.get("state") in ACTIVE_RELEASE or previous.get("pending_effect"):
            raise PublishError("PUBLISH_RELEASE_RECONCILIATION_REQUIRED")
        package = _package(item, package_id)
        git, oid = self._accepted_source(item)
        if package["source_oid"] != oid:
            raise PublishError("PUBLISH_PACKAGE_STALE")
        validate_package(_blob(self.repo.storage, package["blob_ref"]), package["sha256"],
                         expected_source_oid=oid, expected_tree_oid=package["tree_oid"])
        try:
            target = PlatformTarget.parse(target)
            candidates = resolve_candidates(target, ci_artifacts, oid) if ci_artifacts else []
        except PlatformError as exc:
            raise PublishError(exc.code) from exc
        if ci_artifacts is not None and not isinstance(ci_artifacts, list):
            raise PublishError("PUBLISH_CI_SELECTION_INVALID")
        notes = text(notes, 16000, required=False)
        if notes and formal.privacy([("release-notes.md", "100644", "", notes.encode("utf8"))], formal.policy(self.project))["outcome"] != "passed":
            raise PublishError("PUBLISH_RELEASE_PRIVACY_BLOCKED")
        phases = ["prepared", "draft", "published"] if target.kind == "github" else ["prepared", "published"]
        anonymous = target.kind == "github" and target.instance == "https://github.com"
        if anonymous: phases.append("verified")
        native_supported = anonymous and native_source(git,oid,package["version"])
        if delivery_recipe not in {"project","gitgo.native"}:
            raise PublishError("PUBLISH_RELEASE_RECIPE_INVALID")
        native = None
        if delivery_recipe == "gitgo.native":
            if not native_supported or not candidates:
                raise PublishError("PUBLISH_NATIVE_SOURCE_AND_CI_REQUIRED")
            try:
                native = prepare_native_plan(self.repo.storage,target,package,key_id=signing_key_id,upgrade_route=upgrade_route)
            except (PlatformError,UpdateError) as exc:
                # These are read/validation failures before a saved plan or
                # external mutation, so retire the request as not committed.
                raise PublishError(exc.code) from exc
        value = {"schema_version": 1, "item_id": item_id, "item_binding": item["binding"],
            "recipe": NativeReleaseRecipe.identity if native else CIReleaseRecipe.identity if candidates else PackageReleaseRecipe.identity,
            "package": deepcopy(package), "target": target.descriptor(), "ci_artifacts":candidates,
            "source_oid":oid, "tree_oid":package["tree_oid"], "notes":notes,
            "delivery_recipe":delivery_recipe, **({"native":native} if native else {}),
            "body": release_body(package) + ("\n\n" + notes if notes else ""), "supported_phases": phases,
            "capabilities": {"draft": target.kind == "github", "anonymous_delivery_verification": anonymous,
                             "ci_acquisition": target.kind == "github" and target.instance == "https://github.com",
                             "native_update_recipe":native_supported, "signed_update_feed": bool(native)}}
        binding = digest(value)
        if control.get("plan", {}).get("binding") == binding:
            return {"item": projection(item, ref), "effect_state": "committed"}
        if control:
            item.setdefault("release_controller_history", []).append(deepcopy(control))
        if len(item.get("release_controller_history", [])) > 128:
            raise PublishError("PUBLISH_CAPACITY_REACHED")
        pointer = self.repo.storage.put_blob(encoded(value), media_type="application/json")
        item["release_controller"] = {"schema_version": 1, "plan": {
            "id": binding[:32], "binding": binding, "blob_ref": pointer, "version": package["version"],
            "package_id": package_id, "target": target.descriptor(), "created_at": now(),
            "supported_phases": phases, "capabilities": value["capabilities"],
            "references": [{"blob_ref": row["blob_ref"]} for row in package["assets"]],
        }, "history": []}
        item["revision"] += 1
        return self._save_item(ledger, item, ref)

    def _release_admit(self, item_id, expected_revision, through, *, observe_only=False, job_id=""):
        from .service import now
        self._release_operator()
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        plan = plan_for(self.repo, item)
        if through not in plan["supported_phases"]:
            raise PublishError("PUBLISH_RELEASE_PHASE_UNAVAILABLE")
        control = item["release_controller"]
        previous = control.get("job", {})
        if observe_only:
            if previous.get("id") != job_id:
                raise PublishError("PUBLISH_RELEASE_JOB_CHANGED")
            # The caller takes this same nonblocking lease before admitting
            # recovery. Reads/startup never infer that a worker has stopped.
        elif previous.get("state") in ACTIVE_RELEASE or previous.get("pending_effect") or previous.get("state") == "needs_reconciliation":
            raise PublishError("PUBLISH_RELEASE_RECONCILIATION_REQUIRED")
        if previous:
            if len(control["history"]) >= 128:
                raise PublishError("PUBLISH_CAPACITY_REACHED")
            control["history"].append(deepcopy(previous))
        job = {"id": self.operation_id, "plan_binding": control["plan"]["binding"], "state": "queued",
            "through": through, "observe_only": observe_only, "created_at": now(), "records": [], "latest": {}, "phases": {},
            "admission": {"actor": self.actor, "request_key": self.request_key, "through": through,
                          "plan_binding": control["plan"]["binding"]},
            **{key: deepcopy(previous[key]) for key in ("remote", "remote_state", "pending_effect", "release_url", "acquisitions", "native_prepared") if key in previous}}
        control["job"] = job
        item["revision"] += 1
        return {**self._save_item(ledger, item, ref), "_new_release_job": True}

    def _release_run(self, *, item_id, expected_revision, through="prepared"):
        return self._release_admit(item_id, expected_revision, through)

    def _release_cancel(self, *, item_id, expected_revision, job_id):
        self._release_operator()
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id)
        job = item.get("release_controller", {}).get("job", {})
        if job.get("id") != job_id or expected_revision > item["revision"]:
            raise PublishError("PUBLISH_RELEASE_JOB_CHANGED")
        if job["state"] not in ACTIVE_RELEASE:
            return {"item": projection(item, ref), "effect_state": "committed", "already_terminal": True}
        # Journal updates do not change the authorized cancellation target.
        # The exact job ID permits this action across progress-only revisions.
        job["state"] = "stopping"
        item["revision"] += 1
        return self._save_item(ledger, item, ref)

    def _release_reconcile(self, *, item_id, expected_revision, job_id):
        from backend.core.loop.publish_queue import run_lease
        self._release_operator()
        with run_lease(self.repo.storage, "release-" + item_id):
            ledger, _ = self.repo.load()
            item = find_item(ledger, item_id, expected_revision)
            previous = item.get("release_controller", {}).get("job", {})
            if previous.get("id") != job_id:
                raise PublishError("PUBLISH_RELEASE_JOB_CHANGED")
            return self._release_admit(item_id, expected_revision, previous["through"], observe_only=True, job_id=job_id)
