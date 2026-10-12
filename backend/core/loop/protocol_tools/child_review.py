"""child review protocol tools."""
from __future__ import annotations
from backend.core.loop.execution_contract import HOST_COMPUTE, data_broker
import json
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.models import ProcessStatus
from backend.core.loop.operation_policy import is_effectful_mutation

def install(ctx):
    process = ctx.process
    on_stream_event = ctx.on_stream_event
    tools = ctx.tools
    manager = ctx.manager
    dossier_store = ctx.dossier_store
    _owned_child = ctx.owned_child
    _spawn_child = ctx.spawn_child
    def list_agents(_args: dict) -> dict:
        children = [
            child for child_id in process.coordination_snapshot()[0]
            if manager is not None and (child := manager.get(child_id)) is not None
        ]
        return {
            "agents": [
                {
                    "process_id": child.process_id,
                    "session_id": child.session.session_id,
                    "display_name": child.session.display_name,
                    **(manager.presentation.read(child.process_id) if getattr(manager, "presentation", None) else {}),
                    "actor_kind": child.actor_kind,
                    "status": child.status.value,
                    "task": child.task_description[:300],
                    "target_files": list(process.delegated_contracts.get(child.process_id, {}).get("target_files", [])),
                    "superseded_by": process.delegated_contracts.get(child.process_id, {}).get("superseded_by", ""),
                    "review_target_id": child.review_target_id,
                    "depends_on": list(child.depends_on),
                    "worktree": dict(child.worktree or {}),
                    "error": (child.result or {}).get("error", ""),
                }
                for child in children
            ]
        }


    tools["list_agents"] = AgentTool(
        execution_contract=data_broker("host.runtime.list_agents"),
        name="list_agents",
        description="List this supervisor's B Agents and their durable execution states.",
        parameters={"type": "object", "properties": {}, "required": []},
        execute=list_agents, read_only=True, effect=ToolEffect.READ, idempotent=True,
    )


    def wait_agents(args: dict) -> dict:
        ids = [str(item) for item in (args.get("process_ids", []) or process.child_ids)]
        timeout = max(0.0, min(float(args.get("timeout", 30) or 30), 300.0))
        return_when = str(args.get("return_when", "all_terminal"))
        owned_ids = []
        results = {}
        for child_id in ids:
            if _owned_child(child_id) is None:
                results[child_id] = {
                    "status": "not_found_or_not_owned", "result": None,
                }
            else:
                owned_ids.append(child_id)
        waited = manager.wait_many(
            owned_ids,
            timeout=timeout,
            cancel_event=process.cancellation_event,
            return_when=return_when,
        ) if manager else {
            "processes": {}, "all_terminal": False,
            "state_version": 0, "state_changed": False,
        }
        for child_id, state in waited["processes"].items():
            child = _owned_child(child_id)
            outcome = (
                dict(child.result) if child is not None
                and isinstance(child.result, dict) else {}
            )
            outcome_ref = ""
            if outcome:
                refs = dossier_store.put(
                    f"agent-outcomes/{process.active_task_id or process.process_id}/{child_id}",
                    outcome,
                    metadata={"authority": "host_runtime_outcome"},
                )
                outcome_ref = refs["pinned"]
            response = str(outcome.get("response", ""))
            metadata = dict(outcome.get("metadata") or {})
            results[child_id] = {
                "status": state.get("status", "not_found"),
                "outcome_ref": outcome_ref,
                "response_excerpt": response[:1800],
                "response_truncated": len(response) > 1800,
                "error": outcome.get("error"),
                "steps_used": outcome.get("steps_used", 0),
                "tool_calls_executed": outcome.get("tool_calls_executed", 0),
                "cache_summary": metadata.get("cache_summary"),
            }
        return {
            "agents": results,
            "all_terminal": waited["all_terminal"],
            "state_version": waited["state_version"],
            "state_changed": waited["state_changed"],
            "wait_mode": return_when,
        }


    tools["wait_agents"] = AgentTool(
        execution_contract=data_broker("host.runtime.wait_agents"),
        name="wait_agents",
        description="Wait for selected B Agents with a bounded, cancellation-aware timeout.",
        parameters={
            "type": "object",
            "properties": {
                "process_ids": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number"},
                "return_when": {
                    "type": "string",
                    "enum": ["all_terminal", "any_terminal"],
                },
            },
            "required": [],
        },
        execute=wait_agents, read_only=True, effect=ToolEffect.READ,
        prepare_args=lambda raw: {
            **dict(raw or {}),
            **({"timeout": float(raw["timeout"])}
               if isinstance((raw or {}).get("timeout"), str)
               and str((raw or {}).get("timeout", "")).strip().replace(".", "", 1).isdigit()
               else {}),
        },
    )


    def review_child_outcome(args: dict) -> dict:
        child_id = str(args.get("process_id", ""))
        child = _owned_child(child_id)
        if child is None:
            return {"accepted": False, "error": "process is not an owned child"}
        if child.status in {
            ProcessStatus.RUNNING, ProcessStatus.WAITING,
            ProcessStatus.CANCELLING, ProcessStatus.AWAITING_USER,
        }:
            return {"accepted": False, "error": "child outcome is not terminal"}
        raw_verdict = str(args.get("verdict", "")).strip().casefold()
        verdict_key = raw_verdict.replace("-", "_").replace(" ", "_")
        verdict = {
            "approve": "approved",
            "approved": "approved",
            "changes_required": "changes_required",
            "change_required": "changes_required",
            "request_changes": "changes_required",
            "reject": "changes_required",
            "rejected": "changes_required",
        }.get(verdict_key, "")
        summary = str(args.get("summary", "")).strip()
        if not verdict:
            return {
                "accepted": False,
                "error": "verdict must be approved or changes_required",
            }
        if not summary:
            return {"accepted": False, "error": "non-empty summary is required"}
        if verdict == "approved" and child.status != ProcessStatus.COMPLETED:
            return {
                "accepted": False,
                "error": "a failed or cancelled child cannot be approved",
            }
        known_receipts = {
            str(item.get("receipt_id", "")) for item in child.tool_receipts
            if item.get("receipt_id")
        }
        committed_action_receipts = {
            str(item.get("receipt_id", "")) for item in child.tool_receipts
            if item.get("receipt_id")
            and item.get("succeeded") is True
            and item.get("committed") is True
            and is_effectful_mutation(str(item.get("effect", "read")))
        }
        # Receipt selection is mechanical host work.  The reviewer judges
        # semantics; it should not spend provider turns copying opaque IDs
        # that the process tree already owns.  Keep explicit IDs accepted
        # for backwards compatibility, but auto-link every committed action
        # receipt when the field is omitted.
        explicit_receipts = args.get("receipt_ids") if "receipt_ids" in args else None
        receipt_ids = (
            [str(item) for item in (explicit_receipts or []) if str(item)]
            if explicit_receipts is not None
            else (
                sorted(committed_action_receipts)
                if verdict == "approved" and child.task_kind == "action"
                else []
            )
        )
        unknown = [item for item in receipt_ids if item not in known_receipts]
        if unknown:
            return {
                "accepted": False,
                "error": "unknown child receipts: " + ", ".join(unknown),
            }
        if verdict == "approved" and child.task_kind == "action" and not receipt_ids:
            return {
                "accepted": False,
                "error": "approving an action child requires cited receipt_ids",
            }
        if (
            verdict == "approved"
            and child.task_kind == "action"
            and not committed_action_receipts.intersection(receipt_ids)
        ):
            return {
                "accepted": False,
                "error": (
                    "approving an action child requires at least one cited "
                    "successful committed action receipt"
                ),
            }
        review = {
            "verdict": verdict,
            "summary": summary,
            "receipt_ids": receipt_ids,
            "receipt_selection": (
                "explicit" if explicit_receipts is not None else "host_auto"
            ),
            "required_test_ids": list(child.required_test_ids),
            "reviewed_at_step": process.steps_used,
        }
        if (
            verdict == "approved"
            and child.worktree.get("state") == "sealed"
            and not child.worktree.get("own_commit")
            and manager is not None
        ):
            try:
                manager.dispose_process_worktree(child, keep_ref=True)
            except Exception as exc:
                return {
                    "accepted": False,
                    "error": "approved no-change worktree cleanup failed: " + str(exc),
                }
            review["worktree_cleanup"] = "disposed_no_changes"
            if on_stream_event is not None:
                on_stream_event({
                    "event": "worktree_disposed",
                    "process_id": child.process_id,
                    "reason": "approved_no_changes",
                    "worktree": dict(child.worktree),
                })
        process.child_reviews[child_id] = review
        return {"accepted": True, "process_id": child_id, "review": review}


    tools["review_child_outcome"] = AgentTool(
        execution_contract=data_broker("host.runtime.review_child_outcome"),
        name="review_child_outcome",
        description=(
            "Record A's structured review of a terminal owned B outcome. "
            "Approval is required before any required delegated outcome can satisfy "
            "the supervisor completion gate. The Host automatically links the "
            "child's qualifying receipts; do not copy internal receipt IDs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_id": {"type": "string"},
                "verdict": {
                    "type": "string",
                    "enum": ["approved", "changes_required"],
                },
                "summary": {"type": "string"},
            },
            "required": ["process_id", "verdict", "summary"],
        },
        execute=review_child_outcome, read_only=True, effect=ToolEffect.READ,
    )


    def promote_agent_changes(args: dict) -> dict:
        if manager is None:
            return {"promoted": False, "error": "AgentProcessManager is unavailable"}
        process_ids = [
            str(item) for item in (args.get("process_ids") or []) if str(item)
        ]
        if not process_ids:
            return {"promoted": False, "error": "process_ids is required"}
        try:
            result = manager.promote_process_results(process, process_ids)
            if on_stream_event is not None:
                on_stream_event({
                    "event": "worktree_promoted",
                    "process_id": process.process_id,
                    "task_id": process.active_task_id,
                    "promotion": result,
                })
            return result
        except (ValueError, RuntimeError) as exc:
            return {"promoted": False, "error": str(exc)}


    tools["promote_agent_changes"] = AgentTool(
        execution_contract=data_broker("host.runtime.promote_agent_changes"),
        name="promote_agent_changes",
        description=(
            "Promote an A-approved, sealed DAG result into the user working "
            "repository. The Host includes transitive predecessors, checks the "
            "task snapshot and blocks overlapping workspace drift."
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["process_ids"],
        },
        execute=promote_agent_changes,
        read_only=False,
        effect=ToolEffect.WORKSPACE_WRITE,
        resources=["filesystem:*"],
        idempotent=False,
    )


    def send_feedback(args: dict) -> dict:
        if manager is None:
            return {"error": "AgentProcessManager is unavailable"}
        child_id = str(args.get("process_id", ""))
        if _owned_child(child_id) is None:
            return {"accepted": False, "error": "process is not an owned child"}
        child = _owned_child(child_id)
        if child.status in {ProcessStatus.COMPLETED, ProcessStatus.FAILED,
                            ProcessStatus.TIMED_OUT, ProcessStatus.CANCELLED}:
            from backend.core.errors import error_payload
            return {"accepted": False, **error_payload(
                "DELEGATION_ADMISSION_FAILED",
                message="This execution has ended. Continue its durable session with delegate_task; a closed mailbox cannot restart execution.",
                next_actions=[{"action": "delegate_task", "patch": {"continue_process_id": child_id},
                               "reason": "Keep the original agent context and provide the updated task contract."}],
            )}
        return manager.send_instruction(
            child_id, str(args.get("message", "")),
        )


    tools["send_feedback"] = AgentTool(
        execution_contract=data_broker("host.runtime.send_feedback"),
        name="send_feedback",
        description=(
            "Send corrective or additional instructions to a B Agent mailbox. "
            "Delivery is applied at the next safe turn boundary."
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["process_id", "message"],
        },
        execute=send_feedback, read_only=True, effect=ToolEffect.READ,
    )


    def cancel_agent(args: dict) -> dict:
        child_id = str(args.get("process_id", ""))
        if manager and _owned_child(child_id) is None:
            return {"requested": False, "status": "not_owned"}
        return manager.kill(child_id) if manager else {
            "requested": False, "status": "manager_unavailable",
        }


    tools["cancel_agent"] = AgentTool(
        execution_contract=data_broker("host.runtime.cancel_agent"),
        name="cancel_agent",
        description="Request cancellation of one delegated B Agent without faking a terminal state.",
        parameters={
            "type": "object",
            "properties": {"process_id": {"type": "string"}},
            "required": ["process_id"],
        },
        execute=cancel_agent, read_only=True, effect=ToolEffect.READ,
    )


    def request_review(args: dict) -> dict:
        if manager is None:
            return {"error": "AgentProcessManager is unavailable"}
        target_id = str(args.get("process_id", ""))
        target = manager.get(target_id)
        if target is None or (target is not process and _owned_child(target_id) is None):
            return {"error": "review target not found or not owned"}
        if target is not process and target.status in {
            ProcessStatus.RUNNING, ProcessStatus.WAITING,
            ProcessStatus.CANCELLING, ProcessStatus.AWAITING_USER,
        }:
            return {"error": "review target is not terminal"}
        if target is process:
            from backend.core.loop.verification_policy import verification_plan
            plan = verification_plan(process)
            if not plan.requires_independent_review:
                return {
                    "accepted": False,
                    "code": "REVIEW_NOT_REQUIRED",
                    "detail": (
                        "The Host selected verification level "
                        f"{plan.level}; use existing receipts/tests and submit "
                        "complete_task instead of creating a Reviewer B."
                    ),
                    "verification_plan": plan.to_dict(),
                    "next_action": "complete_task",
                }
        evidence = {
            "task": target.task_description,
            "status": target.status.value,
            "result": target.result,
            "tool_receipts": target.tool_receipts,
            "completion_claim": (
                target.completion_claim.to_dict()
                if getattr(target.completion_claim, "to_dict", None) else None
            ),
        }
        review_files = []
        if getattr(target.completion_claim, "files", None):
            review_files.extend(str(item) for item in target.completion_claim.files)
        instruction = (
            "Independently review the following task result. Inspect the workspace, "
            "run relevant registered tests when useful, and call complete_review with "
            "verdict approved or changes_required. Do not modify product files.\n\n"
            f"Review focus: {str(args.get('focus', '')).strip()}\n"
            f"Evidence bundle:\n{json.dumps(evidence, ensure_ascii=False, default=str)[:24000]}"
        )
        child = _spawn_child(
            task_description=instruction,
            profile_id="review.independent",
            task_kind="review",
            max_steps=int(args.get("max_steps", 40) or 40),
            target_files=review_files,
            acceptance_criteria=["Issue a structured complete_review verdict"],
            required_for_parent_completion=False,
            actor_kind="reviewer",
            review_target_id=target_id,
        )
        return {"reviewer_process_id": child.process_id, "target_process_id": target_id}


    tools["request_review"] = AgentTool(
        execution_contract=data_broker("host.runtime.request_review"),
        name="request_review",
        description=(
            "Start an independent read-only Reviewer B for a completed B result or "
            "for this supervisor's Level-2 self-executed work. For bounded self-work, "
            "submit complete_task first: the Host will request an independent review "
            "only when the verification tier requires one. Hidden reasoning is not shared."
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_id": {"type": "string"},
                "focus": {"type": "string"},
                "max_steps": {"type": "integer"},
            },
            "required": ["process_id", "focus"],
        },
        execute=request_review, read_only=True, effect=ToolEffect.READ,
    )
