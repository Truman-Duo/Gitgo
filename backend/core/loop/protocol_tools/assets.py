"""Model-facing operations on durable project maintenance responsibilities."""
import json
from pathlib import Path

from backend.core.errors import error_payload
from backend.core.prompt_templates import load_prompt, render_prompt
from backend.core.storage.maintenance import StorageMaintenanceActive
from ..agent_tool import AgentTool, ToolEffect
from ..asset_maintenance import (
    AssetMaintenance, AssetMaintenanceError, PROJECT_OWNER, actor_identity, contract_paths, workspace_write_versions,
)
from ..execution_contract import data_broker
from ..lifecycle import is_consumable_terminal
from .subagent import _paths


def install(ctx):
    process, manager = ctx.process, ctx.manager
    store = getattr(manager, "session_store", None)

    def service():
        if store is None:
            raise AssetMaintenanceError("ASSET_STORAGE_REQUIRED", "Project state is unavailable.")
        return AssetMaintenance(store._storage)

    def invoke(handler, args):
        try:
            return handler(service(), args)
        except AssetMaintenanceError as exc:
            return error_payload(exc.code, message=str(exc))
        except StorageMaintenanceActive:
            return error_payload("ASSET_STATE_CHANGED", message="The asset is busy. Reload before retrying.")
        except (ValueError, TypeError) as exc:
            return error_payload("INVALID_TOOL_ARGUMENTS", message=str(exc))

    def lookup(assets, args):
        return {"assets": assets.lookup(actor_identity(process), query=str(args.get("query") or ""),
                                        asset_id=str(args.get("asset_id") or ""))}

    def lookup_feedback(_, result):
        # The stored decision describes registration. Its historical wording
        # must not be mistaken for the latest committed delivery.
        written = workspace_write_versions(process) if actor_identity(process) == PROJECT_OWNER else {}
        for asset in result.get("assets", []):
            if "decision" not in asset:
                continue
            decision_key = "scope_decision" if asset.get("scope_history") else "registration_decision"
            asset[decision_key] = asset.pop("decision")
            paths = sorted(path for path in asset["paths"] if written.get(path))
            if paths:
                asset["current_task_write_paths"] = paths
                result["delivery_timing"] = load_prompt("runtime/asset_status_pending_delivery").strip()
        return result

    def required(args, key, limit=2000):
        value = args.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"Provide {key} with 1–{limit} characters.")
        return value.strip()

    def worker(process_id):
        result = ctx.owned_child(process_id) if ctx.owned_child else None
        if result is None or result.role == "subagent" or result.actor_kind != "worker":
            raise AssetMaintenanceError("ASSET_ROLE_REQUIRED", "Select a project subprocess you supervise.")
        return result

    def register(assets, args):
        paths = _paths(Path(ctx.workspace_path).resolve(), args.get("paths", []))
        if not paths:
            raise ValueError("Select concrete asset paths.")
        source = {}
        owner, owner_name = PROJECT_OWNER, ""
        if source_id := args.get("process_id"):
            child = worker(source_id)
            if (child.status.value != "completed" or not is_consumable_terminal(child, manager)
                    or not child.worktree.get("promoted") or not child.worktree.get("result_commit")):
                raise AssetMaintenanceError("ASSET_DELIVERY_REQUIRED", "Select the worker of an accepted workspace delivery.")
            assignment = process.delegated_contracts.get(source_id, {})
            if not set(paths).issubset(assignment.get("target_files") or []):
                raise ValueError("Asset paths must be covered by the accepted assignment.")
            owner, owner_name = child.session.session_id, child.session.display_name
            integrated = child.worktree.get("promoted_commit")
            source = {"process_id": source_id, "task_id": child.active_task_id,
                      "commit": integrated or child.worktree["result_commit"],
                      "basis": "integrated" if integrated else "approved_worker_seal"}
        return assets.register(name=required(args, "name", 100), commitment=required(args, "commitment"),
                               decision=required(args, "decision"), paths=paths, owner=owner,
                               owner_name=owner_name, source=source)

    def follow_up_required(*, message="", details=None):
        completion_tool = "complete_supervision" if process.task_kind == "supervisor" else "complete_task"
        return {"delegated": False, **error_payload("ASSET_FOLLOW_UP_REQUIRED", message=message, details=details,
            next_actions=[{"action": completion_tool,
                           "reason": load_prompt("routing/asset_follow_up_close").strip()}]),
            "effect_state": "not_committed", "execution_state": "not_started"}

    def wake(assets, head, records, asset, args, *, continuation=None):
        from ..conversation_continuation import resolve_conversation_process
        from ..task_contract import get_task_contract
        if asset["owner"] == PROJECT_OWNER:
            raise ValueError("The main process already owns this asset. Use its saved context in the current task.")
        if asset.get("delivery_pending"):
            raise AssetMaintenanceError("ASSET_DELIVERY_PENDING", "Reconcile the original delivery first.")
        previous_id = asset["owner_process_id"]
        existing = asset.get("work") or {}
        request_task_id = process.active_task_id or process.process_id
        same_task = (existing.get("request_task_id") == request_task_id
                     and existing.get("revision", asset["revision"]) == asset["revision"])
        description = args.get("instruction")
        if (description is None and continuation is None and same_task
                and existing.get("process_id") == previous_id
                and existing.get("status") in {"offered", "accepted", "delivered"}):
            description = existing["instruction"]
        if not isinstance(description, str) or not description.strip() or len(description) > 2000:
            field = "task_description" if continuation is not None else "instruction"
            raise AssetMaintenanceError("ASSET_WORK_INSTRUCTION_REQUIRED",
                                        f"Describe the work to do in {field} (1–2000 characters).")
        description = description.strip()
        same_offer = same_task and existing.get("instruction") == description
        previous = None
        if same_task:
            if same_offer and existing.get("status") == "delivered":
                return {"delegated": True, "process_id": existing["process_id"], "asset": assets.summary(asset)}
            if existing.get("status") == "delivered" and args.get("follow_up") is not True:
                return follow_up_required(details={"asset_delivery": assets.delivery_receipt(asset)})
            if same_offer and existing.get("status") in {"offered", "accepted"}:
                previous = resolve_conversation_process(manager, store, ctx.workspace_path, previous_id, asset["owner"])
                if not is_consumable_terminal(previous, manager) or previous.status.value == "completed":
                    return {"delegated": True, "process_id": previous_id, "asset": assets.summary(asset)}
        contract = get_task_contract(process)
        targets = contract_paths(contract)
        scoped_targets = targets.intersection(asset["paths"])
        covered = bool(scoped_targets) and scoped_targets == targets
        if scoped_targets and args.get("follow_up") is not True:
            written = workspace_write_versions(process)
            if all(written.get(path) for path in scoped_targets):
                return follow_up_required(message=load_prompt("routing/asset_files_already_written").strip(),
                     details={"written_paths": sorted(scoped_targets)})
        if previous is None:
            previous = resolve_conversation_process(manager, store, ctx.workspace_path, previous_id, asset["owner"])
        if not is_consumable_terminal(previous, manager):
            raise AssetMaintenanceError("ASSET_EXECUTION_ACTIVE", "The maintainer already has an execution. Use that execution or close it first.")
        if (existing.get("status") == "accepted" and previous.status.value == "completed"
                and previous.worktree.get("result_commit") and not previous.worktree.get("promoted")):
            raise AssetMaintenanceError("ASSET_DELIVERY_PENDING", "Review or transfer the prior accepted work first.")
        context = {"name": asset["name"], "commitment": asset["commitment"], "scope_paths": asset["paths"],
                   "revision": asset["revision"],
                   ("scope_decision" if asset.get("scope_history") else "registration_decision"): asset["decision"],
                   "accepted_version": asset["accepted_version"]}
        if existing.get("status") == "needs_reassignment":
            context["previous_unfulfilled_instruction"] = existing["instruction"]
        task_description = render_prompt("routing/asset_work_offer", instruction=description,
                                         context=json.dumps(context, ensure_ascii=False)).strip()
        binding = {"asset_id": asset["asset_id"], "revision": asset["revision"]}
        assignment = dict(continuation) if continuation is not None else None
        if assignment is None:
            assignment = {"target_files": sorted(targets) if covered else asset["paths"],
                          "acceptance_criteria": (contract.get("acceptance_criteria") or [description]) if covered else [description],
                          "task_kind": "action"}
        if (same_task and existing.get("process_id") == previous_id
                and existing.get("status") in {"offered", "accepted"} and previous.status.value != "completed"):
            # The current owner is retrying the same unfulfilled obligation.
            # Use the normal continuation so the failed execution is retired
            # as a completion prerequisite and any retained checkout survives.
            prior = process.delegated_contracts.get(previous_id) or {}
            assignment = {**assignment, "target_files": prior.get("target_files") or assignment["target_files"],
                          "continue_process_id": previous_id,
                          "supersedes_process_id": previous_id, "continuation_mode": "remaining"}
        elif (assignment.get("continue_process_id") and previous.status.value == "completed"
              and (previous.worktree.get("promoted") or existing.get("status") == "delivered")):
            # A new change to a delivered asset needs the original session
            # on this task's checkout. A completed checkout is historical
            # and cannot be resumed, especially after a sibling pins the task.
            for field in ("continue_process_id", "supersedes_process_id", "continuation_mode"):
                assignment.pop(field, None)
        reply = ctx.delegate_task({**assignment, "task_description": task_description},
                                  start_immediately=False,
                                  asset_session=None if assignment.get("continue_process_id") else previous.session,
                                  asset_binding=binding)
        if not reply.get("delegated"):
            return reply
        child = manager.get(reply["process_id"])
        old_work = asset.get("work")
        asset["work"] = {"status": "offered", "process_id": child.process_id, "task_id": child.active_task_id,
                         "revision": asset["revision"],
                         "instruction": description, "request_task_id": request_task_id}
        asset["owner_process_id"] = child.process_id
        published = False
        try:
            store.save_process_checkpoint(child)
            store.save_process_checkpoint(process)
            assets.save(head, records)
            published = True
            for notice in asset["notices"].values():
                if notice["status"] != "acknowledged":
                    notice_text = render_prompt("context/asset_notice", notice_id=notice["notice_id"],
                                                version=notice["version"], message=notice["message"]).rstrip("\n")
                    receipt = manager.send_instruction(child.process_id, notice_text, durable=True,
                                                       message_id=notice["notice_id"], coordination=True)
                    if receipt.get("accepted"):
                        notice["status"] = "delivered"
            assets.save(assets.load()[0], records)
            ctx.start_prepared_child(child.process_id)
        except Exception as exc:
            from backend.core.storage import StorageCommitUnknown
            if isinstance(exc, StorageCommitUnknown):
                ctx.cancel_prepared_child(child.process_id)
                raise AssetMaintenanceError("ASSET_DELIVERY_PENDING", "Asset admission commit is uncertain. Inspect project state before retrying.") from exc
            ctx.cancel_prepared_child(child.process_id)
            if published:
                asset["work"] = old_work
                asset["owner_process_id"] = previous_id
                assets.save(assets.load()[0], records)
            raise
        return {**reply, "execution_state": "started", "asset": assets.summary(asset)}

    def continue_owned_asset(assets, args):
        source = args.get("continue_process_id")
        if (args.get("draft_id") or args.get("supersedes_process_id") not in (None, "", source)
                or args.get("continuation_mode") not in (None, "", "follow_up")
                or args.get("task_kind") not in (None, "", "action")
                or args.get("capability_profile_id") not in (None, "", "development.workspace")
                or args.get("tool_scope_mode") == "exact"):
            return None
        if not args.get("target_files") or not assets.current_ref():
            return None
        try:
            paths = set(_paths(Path(ctx.workspace_path).resolve(), args["target_files"]))
        except ValueError:
            return None
        if not paths:
            return None
        if source:
            previous = ctx.owned_child(source)
            if previous is None:
                return None
            identity = actor_identity(previous)
            matches = lambda record: record["owner"] == identity
        else:
            from ..task_contract import get_task_contract
            requirements = get_task_contract(process).get("host_requirements") or {}
            if (process.runtime_preferences.get("agent_routing", "owner") != "owner"
                    or requirements.get("manual_B_creation") or requirements.get("excluded_process_ids")):
                return None
            name = str(args.get("display_name") or "").strip().casefold()
            if not name:
                return None
            matches = lambda record: record["owner_name"].casefold() == name
        _, records = assets.load()
        candidates = [row for row in records.values()
                      if not row.get("retired") and matches(row) and paths.issubset(row["paths"])]
        if len(candidates) != 1:
            return None
        selected = candidates[0]
        with assets.fence():
            head, records = assets.load()
            asset = assets.asset(records, selected["asset_id"])
            if (asset["owner"] != selected["owner"] or asset["revision"] != selected["revision"]
                    or not matches(asset) or not paths.issubset(asset["paths"])):
                raise AssetMaintenanceError("ASSET_STATE_CHANGED", "Look up the current owner and retry.")
            return wake(assets, head, records, asset, {"instruction": args.get("task_description")}, continuation=args)

    def update_scope(assets, head, records, asset, args, *, retire):
        paths = asset["paths"]
        commitment = asset["commitment"]
        decision = required(args, "decision")
        if not retire:
            workspace = Path(ctx.workspace_path).resolve()
            added = _paths(workspace, args.get("add_paths", []))
            removed = set(_paths(workspace, args.get("remove_paths", [])))
            if removed.intersection(added) or removed.difference(paths):
                raise ValueError("Remove only current paths and keep add_paths and remove_paths distinct.")
            paths = list(dict.fromkeys([*(path for path in paths if path not in removed), *added]))
            if not 1 <= len(paths) <= 12:
                raise ValueError("Keep 1–12 concrete paths or retire the responsibility.")
            if "commitment" in args:
                commitment = required(args, "commitment")
        if assets.revise(records, asset, revision=args.get("expected_revision"), paths=paths,
                         commitment=commitment, decision=decision, retire=retire):
            if not retire and asset["owner"] != PROJECT_OWNER:
                assets.notify(asset, version=f"scope-{asset['revision']}", required=True,
                    message=render_prompt("context/asset_scope_changed", revision=asset["revision"],
                                          decision=decision).strip())
            assets.save(head, records)
        return {"asset": assets.summary(asset)}

    def mutate(assets, args):
        operation = required(args, "operation", 40)
        identity = actor_identity(process)
        owner_operation = operation in {"register", "revise", "retire", "subscribe", "unsubscribe", "notify", "wake", "offer_transfer"}
        if owner_operation and identity != PROJECT_OWNER:
            raise AssetMaintenanceError("ASSET_ROLE_REQUIRED", "Use the project's main process for this operation.")
        with assets.fence():
            if operation == "register":
                return {"asset": register(assets, args)}
            head, records = assets.load()
            binding = process.runtime_preferences.get("asset_execution") or {}
            asset = assets.asset(records, args.get("asset_id") or binding.get("asset_id"))
            if not assets.visible(asset, identity):
                raise AssetMaintenanceError("ASSET_ROLE_REQUIRED", "This execution has no role on the asset.")
            if asset.get("retired"):
                raise AssetMaintenanceError("ASSET_RETIRED", "This asset is available only as historical responsibility.")
            if operation in {"revise", "retire"}:
                return update_scope(assets, head, records, asset, args, retire=operation == "retire")
            if operation == "wake":
                return wake(assets, head, records, asset, args)
            if operation in {"subscribe", "unsubscribe"}:
                asset["subscribed"] = operation == "subscribe"
            elif operation == "notify":
                notice, changed = assets.notify(asset, version=required(args, "version", 200),
                                                message=required(args, "message"))
                if changed:
                    assets.save(head, records)
                return {"notice": notice}
            elif operation == "accept_work":
                work = assets.require_binding(asset, process, binding)
                if work["status"] == "offered":
                    work["status"] = "accepted"
            elif operation == "acknowledge_notice":
                if identity != asset["owner"]:
                    raise AssetMaintenanceError("ASSET_ROLE_REQUIRED", "The current recipient acknowledges its own notices.")
                notice = asset["notices"].get(required(args, "notice_id", 100))
                if notice is None:
                    raise AssetMaintenanceError("ASSET_NOT_FOUND", "This notice was not found.")
                notice["status"] = "acknowledged"
            elif operation == "offer_transfer":
                child = worker(str(args.get("process_id") or "")) if args.get("process_id") else None
                recipient = actor_identity(child) if child else PROJECT_OWNER
                if recipient == asset["owner"]:
                    raise ValueError("Select a different maintainer.")
                asset["transfer"] = {"recipient": recipient, "name": child.session.display_name if child else "",
                                     "process_id": child.process_id if child else "", "revision": asset["revision"]}
            elif operation == "accept_transfer":
                offer = asset.get("transfer") or {}
                if offer.get("recipient") != identity or offer.get("revision") != asset["revision"]:
                    raise AssetMaintenanceError("ASSET_ROLE_REQUIRED", "The pending transfer must be accepted by its named successor.")
                if asset.get("delivery_pending"):
                    raise AssetMaintenanceError("ASSET_DELIVERY_PENDING", "Reconcile the original delivery before transferring responsibility.")
                asset.update(owner=identity, owner_name=offer["name"],
                             owner_process_id=process.process_id if identity != PROJECT_OWNER else "",
                             revision=asset["revision"] + 1, transfer=None)
                if (asset.get("work") or {}).get("status") in {"offered", "accepted"}:
                    asset["work"]["status"] = "needs_reassignment"
                for notice in asset["notices"].values():
                    if notice["status"] == "delivered":
                        notice["status"] = "queued"
            else:
                raise ValueError("Unknown asset maintenance operation.")
            assets.save(head, records)
            return {"asset": assets.summary(asset)}

    ctx.tools["asset_status"] = AgentTool(name="asset_status", immediate_interface=True,
        description=load_prompt("tools/asset_status").strip(),
        parameters={"type": "object", "properties": {name: {"type": "string"} for name in ("asset_id", "query")}},
        execute=lambda args: invoke(lookup, args), execution_contract=data_broker("host.runtime.asset_status"),
        model_result=lookup_feedback)
    operations = ["accept_work", "acknowledge_notice", "accept_transfer"]
    if actor_identity(process) == PROJECT_OWNER:
        if store is not None:
            ctx.continue_owned_asset = lambda args: invoke(continue_owned_asset, args)
        operations += ["register", "revise", "retire", "subscribe", "unsubscribe", "notify", "wake", "offer_transfer"]
    properties = {name: {"type": "string"} for name in (
        "asset_id", "name", "commitment", "decision", "process_id", "version", "message", "instruction", "notice_id")}
    path_fields = {"type": "array", "items": {"type": "string"}, "maxItems": 12}
    properties.update(operation={"type": "string", "enum": operations},
                      paths=path_fields, add_paths=path_fields, remove_paths=path_fields,
                      expected_revision={"type": "integer", "minimum": 1},
                      follow_up={"type": "boolean", "description": load_prompt("tools/asset_distinct_follow_up").strip()})
    ctx.tools["asset_maintenance"] = AgentTool(name="asset_maintenance", immediate_interface=True,
        description=load_prompt("tools/asset_maintenance").strip(),
        parameters={"type": "object", "required": ["operation"], "properties": properties, "additionalProperties": False},
        execute=lambda args: invoke(mutate, args), read_only=False, effect=ToolEffect.PROCESS,
        execution_contract=data_broker("host.runtime.asset_maintenance"))
