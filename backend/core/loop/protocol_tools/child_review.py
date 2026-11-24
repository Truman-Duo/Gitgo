"""child review protocol tools."""
from __future__ import annotations

from contextlib import nullcontext
from backend.core.prompt_templates import load_prompt
from backend.core.loop.execution_contract import HOST_COMPUTE, data_broker
import json
from backend.core.loop.agent_tool import AgentTool, ApprovalMode, ToolEffect
from backend.core.loop.models import ProcessStatus
from backend.core.loop.lifecycle import execution_closeout, is_consumable_terminal
from backend.core.loop.operation_policy import is_effectful_mutation

def install(ctx):
    process = ctx.process
    on_stream_event = ctx.on_stream_event
    tools = ctx.tools
    manager = ctx.manager
    dossier_store = ctx.dossier_store
    _owned_child = ctx.owned_child
    _spawn_child = ctx.spawn_child
    from backend.core.loop.asset_maintenance import asset_recovery_call
    from backend.core.loop.model_feedback import child_results_feedback

    def list_agents(_args: dict) -> dict:
        from backend.core.loop.assignment_handoff import review_draft_id, sealed_delivery_target
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
                    **execution_closeout(child, manager),
                    "task": child.task_description[:300],
                    "target_files": list(process.delegated_contracts.get(child.process_id, {}).get("target_files", [])),
                    "superseded_by": process.delegated_contracts.get(child.process_id, {}).get("superseded_by", ""),
                    **({"returned_assignment": returned} if (returned :=
                        process.delegated_contracts.get(child.process_id, {}).get("returned_assignment")) else {}),
                    "review_target_id": child.review_target_id,
                    "depends_on": list(child.depends_on),
                    "worktree": dict(child.worktree or {}),
                    **({"handoff_review_target": {"draft_id": draft_id,
                         "result_commit": (child.worktree or {}).get("result_commit", "")}}
                       if (draft_id := review_draft_id(process, child)) else {}),
                    **({"result_ref": target["result_ref"]}
                       if (target := sealed_delivery_target(process, child, manager)) else {}),
                    **({"asset_recovery_call": recovery}
                       if (recovery := asset_recovery_call(process, child, manager)) else {}),
                    "error": (child.result or {}).get("error", ""),
                }
                for child in children
            ]
        }


    tools["list_agents"] = AgentTool(
        execution_contract=data_broker("host.runtime.list_agents"),
        name="list_agents",
        immediate_interface=True,
        description=load_prompt("tools/list_agents"),
        parameters={"type": "object", "properties": {}, "required": []},
        execute=list_agents, read_only=True, effect=ToolEffect.READ, idempotent=True,
        model_result=child_results_feedback,
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
        from backend.core.loop.assignment_handoff import handoff_status, sealed_delivery_target
        from backend.core.loop.coordination import supervisor_wait_interruption
        from backend.core.loop.task_maintenance import state_for
        waited = manager.wait_many(
            owned_ids,
            timeout=timeout,
            cancel_event=process.cancellation_event,
            return_when=return_when,
            interrupt_check=lambda: supervisor_wait_interruption(process, owned_ids),
        ) if manager else {
            "processes": {}, "all_terminal": False,
            "state_version": 0, "state_changed": False,
        }
        from backend.core.loop.question_broker import decision_evidence
        handoffs = {item["source_process_id"]: item for item in handoff_status(process, state_for(process))}
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
            review_target = None
            delivery_target = sealed_delivery_target(process, child, manager) if child is not None else None
            if delivery_target:
                files = child.worktree.get("changed_files") or []
                review_target = {"draft_id": delivery_target["draft_id"], "result_commit": delivery_target["result_commit"],
                                 "changed_files": files[:64], "changed_files_total": len(files)}
            results[child_id] = {
                "status": state.get("status", "not_found"),
                **({"display_name": child.session.display_name,
                    "already_promoted": bool((child.worktree or {}).get("promoted"))} if child is not None else {}),
                "lifecycle_finalized": state.get("lifecycle_finalized", False),
                "closeout_state": state.get("closeout_state", "pending"),
                "outcome_ref": outcome_ref,
                "response_excerpt": response[:1800],
                "response_truncated": len(response) > 1800,
                "error": outcome.get("error"),
                "steps_used": outcome.get("steps_used", 0),
                "tool_calls_executed": outcome.get("tool_calls_executed", 0),
                "cache_summary": metadata.get("cache_summary"),
                "user_decisions": decision_evidence(child) if child is not None else None,
                **({"handoff": handoffs[child_id]} if child_id in handoffs else {}),
                **({"handoff_review_target": review_target} if review_target else {}),
                **({"asset_recovery_call": recovery}
                   if child is not None and (recovery := asset_recovery_call(process, child, manager)) else {}),
                **({"delivery_call": {
                    "tool": "promote_agent_changes",
                    "arguments": {"result_ref": delivery_target["result_ref"]},
                    "required_decision": "After inspecting this sealed result, add review.summary to approve it. "
                                         "Add completion_result only if ready to deliver to the user.",
                    **({"asset_delivery": load_prompt("completion/asset_review").strip()}
                       if child.runtime_preferences.get("asset_execution") else {}),
                }} if delivery_target else {}),
            }
        return {
            "agents": results,
            "all_terminal": waited["all_terminal"],
            "state_version": waited["state_version"],
            "state_changed": waited["state_changed"],
            "wait_mode": return_when,
            "interruption": waited.get("interruption"),
        }


    tools["wait_agents"] = AgentTool(
        execution_contract=data_broker("host.runtime.wait_agents"),
        name="wait_agents",
        immediate_interface=True,
        description=load_prompt('tools/wait_agents'),
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
        model_result=child_results_feedback,
        prepare_args=lambda raw: {
            **dict(raw or {}),
            **({"timeout": float(raw["timeout"])}
               if isinstance((raw or {}).get("timeout"), str)
               and str((raw or {}).get("timeout", "")).strip().replace(".", "", 1).isdigit()
               else {}),
        },
    )


    def review_child_outcome(args: dict) -> dict:
        result_ref = args.get("result_ref")
        if not result_ref and args.get("process_id"):
            return review_outcome(args)
        from backend.core.errors import error_payload
        if (not result_ref or manager is None
                or any(key in args for key in ("process_id", "draft_id", "result_commit"))):
            return {"accepted": False, **error_payload("INVALID_TOOL_ARGUMENTS",
                    message="Select result_ref on its own or supply the explicit child review fields.")}
        from backend.core.loop.assignment_handoff import AssignmentHandoffError, resolve_delivery_ref
        from backend.core.loop.mechanism_lifecycle import fence
        with fence(process):
            try:
                target = resolve_delivery_ref(process, manager, result_ref)
            except AssignmentHandoffError as exc:
                return {"accepted": False, **error_payload(exc.code, message=str(exc))}
            return review_outcome({**args, **target})


    def review_outcome(args: dict) -> dict:
        child_id = str(args.get("process_id", ""))
        child = _owned_child(child_id)
        if child is None:
            return {"accepted": False, "error": "process is not an owned child"}
        if not is_consumable_terminal(child, manager):
            return {"accepted": False, "error": "child closeout is not finalized",
                    **execution_closeout(child, manager)}
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
        from backend.core.loop.assignment_handoff import (
            AssignmentHandoffError, checkpoint, recorded_scope_change, requires_versioned_review,
            review_draft_id, sealed_delivery_target,
        )
        handoff_id = review_draft_id(process, child)
        patch_helper = getattr(child, "role", None) == "subagent" and child.task_kind == "action"
        exact_version = requires_versioned_review(process, child)
        sealed = str(child.worktree.get("result_commit") or "")
        if exact_version and verdict == "approved" and (
            not sealed or args.get("result_commit") != sealed or args.get("draft_id") != handoff_id
        ):
            from backend.core.errors import error_payload
            return {"accepted": False, **error_payload("ASSIGNMENT_HANDOFF_RESULT_VERSION_REQUIRED",
                details={"draft_id": handoff_id, "result_commit": sealed})}
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
            and not handoff_id
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
        if exact_version:
            from backend.core.errors import error_payload
            from backend.core.loop.assignment_handoff import contract_digest, subagent_scope_digest
            from backend.core.loop.mechanism_lifecycle import fence
            with fence(process), process._context_lock, process._coordination_lock:
                contract = process.delegated_contracts.get(child_id) or {}
                if patch_helper and sealed_delivery_target(process, child, manager) is None:
                    return {"accepted": False, **error_payload("SUBAGENT_RESULT_STALE")}
                if verdict == "approved" and recorded_scope_change(process, child_id):
                    return {"accepted": False, **error_payload("ASSIGNMENT_HANDOFF_SCOPE_RETIRED")}
                if (contract.get("returned_assignment") or contract.get("superseded_by")
                        or handoff_id != review_draft_id(process, child)
                        or sealed != str(child.worktree.get("result_commit") or "")):
                    return {"accepted": False, **error_payload("ASSIGNMENT_HANDOFF_VERSION_CHANGED")}
                review.update(draft_id=handoff_id, result_commit=sealed, result_contract=contract_digest(child),
                              owner_contract=subagent_scope_digest(process) if patch_helper else contract_digest(process))
                previous = process.child_reviews.get(child_id)
                def rollback():
                    if previous is None:
                        process.child_reviews.pop(child_id, None)
                    else:
                        process.child_reviews[child_id] = previous
                process.child_reviews[child_id] = review
                try:
                    checkpoint(manager, process, {"event": "subagent_result_reviewed" if patch_helper else "assignment_handoff_result_reviewed",
                        "source_process_id": child_id, "owner_process_id": process.process_id, **review}, rollback)
                except AssignmentHandoffError as exc:
                    return {"accepted": False, **error_payload(exc.code, message=str(exc))}
            manager.notify_state_changed(child_id)
        else:
            process.child_reviews[child_id] = review
        return {"accepted": True, "process_id": child_id, "review": review}


    tools["review_child_outcome"] = AgentTool(
        execution_contract=data_broker("host.runtime.review_child_outcome"),
        name="review_child_outcome",
        immediate_interface=True,
        description=(
            load_prompt('tools/review_child_outcome')
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_id": {"type": "string"},
                "result_ref": {"type": "string", "description": "Exact sealed result from run_subagent, wait_agents or list_agents."},
                "draft_id": {"type": "string", "description": "Exact draft ID for a versioned handoff result."},
                "result_commit": {"type": "string", "description": "Exact sealed commit under review."},
                "verdict": {
                    "type": "string",
                    "enum": ["approved", "changes_required"],
                },
                "summary": {"type": "string"},
            },
            "required": ["verdict", "summary"],
        },
        execute=review_child_outcome, read_only=True, effect=ToolEffect.READ,
    )


    def promote_agent_changes(args: dict) -> dict:
        if manager is None:
            return {"promoted": False, "error": "AgentProcessManager is unavailable"}
        process_ids = [
            str(item) for item in (args.get("process_ids") or []) if str(item)
        ]
        result_ref = args.get("result_ref")
        if bool(process_ids) == bool(result_ref):
            from backend.core.errors import error_payload
            return {"promoted": False, **error_payload("INVALID_TOOL_ARGUMENTS",
                        message="Select one result_ref or supply process_ids."),
                    "effect_state": "not_committed", "execution_state": "not_started"}
        review_args = args.get("review")
        completion_result = args.get("completion_result", "").strip()
        if review_args is not None and (not isinstance(review_args, dict)
                or not isinstance(review_args.get("summary"), str) or not review_args["summary"].strip()):
            return {"promoted": False, "error": "review.summary must state the supervisor's approval of the selected result"}
        if "completion_result" in args and (not completion_result or review_args is None):
            return {"promoted": False, "error": "completion_result requires a versioned review and non-empty result"}
        from backend.core.errors import error_payload
        from backend.core.loop.assignment_handoff import (
            AssignmentHandoffError, requires_versioned_review, resolve_delivery_ref, review_draft_id,
        )
        from backend.core.loop.mechanism_lifecycle import fence
        approved = None
        try:
            with fence(process) if review_args is not None or result_ref else nullcontext():
                if result_ref:
                    try:
                        target = resolve_delivery_ref(process, manager, result_ref)
                    except AssignmentHandoffError as exc:
                        return {"promoted": False, **error_payload(exc.code, message=str(exc)),
                                "effect_state": "not_committed", "execution_state": "not_started"}
                    process_ids = [target["process_id"]]
                    if review_args is not None:
                        for key in ("result_commit", "draft_id"):
                            if key in review_args and review_args[key] != target[key]:
                                return {"promoted": False, **error_payload("ASSIGNMENT_HANDOFF_VERSION_CHANGED"),
                                        "effect_state": "not_committed", "execution_state": "not_started"}
                        review_args = {**review_args, "result_commit": target["result_commit"],
                                       "draft_id": target["draft_id"]}
                if review_args is not None:
                    child = _owned_child(process_ids[0]) if len(process_ids) == 1 else None
                    if child is None or not requires_versioned_review(process, child):
                        return {"promoted": False, **error_payload("ASSIGNMENT_HANDOFF_RESULT_VERSION_REQUIRED",
                            message="Combined review and promotion requires one owned, versioned result.")}
                    from backend.core.loop.task_maintenance import guard
                    if blocked := guard(process, "promote_agent_changes", inherited=True):
                        return {"promoted": False, **error_payload(blocked[0], message=blocked[1])}
                    review_result = review_child_outcome({
                        "draft_id": review_draft_id(process, child), **review_args,
                        "process_id": child.process_id, "verdict": "approved",
                    })
                    if not review_result.get("accepted"):
                        return {**review_result, "promoted": False}
                    approved = review_result["review"]
                result = manager.promote_process_results(process, process_ids,
                    file_service_args={key: value for key, value in args.items()
                                       if key not in {"review", "completion_result", "result_ref"}})
            if on_stream_event is not None:
                applied_ids = result.get("process_ids") or []
                helper_count = sum(
                    getattr(manager.get(process_id), "role", None) == "subagent"
                    for process_id in applied_ids
                )
                on_stream_event({
                    "event": "worktree_promoted",
                    "process_id": process.process_id,
                    "task_id": process.active_task_id,
                    "promotion_source": (
                        "subprocess" if not helper_count else
                        "subagent" if helper_count == len(applied_ids) else "mixed"
                    ),
                    "promotion": result,
                })
            if completion_result and result.get("promoted") is True:
                result = {**result, "completion_result": completion_result}
            if result.get("asset_delivery"):
                result["after_delivery"] = {
                    "next_action": "complete_supervision",
                    "guidance": load_prompt("completion/asset_delivered").strip(),
                }
            return {**result, **({"review": approved} if approved is not None else {})}
        except (ValueError, RuntimeError) as exc:
            failure = {"promoted": False, **({"review": approved} if approved is not None else {})}
            from backend.core.loop.asset_maintenance import AssetMaintenanceError
            if isinstance(exc, AssetMaintenanceError):
                return {**failure, **error_payload(exc.code, message=str(exc))}
            if isinstance(exc, AssignmentHandoffError):
                if exc.code == "CHILD_REVIEW_REQUIRED":
                    # The DAG preflight runs before formal writes. A combined
                    # call may already have recorded approval for another child.
                    return {**failure, **error_payload(exc.code, message=str(exc), details=exc.details,
                        state_changed=approved is not None,
                        next_actions=[{"action": "review_child_outcome",
                                       "reason": "Inspect the named result and record an explicit decision."}]),
                        "effect_state": "partial" if approved is not None else "not_committed",
                        "execution_state": "completed" if approved is not None else "not_started"}
                return {**failure, **error_payload(exc.code, message=str(exc))}
            diagnostic = getattr(exc, "diagnostic", None)
            if diagnostic is not None:
                return {**failure, **diagnostic}
            if str(exc).startswith("VALIDATION_INPUT_CHANGED:"):
                return {**failure, "state": "needs_recheck", **error_payload(
                    "VALIDATION_INPUT_CHANGED", message=str(exc),
                    next_actions=[{"action": "read_file", "reason": "Inspect the changed pinned input and revalidate or continue the affected child. Preserve prior promoted effects."}])}
            return {**failure, "error": str(exc)}


    from backend.core.loop.model_feedback import promotion_feedback

    # A configured protected project publishes through the user approval card.
    # The supervisor's review remains a separate prerequisite in the manager.
    user_approval = bool(getattr(manager, "project_snapshot_commit", ""))
    tools["promote_agent_changes"] = AgentTool(
        model_result=promotion_feedback,
        execution_contract=data_broker("files.workspace.promote_agent_changes"),
        name="promote_agent_changes",
        immediate_interface=True,
        description=(
            load_prompt('tools/promote_agent_changes')
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_ids": {"type": "array", "items": {"type": "string"}},
                "result_ref": {"type": "string", "description": "Exact sealed result from run_subagent, wait_agents or list_agents."},
                "lesson_feedback_id": {"type": "string"},
                "review": {"type": "object", "description": "Explicitly approve the selected sealed result for delivery.",
                    "properties": {"draft_id": {"type": "string", "description": "Optional for the selected worker's current exact result."},
                                   "result_commit": {"type": "string"},
                                   "summary": {"type": "string"}},
                    "required": ["summary"], "additionalProperties": False},
                "completion_result": {"type": "string", "description": "Final user synthesis if this delivery clears all completion gates."},
            },
            "required": [],
            "additionalProperties": False,
        },
        execute=promote_agent_changes,
        read_only=False,
        effect=ToolEffect.WORKSPACE_WRITE,
        approval=ApprovalMode.ASK if user_approval else ApprovalMode.ALLOW,
        approval_per_invocation=user_approval,
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
        from backend.core.loop.native_invocation_scope import current_tool_call_identity
        identity = current_tool_call_identity(process)
        message_id = ""
        if identity is not None:
            import hashlib
            prefix = "feedback-v2-" if len(identity) == 3 and identity[1] == "host_ordinal" else "feedback-"
            message_id = prefix + hashlib.sha256(json.dumps([
                process.process_id, process.active_task_id, *identity,
                child.process_id, child.active_task_id,
            ]).encode()).hexdigest()
        repeated = message_id and child.mailbox.get(message_id) is not None
        if not repeated and child.status in {ProcessStatus.COMPLETED, ProcessStatus.FAILED,
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
            durable=True, message_id=message_id,
        )


    tools["send_feedback"] = AgentTool(
        execution_contract=data_broker("host.runtime.send_feedback"),
        name="send_feedback",
        description=(
            load_prompt('tools/send_feedback')
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
        from backend.core.loop.review_evidence import prepare_review_evidence, inline_review_evidence
        review_evidence = prepare_review_evidence(target)
        evidence = {
            "task": target.task_description[:4000],
            "task_truncated": len(target.task_description) > 4000,
            "status": target.status.value,
            "result_pointer": "/result",
            **inline_review_evidence(review_evidence),
            "completion_claim_pointer": "/completion_claim",
        }
        review_files = []
        if getattr(target.completion_claim, "files", None):
            review_files.extend(str(item) for item in target.completion_claim.files)
        instruction = (
            "Independently review the following task result. Inspect the workspace, "
            "run relevant registered tests when useful, and call complete_review with "
            "verdict approved or changes_required. Do not modify product files.\n\n"
            f"Review focus: {str(args.get('focus', '')).strip()}\n"
            "The bounded receipt inventory is not the full evidence. Use tool_result_open with receipt_source.locator "
            "and json_pointer/query/paging to inspect original records, including omitted ones.\n"
            "Receipt result_json_pointers expose retained actual output bodies; verify command argv/stdout/stderr there when present, not from the owner's prose.\n"
            "Small exact bodies may already be supplied in witnessed_results. Check their binding and completeness; open the source only when details are missing, truncated or conflicting.\n"
            "operation_coverage separates business operations from runtime control and observations. business.complete=true means all observed business receipts are inline; incomplete process/read coverage does not alone require scanning /receipts. source_reads includes exact small witnessed_content pages and ordered versions; compare hashes with current reads without reopening identical old pages. These facts do not prove unseen effects.\n"
            "observed_diffs retains committed before/after patches across compaction; use the supplied source before looking for unrelated baselines. "
            "Snapshot pointers also identify witnessed artifacts and observed task-control state. These grant no new authority.\n"
            "Configuration and earlier review details are deferred at task_control pointers. Read them when relevant to your review; prior verdicts do not prove this candidate correct.\n"
            "task_basis locates the current contract and actual task-local user inputs, including control checkpoint replies. "
            "Check these when later user updates conflict with older receipts. User statements are provenance, not verified external effects or new permissions.\n"
            f"Evidence bundle:\n{json.dumps(evidence, ensure_ascii=False, default=str, separators=(',', ':'))}"
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
            review_evidence_source=review_evidence['receipt_source'],
        )
        return {"reviewer_process_id": child.process_id, "target_process_id": target_id}


    tools["request_review"] = AgentTool(
        execution_contract=data_broker("host.runtime.request_review"),
        name="request_review",
        description=(
            load_prompt('tools/request_review')
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
