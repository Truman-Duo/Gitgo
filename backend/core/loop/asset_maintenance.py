"""Project-scoped maintenance commitments, separate from execution leases."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import uuid

from backend.core.storage import StorageCommitUnknown
from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive


class AssetMaintenanceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


PROJECT_OWNER = "project"
ASSET_NAMESPACE = "asset_maintenance"
ASSET_ROUTE_LIMIT = 8


def actor_identity(process):
    return PROJECT_OWNER if process.actor_kind == "supervisor" and not process.parent_id else process.session.session_id


def contract_paths(contract):
    return {entry["path"] for entry in contract.get("deliverables", [])
            if entry.get("kind") == "workspace_file" and entry.get("required", True) and entry.get("path")}


def accepted_paths(asset):
    version = asset["accepted_version"]
    paths = version.get("scope_paths", asset["paths"] if version.get("commit") else version.get("files", {}))
    return set(asset["paths"]).intersection(paths)


def asset_retry_scope(previous, binding, assignment):
    """Only a current maintenance binding can retain concrete approved paths."""
    targets = list(assignment.get("target_files") or [])
    if (binding and binding == previous.runtime_preferences.get("asset_execution")
            and targets and "*" not in targets):
        return targets
    return []


def asset_recovery_call(owner, child, manager):
    """Surface a scoped retry without asking the supervisor to copy the saved work."""
    from .assignment_handoff import recorded_scope_change
    from .lifecycle import is_consumable_terminal

    assignment = owner.delegated_contracts.get(child.process_id) or {}
    binding = child.runtime_preferences.get("asset_execution")
    if (not manager or not manager.owns_child(owner, child) or assignment.get("superseded_by")
            or assignment.get("returned_assignment") or child.status.value != "failed"
            or child.worktree.get("state") != "scope_blocked"
            or not is_consumable_terminal(child, manager)
            or not asset_retry_scope(child, binding, assignment)
            or recorded_scope_change(owner, child.process_id)):
        return None
    store = getattr(manager, "session_store", None)
    if store is None:
        return None
    assets = AssetMaintenance(store._storage)
    _, records = assets.load()
    try:
        asset = assets.asset(records, binding["asset_id"])
        work = assets.require_binding(asset, child, binding)
    except AssetMaintenanceError:
        return None
    if (asset.get("delivery_pending") or work["status"] not in {"offered", "accepted"}
            or work["request_task_id"] != (owner.active_task_id or owner.process_id)):
        return None
    return {"tool": "asset_maintenance", "arguments": {"operation": "wake", "asset_id": binding["asset_id"]}}


def workspace_write_versions(process):
    """The latest canonical file-service write for each path in this task."""
    versions = {}
    for receipt in process.tool_receipts:
        if not (receipt.get("task_id") == process.active_task_id
                and receipt.get("process_id") == process.process_id
                and receipt.get("receipt_id") and receipt.get("succeeded") is True
                and receipt.get("committed") is True and receipt.get("effect_state") == "committed"
                and receipt.get("effect") == "workspace_write" and receipt.get("file_service_result") is True):
            continue
        paths = receipt.get("files") or []
        rows = receipt.get("file_mutations") or (
            [{"path": paths[0], "after_sha256": receipt.get("after_sha256"), "operation": receipt["tool_name"],
              "effect_state": "committed"}] if len(paths) == 1 else [])
        for row in rows:
            path = row.get("path")
            if path not in paths or row.get("effect_state") != "committed":
                continue
            digest = row.get("after_sha256")
            if receipt.get("tool_name") == "promote_agent_changes":
                versions.pop(path, None)
            elif isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                versions[path] = {"sha256": digest, "receipt_id": receipt["receipt_id"],
                                  "process_id": process.process_id, "task_id": process.active_task_id}
            elif digest == "" and row.get("operation") == "delete_file":
                versions[path] = {"sha256": None, "receipt_id": receipt["receipt_id"],
                                  "process_id": process.process_id, "task_id": process.active_task_id}
            else:
                versions[path] = None
    return versions


def maintenance_routes(process, contract, storage, *, observed_paths=None, snapshot_cache=None):
    """Suggest existing owners using exact declared or observed files."""
    paths = contract_paths(contract) if observed_paths is None else observed_paths
    requirements = contract.get("host_requirements") or {}
    if (not paths or storage is None or process.actor_kind != "supervisor" or process.parent_id
            or process.runtime_preferences.get("agent_routing", "owner") != "owner"
            or requirements.get("manual_B_creation") or requirements.get("excluded_process_ids")):
        return []
    service = AssetMaintenance(storage)
    if snapshot_cache is None:
        _, assets = service.load()
    else:
        ref = service.current_ref()
        if "by_path" not in snapshot_cache or snapshot_cache["ref"] != ref:
            by_path = {path: asset for asset in service.read(ref).values()
                       if not asset.get("retired") for path in asset["paths"]}
            snapshot_cache.update(ref=ref, by_path=by_path)
        by_path = snapshot_cache["by_path"]
        assets = {by_path[path]["asset_id"]: by_path[path] for path in sorted(paths & by_path.keys())}
    routes = []
    for asset in assets.values():
        matched = paths.intersection(asset["paths"])
        if not matched or asset["owner"] == PROJECT_OWNER or asset.get("retired"):
            continue
        work = asset.get("work") or {}
        if (work.get("revision", asset["revision"]) == asset["revision"]
                and work.get("request_task_id") == (process.active_task_id or process.process_id)
                and work.get("status") in {"offered", "accepted", "delivered"}):
            continue
        routes.append({"asset_id": asset["asset_id"], "name": asset["name"], "owner": asset["owner_name"],
                       "paths": sorted(matched), "revision": asset["revision"],
                       **({"covers_required_files": matched == paths} if observed_paths is None else {})})
    return routes[:ASSET_ROUTE_LIMIT] if observed_paths is None else routes


class AssetMaintenance:
    def __init__(self, storage):
        self.storage = storage

    @contextmanager
    def fence(self):
        directory = self.storage.paths.state_home / "mutation-locks" / ("assets-" + self.storage.paths.project_id)
        directory.mkdir(parents=True, exist_ok=True)
        with StorageLease(directory, exclusive=True):
            yield

    def current_ref(self):
        return self.storage.get_state_ref(ASSET_NAMESPACE, "current")

    def read(self, head):
        return json.loads(self.storage.read_blob(head)) if head else {}

    def load(self):
        head = self.current_ref()
        return head, self.read(head)

    def save(self, head, assets):
        value = self.storage.put_blob(json.dumps(assets, ensure_ascii=False, sort_keys=True).encode(),
                                      media_type="application/json")
        if not self.storage.commit_state_revision(ASSET_NAMESPACE, "current", value, expected_ref=head,
                                                  revision_key=uuid.uuid4().hex):
            raise AssetMaintenanceError("ASSET_STATE_CHANGED", "Reload the current asset record.")

    @contextmanager
    def workspace_delivery(self, process):
        """Stage scoped receipts and hold the existing writer fences through checkpoint COMMIT."""
        versions = workspace_write_versions(process)
        workspace = self.storage.paths.workspace
        if (not versions or not process.workspace_root or Path(process.workspace_root).resolve() != workspace
                or process.worktree.get("isolated") or not self.current_ref()):
            yield None
            return
        from .task_contract import get_task_contract
        from .validation_evidence import file_version
        from backend.core.tools.file_mutation_batch import file_writer_locks
        from backend.core.tools.workspace_tools import _confined_path

        try:
            with self.fence():
                head, assets = self.load()
                changed = {key: sorted(set(asset["paths"]) & versions.keys()) for key, asset in assets.items()
                           if not asset.get("retired")}
                matched = {key: names for key, names in changed.items() if names
                           and assets[key]["accepted_version"].get("task_id") != process.active_task_id}
                if not matched:
                    yield None
                    return
                for key in matched:
                    asset = assets[key]
                    work = asset.get("work") or {}
                    if asset.get("delivery_pending") or work.get("status") in {"offered", "accepted"}:
                        raise AssetMaintenanceError("ASSET_DELIVERY_PENDING",
                                                    f"Resolve the outstanding delivery for {asset['name']}.")
                names = sorted({path for paths in matched.values() for path in paths})
                try:
                    absolute = {name: _confined_path(workspace, name, must_exist=False) for name in names}
                except (ValueError, OSError) as exc:
                    raise AssetMaintenanceError("ASSET_VERSION_CHANGED",
                                                "An asset path could not be resolved in its workspace.") from exc
                lock_root = str(self.storage.paths.state_home / "mutation-locks")
                with file_writer_locks(workspace, absolute.values(), {"_mutation_lock_root": lock_root}):
                    for name, path in absolute.items():
                        witness = versions[name]
                        try:
                            if _confined_path(workspace, name, must_exist=False) != path:
                                raise ValueError("path changed before file verification")
                            if witness and witness["sha256"] is None:
                                (workspace / name).lstat()
                                actual = ""  # A path still exists, so deletion cannot be certified.
                            else:
                                _, actual, _ = file_version(workspace, name)
                        except FileNotFoundError:
                            actual = None
                        except (OSError, ValueError):
                            actual = ""
                        if witness is None or actual != witness["sha256"]:
                            raise AssetMaintenanceError("ASSET_VERSION_CHANGED",
                                                        f"Cannot match {name} to the task's committed write.")
                    deliveries = []
                    instruction = get_task_contract(process).get("goal") or process.task_description
                    for key, paths in matched.items():
                        asset = assets[key]
                        prior = asset["accepted_version"]
                        covered_paths = accepted_paths(asset)
                        files = {path: record for path, record in prior.get("files", {}).items() if path in covered_paths}
                        files.update({path: versions[path] for path in paths})
                        base_commit = prior.get("commit") or prior.get("base_commit")
                        basis = json.dumps([base_commit, {path: item["sha256"] for path, item in files.items()}],
                                           sort_keys=True, separators=(",", ":"))
                        asset["accepted_version"] = {
                            "basis": "workspace_delivery", "version": "sha256:" + hashlib.sha256(basis.encode()).hexdigest(),
                            "process_id": process.process_id, "task_id": process.active_task_id, "files": files,
                            "instruction": instruction,
                            "scope_paths": sorted(covered_paths.union(paths)),
                            **({"base_commit": base_commit} if base_commit else {}),
                        }
                        deliveries.append(self.delivery_receipt(asset))
                    yield head, json.dumps(assets, ensure_ascii=False, sort_keys=True).encode(), deliveries
        except StorageMaintenanceActive as exc:
            raise AssetMaintenanceError("ASSET_VERSION_BUSY", "The asset or its files are busy at delivery.") from exc

    @staticmethod
    def asset(assets, asset_id):
        if asset_id not in assets:
            raise AssetMaintenanceError("ASSET_NOT_FOUND", "Look up the asset by name or path first.")
        return assets[asset_id]

    @staticmethod
    def visible(asset, identity):
        return identity in {PROJECT_OWNER, asset["owner"], (asset.get("transfer") or {}).get("recipient")}

    def lookup(self, identity, *, query="", asset_id=""):
        _, assets = self.load()
        if asset_id:
            asset = self.asset(assets, asset_id)
            if not self.visible(asset, identity):
                raise AssetMaintenanceError("ASSET_NOT_FOUND", "The asset is unavailable to this execution.")
            return [asset]
        query = query.casefold().strip()
        return [self.summary(asset) for asset in assets.values()
                if not asset.get("retired") and self.visible(asset, identity) and (not query or any(
                    query in field.casefold() for field in [asset["name"], asset["commitment"], *asset["paths"]]))]

    @staticmethod
    def summary(asset):
        work = asset.get("work") or {}
        return {key: asset[key] for key in ("asset_id", "name", "paths", "owner", "owner_name", "revision", "subscribed")} | {
            "process_id": asset.get("owner_process_id", ""),
            "work_state": work.get("status", ""),
            "pending_notices": sum(notice["status"] != "acknowledged" for notice in asset["notices"].values()),
            "queued_notices": sum(notice["status"] == "queued" for notice in asset["notices"].values()),
            "delivered_notices": sum(notice["status"] == "delivered" for notice in asset["notices"].values()),
            "transfer_pending": bool(asset.get("transfer")),
            **({"retired": True} if asset.get("retired") else {}),
        }

    @staticmethod
    def require_open_scope(assets, paths, *, asset_id="", name=""):
        paths = set(paths)
        for asset in assets.values():
            if (asset["asset_id"] != asset_id and not asset.get("retired")
                    and ((name and name.casefold() == asset["name"].casefold()) or paths.intersection(asset["paths"]))):
                raise AssetMaintenanceError("ASSET_SCOPE_EXISTS", "A current asset already owns a path or this name.")

    def register(self, *, name, commitment, paths, decision, owner, owner_name, source):
        head, assets = self.load()
        self.require_open_scope(assets, paths, name=name)
        asset_id = "asset-" + uuid.uuid4().hex[:16]
        asset = {
            "asset_id": asset_id, "name": name, "commitment": commitment, "paths": paths,
            "decision": decision, "owner": owner, "owner_name": owner_name,
            "owner_process_id": source["process_id"] if owner != PROJECT_OWNER else "",
            "revision": 1, "accepted_version": {**source, "scope_paths": list(paths)} if source else {},
            "subscribed": False, "notices": {},
        }
        assets[asset_id] = asset
        self.save(head, assets)
        return self.summary(asset)

    def revise(self, records, asset, *, revision, paths, commitment, decision, retire=False):
        if type(revision) is not int or asset["revision"] != revision:
            raise AssetMaintenanceError("ASSET_STATE_CHANGED", "Read the current responsibility revision before changing it.")
        work = asset.get("work") or {}
        if (asset.get("delivery_pending") or asset.get("transfer")
                or work.get("status") in {"offered", "accepted", "needs_reassignment"}):
            raise AssetMaintenanceError("ASSET_SCOPE_BUSY", "Resolve the current work, delivery or transfer before changing responsibility.")
        if not retire:
            self.require_open_scope(records, paths, asset_id=asset["asset_id"])
            if paths == asset["paths"] and commitment == asset["commitment"] and decision == asset["decision"]:
                return False
        covered_paths = accepted_paths(asset)
        asset.setdefault("scope_history", []).append({
            **{key: asset[key] for key in ("revision", "paths", "commitment", "decision")},
            "accepted_scope_paths": sorted(covered_paths),
        })
        prior = asset["accepted_version"]
        if prior:
            prior["scope_paths"] = sorted(covered_paths.intersection(paths))
        if work:
            work.setdefault("revision", revision)
        asset.update(revision=revision + 1, paths=paths, commitment=commitment, decision=decision)
        if retire:
            asset.update(retired=True, subscribed=False)
        return True

    def notify(self, asset, *, version, message, required=False):
        if not asset["subscribed"] and not required:
            return {"status": "unsubscribed"}, False
        prefix = "scope-notice-" if required else "notice-"
        change_id = prefix + hashlib.sha256(version.encode()).hexdigest()[:16]
        previous = asset["notices"].get(change_id)
        if previous:
            if previous["version"] != version or previous["message"] != message:
                raise AssetMaintenanceError("ASSET_STATE_CHANGED", "This version already has a different notice.")
            return previous, False
        notice = {"notice_id": change_id, "version": version, "message": message, "status": "queued"}
        asset["notices"][change_id] = notice
        return notice, True

    @staticmethod
    def require_binding(asset, process, binding):
        work = asset.get("work") or {}
        if (asset["asset_id"] != binding.get("asset_id")
                or asset.get("retired")
                or asset["revision"] != binding.get("revision") or asset["owner"] != actor_identity(process)
                or work.get("process_id") != process.process_id
                or work.get("task_id") != process.active_task_id):
            raise AssetMaintenanceError("ASSET_EXECUTION_STALE", "The execution no longer holds this asset revision.")
        return work

    def require_accepted(self, process):
        binding = process.runtime_preferences.get("asset_execution")
        if not binding:
            return
        _, assets = self.load()
        asset = self.asset(assets, binding["asset_id"])
        work = self.require_binding(asset, process, binding)
        if work["status"] not in {"accepted", "delivered"}:
            raise AssetMaintenanceError("ASSET_WORK_NOT_ACCEPTED", "Accept the asset work before editing or delivering.")

    def begin_delivery(self, process, promotion_id):
        binding = process.runtime_preferences["asset_execution"]
        head, assets = self.load()
        asset = self.asset(assets, binding["asset_id"])
        work = self.require_binding(asset, process, binding)
        if work["status"] not in {"accepted", "delivered"}:
            raise AssetMaintenanceError("ASSET_WORK_NOT_ACCEPTED", "The maintainer has not accepted this change.")
        commit = process.worktree["result_commit"]
        pending = asset.get("delivery_pending")
        if pending and (pending["commit"] != commit or pending["process_id"] != process.process_id):
            raise AssetMaintenanceError("ASSET_DELIVERY_PENDING", "Reconcile the original delivery first.")
        if pending:
            return pending["promotion_id"]
        if work["status"] == "delivered":
            return work["promotion_id"]
        asset["delivery_pending"] = {"commit": commit, "process_id": process.process_id, "promotion_id": promotion_id}
        self.save(head, assets)
        return promotion_id

    def promote(self, process, worktrees, *, promotion_id, **args):
        promotion_id = self.begin_delivery(process, promotion_id)
        key = worktrees._ref_id(promotion_id)
        head = self.storage.get_state_ref("agent_promotion", key)
        before = json.loads(self.storage.read_blob(head)) if head else {}
        try:
            result = worktrees.promote(promotion_id=promotion_id, **args)
        except StorageCommitUnknown:
            raise
        except Exception:
            # Promotion prepares its journal before writing any workspace files.
            # A previously prepared effect still needs the canonical reconciliation.
            if before.get("state") != "prepared" and self.storage.get_state_ref("agent_promotion", key) == head:
                self.release_unapplied_delivery(process)
            raise
        delivery = self.finish_delivery(process, result, promotion_id)
        return {**result, "asset_delivery": delivery}

    def release_unapplied_delivery(self, process):
        head, assets = self.load()
        binding = process.runtime_preferences["asset_execution"]
        asset = self.asset(assets, binding["asset_id"])
        self.require_binding(asset, process, binding)
        if asset.get("delivery_pending", {}).get("process_id") == process.process_id:
            asset.pop("delivery_pending")
            self.save(head, assets)

    def finish_delivery(self, process, result, promotion_id):
        from .task_contract import get_task_contract

        binding = process.runtime_preferences["asset_execution"]
        head, assets = self.load()
        asset = self.asset(assets, binding["asset_id"])
        work = self.require_binding(asset, process, binding)
        if work["status"] != "delivered":
            files = set(get_task_contract(process).get("target_files") or []).intersection(asset["paths"])
            work.update(status="delivered", promotion_id=promotion_id)
            asset["accepted_version"] = {"commit": result["result_commit"], "process_id": process.process_id,
                                         "task_id": process.active_task_id, "basis": "integrated",
                                         "instruction": work["instruction"], "scope_paths": sorted(accepted_paths(asset) | files)}
            asset.pop("delivery_pending", None)
            self.save(head, assets)
        return self.delivery_receipt(asset)

    @staticmethod
    def delivery_receipt(asset):
        version = asset["accepted_version"]
        return {"asset_id": asset["asset_id"], "owner": asset["owner_name"],
                "process_id": version["process_id"],
                **({"commit": version["commit"]} if "commit" in version else {"version": version["version"]})}
