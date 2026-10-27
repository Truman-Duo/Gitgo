"""Publish distribution commands on the existing receipt/CAS authority."""
from copy import deepcopy
import hashlib
from pathlib import Path

from . import formal
from .git import CandidateGit
from .package import package_bytes, recipe as normalize_recipe, validate_package, version_name
from .state import PublishError, digest, encoded, find_item, formal_ready, active_evidence, projection


def _blob(storage, reference):
    raw = storage.read_blob(reference)
    if reference != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
    return raw


def _package(item, package_id):
    row = next((p for p in item.get("packages", []) if p["id"] == package_id), None)
    if row is None:
        raise PublishError("PUBLISH_PACKAGE_NOT_FOUND")
    if row["binding"] != item["binding"]:
        raise PublishError("PUBLISH_PACKAGE_STALE")
    return row


class DistributionMixin:
    def _distribution_item(self, item_id, expected_revision, *, pending=False):
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        operation = item.get("distribution_operation", {})
        if not pending and operation.get("state") in {"intent", "uncertain"}:
            raise PublishError("PUBLISH_DISTRIBUTION_PENDING")
        return ledger, ref, item

    def _accepted_source(self, item):
        if item["status"] != "integrated" or not formal_ready({**item, "status": "prepared"}):
            raise PublishError("PUBLISH_RELEASE_REQUIRES_FORMAL")
        if formal.policy_binding(self.project) != item["plan"]["policy_binding"]:
            raise PublishError("PUBLISH_PLAN_STALE")
        operations = [o for o in item["operations"] if o.get("kind") == "formal" and o["state"] == "observed" and o["binding"] == item["binding"]]
        if not operations:
            raise PublishError("PUBLISH_RELEASE_REQUIRES_FORMAL")
        git, oid = self._formal_git(), operations[-1]["result_oid"]
        if git.workspace != item["plan"]["repository"]:
            raise PublishError("PUBLISH_PLAN_STALE")
        target = git.target(item["plan"]["target_ref"])
        if not target or target["symbolic"] or target["oid"] != oid or git.oid(oid, "tree") != item["plan"]["tree_oid"]:
            raise PublishError("PUBLISH_TARGET_CHANGED")
        return git, oid

    def _distribution_start(self, ledger, ref, item, kind, **fields):
        from .service import now
        if len(item.get("distribution_history", [])) >= 128:
            raise PublishError("PUBLISH_CAPACITY_REACHED")
        if item.get("distribution_operation"):
            item.setdefault("distribution_history", []).append(deepcopy(item["distribution_operation"]))
        operation = {"id": self.operation_id, "kind": kind, "binding": item["binding"], "state": "intent",
            "actor": self.actor, "task_id": self.task_id, "request_key": self.request_key, "created_at": now(), **fields}
        item["distribution_operation"] = operation
        item["revision"] += 1
        self._save_item(ledger, item, ref)
        return operation

    def _distribution_checkpoint(self, item_id, operation):
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id)
        if item.get("distribution_operation", {}).get("id") != operation["id"] or item["binding"] != operation["binding"]:
            raise PublishError("PUBLISH_REVISION_CONFLICT")
        for name in ("cancel_requested", "cancel_requested_at", "cancel_requested_by"):
            if name in item["distribution_operation"]:
                operation[name] = item["distribution_operation"][name]
        item["distribution_operation"] = deepcopy(operation)
        item["revision"] += 1
        return self._save_item(ledger, item, ref)

    def _distribution_uncertain(self, item_id, operation, exc):
        operation.update(state="uncertain", reason=getattr(exc, "code", "PUBLISH_DISTRIBUTION_UNCERTAIN"))
        saved = self._distribution_checkpoint(item_id, operation)
        return {**saved, "effect_state": "ambiguous", "status": "needs_reconciliation", "reason": operation["reason"]}

    def _package(self, *, item_id, expected_revision, version, recipe=None):
        version, recipe = version_name(version), normalize_recipe(recipe)
        ledger, ref, item = self._distribution_item(item_id, expected_revision)
        git, oid = self._accepted_source(item)
        files = formal.tree_files(git, oid)
        names = {row[0] for row in files}
        if recipe and (recipe["entry"] not in names or any(name in names for name in recipe["outputs"])):
            raise PublishError("PUBLISH_INVALID_BUILD")
        binding = digest({"item_id": item_id, "binding": item["binding"], "source_oid": oid, "recipe": recipe})
        previous = ledger.setdefault("versions", {}).get(version)
        if previous and previous["binding"] != binding:
            raise PublishError("PUBLISH_VERSION_CONFLICT")
        if previous and previous.get("package_id"):
            _package(item, previous["package_id"])
            return {"item": projection(item, ref), "package_id": previous["package_id"], "effect_state": "committed"}
        ledger["versions"][version] = {"binding": binding, "item_id": item_id, "source_oid": oid}
        runtime = None
        extra = {}
        if recipe:
            from backend.core.loop.service_runtime import service_runtime
            runtime = service_runtime()
            if runtime is None or not callable(getattr(runtime, "build_tree", None)):
                raise PublishError("PUBLISH_BUILD_RUNTIME_UNAVAILABLE")
            extra = {"runtime_key": "check/" + self.operation_id, "runtime_owner": list(runtime.owner)}
            # Record an unstarted runtime BEFORE the business intent. A crash
            # between intent and launch remains explicitly reconcilable.
            runtime.bind_operation({"id": self.operation_id, "binding": item["binding"], "kind": "package"}, item_id, self.repo)
        operation = self._distribution_start(ledger, ref, item, "package", version=version, source_oid=oid,
            tree_oid=item["plan"]["tree_oid"], recipe=recipe, reservation_binding=binding, **extra)
        result = {"passed": True, "artifacts": []}
        try:
            if runtime:
                result = runtime.build_tree(formal.image(files), recipe)
            return self._finish_package(item_id, operation, result)
        except (OSError, RuntimeError, ValueError) as exc:
            return self._distribution_uncertain(item_id, operation, exc)

    def _finish_package(self, item_id, operation, result):
        from .service import now
        from backend.core.storage.redaction import redact_for_persistence
        operation["build_log"] = redact_for_persistence(str(result.get("output_tail", ""))[-8000:])
        if result.get("environment"):
            operation["build_environment"] = result["environment"]
        if result.get("cleanup_pending"):
            raise PublishError("SANDBOX_CLEANUP_PENDING")
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id)
        existing = next((p for p in item.get("packages", []) if p["id"] == operation["id"]), None)
        if existing:
            _blob(self.repo.storage, existing["blob_ref"])
            operation.update(state="observed", package_id=existing["id"], completed_at=now())
            return {**self._distribution_checkpoint(item_id, operation), "package_id": existing["id"]}
        if item["distribution_operation"].get("cancel_requested"):
            result = {"passed": False, "error": "PUBLISH_BUILD_CANCELLED"}
        if not result.get("passed"):
            operation.update(state="failed", reason=result.get("error", "PUBLISH_BUILD_FAILED"), completed_at=now())
            return self._distribution_checkpoint(item_id, operation)
        git, oid = self._accepted_source(item)
        if oid != operation["source_oid"] or item["binding"] != operation["binding"]:
            raise PublishError("PUBLISH_TARGET_CHANGED")
        artifacts = []
        for row in result.get("artifacts", []):
            raw = _blob(self.repo.storage, row["blob_ref"])
            if hashlib.sha256(raw).hexdigest() != row["sha256"] or len(raw) != row["size"]:
                raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
            artifacts.append((row["path"], raw))
        if artifacts and formal.privacy([(n, "100644", "", b) for n, b in artifacts], formal.policy(self.project))["outcome"] != "passed":
            raise PublishError("PUBLISH_BUILD_PRIVACY_BLOCKED")
        checks = []
        for evidence in active_evidence(item):
            check = {key: evidence[key] for key in ("kind", "outcome", "tree_oid") if key in evidence}
            if evidence.get("environment", {}).get("digest"):
                check["environment_digest"] = evidence["environment"]["digest"]
            if evidence["kind"] == "project_test":
                check["checks"] = [{"check": row["check"], "passed": row.get("passed") is True} for row in evidence.get("checks", [])]
            checks.append(check)
        raw, manifest = package_bytes(git, oid, operation["version"], checks, artifacts=artifacts,
            build_recipe=operation["recipe"], environment=result.get("environment"))
        checksum = hashlib.sha256(raw).hexdigest()
        filename = "gitgo-" + operation["version"] + "-" + checksum[:12] + ".zip"
        manifest_name = "gitgo-" + operation["version"] + ".manifest.json"
        manifest_raw = encoded(manifest)
        sums = (checksum + "  " + filename + "\n" + hashlib.sha256(manifest_raw).hexdigest() + "  " + manifest_name + "\n").encode("ascii")
        assets = []
        for name, content in ((filename, raw), (manifest_name, manifest_raw), ("gitgo-" + operation["version"] + ".sha256", sums)):
            assets.append({"name": name, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
                           "blob_ref": self.repo.storage.put_blob(content)})
        package = {"id": operation["id"], "binding": item["binding"], "source_oid": oid, "tree_oid": manifest["tree_oid"],
            "version": operation["version"], "sha256": checksum, "blob_ref": assets[0]["blob_ref"], "filename": filename,
            "manifest_ref": assets[1]["blob_ref"], "assets": assets, "build": manifest["build"], "created_at": now()}
        item.setdefault("packages", []).append(package)
        ledger["versions"][package["version"]]["package_id"] = package["id"]
        operation.update(state="observed", package_id=package["id"], completed_at=now())
        item["distribution_operation"] = operation
        item["revision"] += 1
        return {**self._save_item(ledger, item, ref), "package_id": package["id"]}

    def _export(self, *, item_id, expected_revision, package_id, destination):
        from .package_io import observe, export_new
        ledger, ref, item = self._distribution_item(item_id, expected_revision)
        package = _package(item, package_id)
        path = Path(destination)
        if not path.is_absolute() or not path.is_dir():
            raise PublishError("PUBLISH_EXPORT_INVALID")
        raw = _blob(self.repo.storage, package["blob_ref"])
        validate_package(raw, package["sha256"], expected_source_oid=package["source_oid"], expected_tree_oid=package["tree_oid"])
        observed = observe(str(path), package["filename"], package["sha256"])
        if observed != "absent":
            raise PublishError("PUBLISH_EXPORT_EXISTS")
        operation = self._distribution_start(ledger, ref, item, "export", package_id=package_id,
            destination=str(path), filename=package["filename"], sha256=package["sha256"])
        try:
            export_new(str(path), package["filename"], raw, operation["id"])
            return self._observe_export(item_id, operation)
        except (OSError, RuntimeError, ValueError) as exc:
            return self._distribution_uncertain(item_id, operation, exc)

    def _observe_export(self, item_id, operation):
        from .package_io import observe
        observed = observe(operation["destination"], operation["filename"], operation["sha256"])
        operation.update(state="observed" if observed == "matching" else "failed",
            reason="PUBLISH_EXPORT_VERIFIED" if observed == "matching" else "PUBLISH_EXPORT_" + observed.upper())
        return self._distribution_checkpoint(item_id, operation)

    def _tag(self, *, item_id, expected_revision, package_id, remote=""):
        from .remote_git import RemoteGit
        ledger, ref, item = self._distribution_item(item_id, expected_revision)
        package = _package(item, package_id)
        git, oid = self._accepted_source(item)
        if oid != package["source_oid"]:
            raise PublishError("PUBLISH_PACKAGE_STALE")
        tag = "refs/tags/" + version_name(package["version"])
        existing = git.target(tag)
        if existing and (existing["symbolic"] or existing["oid"] != oid):
            raise PublishError("PUBLISH_TAG_CONFLICT")
        observed = RemoteGit(git.workspace, remote).observe(tag) if remote else None
        if observed and observed["oid"] not in (None, oid):
            raise PublishError("PUBLISH_TAG_CONFLICT")
        operation = self._distribution_start(ledger, ref, item, "tag", package_id=package_id,
            repository=git.workspace, tag=tag, source_oid=oid, remote=observed)
        try:
            if existing is None:
                changed = git.run(["update-ref", "--no-deref", "-m", "gitgo publish version", tag, oid, "0" * len(oid)])
                if changed.returncode:
                    raise PublishError("PUBLISH_TAG_CONFLICT")
            if observed and observed["oid"] != oid:
                result = RemoteGit(git.workspace, remote).update(observed, oid)
                if result.returncode:
                    raise PublishError("PUBLISH_TAG_PUSH_UNCERTAIN")
            return self._observe_tag(item_id, operation)
        except (OSError, RuntimeError, ValueError) as exc:
            return self._distribution_uncertain(item_id, operation, exc)

    def _observe_tag(self, item_id, operation):
        from .remote_git import RemoteGit
        local = CandidateGit(operation["repository"]).target(operation["tag"])
        matching = bool(local and not local["symbolic"] and local["oid"] == operation["source_oid"])
        if operation["remote"]:
            remote = operation["remote"]
            actual = RemoteGit(operation["repository"], remote["remote"]).observe(operation["tag"], identity=remote["identity"])
            matching = matching and actual["oid"] == operation["source_oid"]
        operation.update(state="observed" if matching else "failed", reason="PUBLISH_TAG_VERIFIED" if matching else "PUBLISH_TAG_UNVERIFIED")
        return self._distribution_checkpoint(item_id, operation)

    def _release(self, *, item_id, expected_revision, package_id, target):
        from backend.remote.publish_platform import PlatformTarget
        ledger, ref, item = self._distribution_item(item_id, expected_revision)
        package = _package(item, package_id)
        _, oid = self._accepted_source(item)
        if oid != package["source_oid"]:
            raise PublishError("PUBLISH_PACKAGE_STALE")
        target = PlatformTarget.parse(target)
        previous = next((r for r in reversed(item.get("distribution_history", []) + [item.get("distribution_operation", {})])
            if r.get("kind") == "release" and r.get("package_id") == package_id and r.get("target") == target.descriptor()), {})
        operation = self._distribution_start(ledger, ref, item, "release", package_id=package_id, target=target.descriptor(),
            release=deepcopy(previous.get("release", {})))
        return self._drive_distribution_release(item_id, operation, observe_only=False)

    def _drive_distribution_release(self, item_id, operation, *, observe_only):
        from backend.remote.publish_platform import PlatformClient, PlatformTarget
        from backend.remote.publish_accounts import resolve_secret
        from .release_transport import ReleaseTransport, drive_release
        ledger, _ = self.repo.load()
        package = _package(find_item(ledger, item_id), operation["package_id"])
        assets = [{**row, "raw": _blob(self.repo.storage, row["blob_ref"])} for row in package["assets"]]
        for row in assets:
            if hashlib.sha256(row["raw"]).hexdigest() != row["sha256"] or len(row["raw"]) != row["size"]:
                raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
        target = PlatformTarget.parse(operation["target"])
        def checkpoint(record):
            operation["release"] = deepcopy(record)
            self._distribution_checkpoint(item_id, operation)
        try:
            with PlatformClient(target, resolve_secret(target)) as client:
                result = drive_release(ReleaseTransport(client), package, assets, operation["release"], checkpoint, observe_only=observe_only)
            operation.update(state="observed" if result["state"] == "published" else "partial", reason=result["reason"])
            return self._distribution_checkpoint(item_id, operation)
        except (OSError, RuntimeError, ValueError) as exc:
            return self._distribution_uncertain(item_id, operation, exc)

    def _distribution_reconcile(self, *, item_id, expected_revision):
        _, _, item = self._distribution_item(item_id, expected_revision, pending=True)
        operation = deepcopy(item.get("distribution_operation", {}))
        if operation.get("state") not in {"intent", "uncertain", "partial"}:
            raise PublishError("PUBLISH_NO_PENDING_OPERATION")
        try:
            if operation["kind"] in {"export", "download"}:
                return self._observe_export(item_id, operation)
            if operation["kind"] == "tag":
                return self._observe_tag(item_id, operation)
            if operation["kind"] == "release":
                return self._drive_distribution_release(item_id, operation, observe_only=True)
            if operation["kind"] == "package":
                if operation.get("recipe"):
                    from backend.core.loop.publish_recovery import recover_check
                    record = recover_check(self.repo.storage, operation)
                    result = record.get("result") if record.get("state") == "finished" else None
                    if not result or result.get("cleanup_pending"):
                        result = {"passed": False, "error": "PUBLISH_BUILD_INTERRUPTED"}
                else:
                    # No project code ever ran. Deterministic CAS computation
                    # can be completed from the immutable saved source intent.
                    result = {"passed": True, "artifacts": []}
                return self._finish_package(item_id, operation, result)
        except (OSError, RuntimeError, ValueError) as exc:
            return self._distribution_uncertain(item_id, operation, exc)
        raise PublishError("PUBLISH_NO_PENDING_OPERATION")

    def _download(self, *, item_id, expected_revision, package_id, target, destination):
        from backend.remote.publish_platform import PlatformClient, PlatformTarget
        from backend.remote.publish_accounts import resolve_secret
        from .release_transport import ReleaseTransport, release_body
        from .package_io import observe, export_new
        ledger, ref, item = self._distribution_item(item_id, expected_revision)
        package = _package(item, package_id)
        destination = Path(destination)
        if not destination.is_absolute() or not destination.is_dir():
            raise PublishError("PUBLISH_EXPORT_INVALID")
        if observe(str(destination), package["filename"], package["sha256"]) != "absent":
            raise PublishError("PUBLISH_EXPORT_EXISTS")
        target = PlatformTarget.parse(target)
        operation = self._distribution_start(ledger, ref, item, "download", package_id=package_id,
            target=target.descriptor(), destination=str(destination), filename=package["filename"], sha256=package["sha256"])
        try:
            with PlatformClient(target, resolve_secret(target)) as client:
                client.verify_identity()
                repository = client.repository()
                transport = ReleaseTransport(client)
                if transport.tag(package["version"]) != package["source_oid"]:
                    raise PublishError("PUBLISH_TAG_CONFLICT")
                release = transport.release(package["version"])
                if release is None or release["body"] != release_body(package) or release["draft"] is not False:
                    raise PublishError("PUBLISH_RELEASE_INCOMPLETE")
                raw = transport.download(release, package["version"], package["filename"])
                if raw is None:
                    raise PublishError("PUBLISH_RELEASE_ASSET_MISSING")
                validate_package(raw, package["sha256"], expected_source_oid=package["source_oid"], expected_tree_oid=package["tree_oid"])
                if transport.tag(package["version"]) != package["source_oid"]:
                    raise PublishError("PUBLISH_TAG_CONFLICT")
                operation.update(repository_id=repository["id"], downloaded_blob_ref=self.repo.storage.put_blob(raw),
                                 verification="downloaded_bytes_and_git_tree")
                self._distribution_checkpoint(item_id, operation)
            export_new(str(destination), package["filename"], raw, operation["id"])
            return self._observe_export(item_id, operation)
        except (OSError, RuntimeError, ValueError) as exc:
            return self._distribution_uncertain(item_id, operation, exc)

    def _import_package(self, *, item_id, expected_revision, path, sha256):
        from .package_io import read_file
        ledger, ref, item = self._distribution_item(item_id, expected_revision)
        source = Path(path)
        if not source.is_absolute():
            raise PublishError("PUBLISH_EXPORT_INVALID")
        raw = read_file(str(source.parent), source.name)
        expected_oid = self._accepted_source(item)[1]
        manifest = validate_package(raw, sha256, expected_source_oid=expected_oid, expected_tree_oid=item["plan"].get("tree_oid"))
        imports = item.setdefault("package_imports", [])
        if len(imports) >= 128:
            raise PublishError("PUBLISH_CAPACITY_REACHED")
        imports.append({"id": self.operation_id, "sha256": sha256, "source_oid": manifest["source_oid"],
            "tree_oid": manifest["tree_oid"], "version": manifest["version"], "blob_ref": self.repo.storage.put_blob(raw),
            "verification": "digest_and_git_tree", "installation": "not_performed"})
        item["revision"] += 1
        return self._save_item(ledger, item, ref)
