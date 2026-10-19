"""bundle execution protocol tools."""
from __future__ import annotations
from backend.core.loop.execution_contract import HOST_COMPUTE, data_broker
import json
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.code_dossier import build_shard_dossier

def install(ctx):
    process = ctx.process
    on_stream_event = ctx.on_stream_event
    tools = ctx.tools
    manager = ctx.manager
    dossier_store = ctx.dossier_store
    _spawn_child = ctx.spawn_child
    prepare_task_bundle = ctx.prepare_task_bundle
    def delegate_task_bundle(args: dict) -> dict:
        """Deterministically shard and start a bounded set of worker contracts."""
        children = []
        try:
            profile_id = str(args.get("capability_profile_id") or "governance.observe")
            profile_error = CapabilityProfiles.worker_error(profile_id)
            if profile_error:
                return {"delegated": False, **profile_error}
            prepared = prepare_task_bundle(args)
            if prepared.get("error"):
                return {"delegated": False, **prepared}
            plan = prepared["bundle"]
            if plan.get("requires_semantic_partition"):
                return {
                    "delegated": False,
                    "requires_semantic_partition": True,
                    "reason": (
                        "At least one deterministic shard is still oversized; "
                        "refine target_files or ask the user if the partition changes scope."
                    ),
                    **prepared,
                }
            shards = list(plan.get("shards", []))
            if not shards:
                from backend.core.errors import error_payload
                return {
                    "delegated": False,
                    **error_payload(
                        "DELEGATION_EMPTY_RESULT",
                        message=(
                            "The deterministic bundle found no existing source "
                            "files to shard; no B Agent was started."
                        ),
                        details={
                            "target_files": list(args.get("target_files") or []),
                            "dossier_ref": prepared.get("pinned", ""),
                        },
                        next_actions=[{
                            "action": "use_delegate_task",
                            "reason": (
                                "Use direct delegation for creating a new file or "
                                "for any task without existing source shards."
                            ),
                            "patch": {
                                "capability_profile_id": "development.workspace",
                                "task_kind": "action",
                            },
                        }],
                    ),
                    **prepared,
                }
            if process.task_budget is not None:
                snapshot = process.task_budget.snapshot()
                remaining = (
                    int(snapshot["limits"]["max_agents"])
                    - int(snapshot["used"]["agents"])
                )
                if len(shards) > remaining:
                    return {
                        "delegated": False,
                        "error": (
                            f"bundle needs {len(shards)} agents but task-tree "
                            f"budget has {remaining} remaining"
                        ),
                        **prepared,
                    }
            criteria = [
                str(item) for item in (args.get("acceptance_criteria", []) or [])
                if str(item).strip()
            ]
            criteria = list(dict.fromkeys(criteria + [
                "Report semantic findings with precise file:line evidence",
                "Do not claim files outside the assigned deterministic shard were inspected",
                "Use the pinned shard handoff before requesting any additional context",
            ]))
            task_kind = str(args.get("task_kind", "answer"))
            full_dossier = json.loads(
                dossier_store.resolve(prepared["pinned"]).content
            )
            shard_dossiers = [(shard, build_shard_dossier(full_dossier, shard)) for shard in shards]
            for shard, shard_dossier in shard_dossiers:
                if not shard_dossier["source_complete"]:
                    return {
                        "delegated": False,
                        "requires_semantic_partition": True,
                        "reason": (
                            "At least one source file exceeds the bounded snapshot; "
                            "split that file semantically before delegation."
                        ),
                        **prepared,
                    }
            # Validate all bounded sources before the first worker starts.
            for shard, shard_dossier in shard_dossiers:
                shard_refs = dossier_store.put(
                    f"code-dossiers/{prepared['digest']}/{shard['shard_id']}",
                    shard_dossier,
                    metadata={
                        "authority": "deterministic_host_snapshot",
                        "semantic_conclusion": False,
                        "parent_dossier": prepared["pinned"],
                    },
                )
                description = (
                    str(shard["task_description"])
                    + "\n\nOpen this pinned shard handoff first with context_open "
                    + f"(raise max_chars if it continues): {shard_refs['pinned']}"
                    + "\nIt contains numbered source and dependency facts for the "
                    + "assigned files. Do not reread or enumerate the workspace. "
                    + "Stop gathering once the acceptance criteria can be answered."
                )
                child = _spawn_child(
                    task_description=description,
                    profile_id=profile_id,
                    task_kind=task_kind,
                    max_steps=int(args.get("max_steps", 35) or 35),
                    target_files=list(shard["target_files"]),
                    acceptance_criteria=criteria,
                    allowed_tools=[
                        "context_open", "context_search", "artifact_read",
                        "calculate", "decision_evidence",
                    ],
                    required_for_parent_completion=bool(
                        args.get("required_for_parent_completion", True)
                    ),
                )
                children.append({
                    "shard_id": shard["shard_id"],
                    "process_id": child.process_id,
                    "target_files": shard["target_files"],
                    "estimated_tokens": shard["estimated_tokens"],
                    "handoff_ref": shard_refs["pinned"],
                })
            if on_stream_event is not None:
                on_stream_event({
                    "event": "task_bundle_delegated",
                    "process_id": process.process_id,
                    "dossier_ref": prepared["pinned"],
                    "shard_count": len(children),
                    "children": children,
                })
            return {
                "delegated": True,
                "dossier_ref": prepared["pinned"],
                "children": children,
                "recommended_parallelism": plan["recommended_parallelism"],
            }
        except (ValueError, OSError, RuntimeError, KeyError) as exc:
            from backend.core.errors import error_payload
            cancelled = []
            cancellation_errors = {}
            for item in children:
                child_id = item["process_id"]
                try:
                    manager.kill(child_id, reason="bundle_admission_failed")
                    cancelled.append(child_id)
                except Exception as cancel_exc:
                    cancellation_errors[child_id] = str(cancel_exc)
                contract = process.delegated_contracts.get(child_id)
                if contract is not None:
                    contract["required_for_parent_completion"] = False
                    contract["admission_rolled_back"] = True
            return {"delegated": False, "children": children,
                    **error_payload("DELEGATION_ADMISSION_FAILED", message=str(exc),
                                    state_changed=bool(children), details={
                                        "partial_admission": bool(children),
                                        "cancel_requested_process_ids": cancelled,
                                        "cancellation_errors": cancellation_errors,
                                        "side_effects_rolled_back": False,
                                    })}


    tools["delegate_task_bundle"] = AgentTool(
        execution_contract=data_broker("host.runtime.delegate_task_bundle"),
        name="delegate_task_bundle",
        description=(
            "Build one deterministic dossier, shard a large task, and start all "
            "bounded B contracts in one call. Host handles enumeration, copying, "
            "budget checks and dispatch; A remains responsible for semantic review."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "target_files": {"type": "array", "items": {"type": "string"}},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "capability_profile_id": {
                    "type": "string",
                    "enum": list(CapabilityProfiles.ids(actor_kind="worker")),
                    "default": "governance.observe",
                },
                "task_kind": {
                    "type": "string",
                    "enum": ["answer", "plan", "action"],
                    "default": "answer",
                },
                "max_files": {"type": "integer"},
                "max_tokens_per_shard": {"type": "integer"},
                "max_files_per_shard": {"type": "integer"},
                "max_shards": {"type": "integer"},
                "max_steps": {"type": "integer"},
                "required_for_parent_completion": {"type": "boolean"},
            },
            "required": ["goal", "target_files", "acceptance_criteria"],
        },
        execute=delegate_task_bundle,
        effect=ToolEffect.READ,
        idempotent=False,
    )
