"""artifacts protocol tools."""
from __future__ import annotations

from backend.core.prompt_templates import load_prompt
from backend.core.loop.execution_contract import HOST_COMPUTE, data_broker
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.models import ProcessStatus

def install(ctx):
    process = ctx.process
    tools = ctx.tools
    manager = ctx.manager
    _owned_child = ctx.owned_child
    def _owned_child(process_id: str):
        target = manager.get(process_id) if manager else None
        if manager is not None and target is None:
            child_ids, contracts, _ = process.coordination_snapshot()
            if process_id in child_ids and process_id in contracts:
                from backend.core.loop.conversation_continuation import resolve_conversation_process
                target = resolve_conversation_process(manager, getattr(manager, "session_store", None),
                    ctx.workspace_path, process_id)
        if manager is None or not manager.owns_child(process, target):
            return None
        return target


    def read_child_artifact(args: dict) -> dict:
        if manager is None:
            return {"error": "AgentProcessManager is unavailable"}
        from backend.core.errors import error_payload
        from backend.core.loop.assignment_handoff import AssignmentHandoffError, resolve_delivery_ref

        process_id = str(args.get("process_id") or "")
        expected_commit = ""
        if process.actor_kind != "supervisor" and not args.get("result_ref"):
            return error_payload("INVALID_TOOL_ARGUMENTS", message="Use the sealed result_ref from your helper.")
        if result_ref := args.get("result_ref"):
            if process_id or args.get("draft_id"):
                return error_payload("INVALID_TOOL_ARGUMENTS", message="Select result_ref by itself.")
            try:
                target = resolve_delivery_ref(process, manager, result_ref)
            except AssignmentHandoffError as exc:
                return {"available": False, **error_payload(exc.code, message=str(exc))}
            process_id, expected_commit = target["process_id"], target["result_commit"]
        if not process_id:
            return error_payload("INVALID_TOOL_ARGUMENTS", message="Select one result_ref or supply process_id.")
        child = _owned_child(process_id)
        if child is None:
            raise PermissionError("artifact process must be an owned child task")
        if process.actor_kind != "supervisor":
            scope = process.delegated_contracts.get(process_id) or {}
            path = str(args.get("path") or "").replace("\\", "/")
            if child.role != "subagent" or path not in scope.get("target_files", []):
                return error_payload("SUBAGENT_WRITE_OUTSIDE_SCOPE", message="Read a file from the helper's authorized patch scope.")
        if draft_id := str(args.get("draft_id") or ""):
            from backend.core.loop.assignment_handoff import draft_event
            from backend.core.loop.worktree import WorktreeError
            try:
                event = draft_event(process, draft_id, process_id)
                draft = event["draft"]
                path = str(args.get("path") or "").replace("\\", "/")
                if path not in draft["files"]:
                    raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DRAFT_NOT_FOUND",
                                                 "Select a file listed on this exact draft.")
                result = manager.worktree_manager.read_artifact(
                    draft["commit"], path, sha256=str(args.get("sha256") or ""),
                    offset=int(args.get("offset") or 0), max_chars=int(args.get("max_chars") or 24000))
                return {"process_id": process_id, "authority": "versioned_draft",
                        "draft_id": draft_id, "version": draft["version"],
                        "current_draft_id": event["current_draft_id"], **result}
            except AssignmentHandoffError as exc:
                return {"available": False, **error_payload(exc.code, message=str(exc))}
            except WorktreeError as exc:
                return {"available": False, **error_payload("ASSIGNMENT_HANDOFF_DRAFT_UNAVAILABLE", message=str(exc))}
        returned = (process.delegated_contracts.get(process_id) or {}).get("returned_assignment")
        if returned:
            draft = returned["draft"]
            path = str(args.get("path") or "").replace("\\", "/")
            if path not in draft["files"]:
                raise ValueError("path must identify a retained file from this returned assignment")
            return {"process_id": process_id, "authority": "retained_draft",
                    **manager.worktree_manager.read_artifact(
                        draft["commit"], path, sha256=str(args.get("sha256") or ""),
                        offset=int(args.get("offset") or 0), max_chars=int(args.get("max_chars") or 24000))}
        if child.status != ProcessStatus.COMPLETED or (
            child.process_id in manager._threads and not child.lifecycle_finalized
        ):
            from backend.core.loop.assignment_handoff import handoff_status
            from backend.core.loop.task_maintenance import state_for
            handoff = next((item for item in handoff_status(process, state_for(process))
                            if item["source_process_id"] == process_id and item["next_action"]), None)
            return {"available": False, **error_payload(
                "CHILD_ARTIFACT_NOT_READY",
                details={"process_id": child.process_id, "status": child.status.value,
                         "lifecycle_finalized": child.lifecycle_finalized,
                         **({"handoff": handoff} if handoff else {})},
                next_actions=(None if handoff else [{"action": "list_agents", "effect": "Inspect the owned execution before choosing wait, failure review or continuation."}]),
            )}
        if child.worktree.get("isolated") or child.worktree.get("retained_result"):
            from backend.core.loop.worktree import WorktreeArtifactUnavailable

            path = str(args.get("path") or "")
            try:
                return manager.read_sealed_artifact(
                    process, process_id, path, sha256=str(args.get("sha256") or ""),
                    offset=int(args.get("offset", 0) or 0), max_chars=int(args.get("max_chars", 24000) or 24000),
                    expected_commit=expected_commit,
                )
            except AssignmentHandoffError as exc:
                return {"available": False, **error_payload(exc.code, message=str(exc))}
            except WorktreeArtifactUnavailable:
                return {"available": False, **error_payload("CHILD_ARTIFACT_PATH_UNAVAILABLE",
                    details={"path": path, "result_commit": child.worktree.get("result_commit"),
                             "changed_files": child.worktree.get("changed_files", [])[:64]})}
        # Non-Git projects deliberately use the shared workspace.  Keep
        # the same owner/completion boundary, then reuse the canonical
        # bounded artifact reader instead of advertising an immutable
        # worktree API that can only fail in this mode.
        result = tools["artifact_read"].execute({
            "path": str(args.get("path") or ""),
            "sha256": str(args.get("sha256") or ""),
            "offset": int(args.get("offset", 0) or 0),
            "max_chars": int(args.get("max_chars", 24000) or 24000),
        })
        return {
            "process_id": child.process_id,
            "authority": "shared_workspace_current",
            **result,
        }


    tools["read_child_artifact"] = AgentTool(
        execution_contract=data_broker("host.runtime.read_child_artifact"),
        name="read_child_artifact",
        immediate_interface=True,
        description=(
            load_prompt('tools/read_child_artifact')
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_id": {"type": "string"},
                "result_ref": {"type": "string", "description": "Exact sealed result from run_subagent, wait_agents or list_agents."},
                "path": {"type": "string"},
                "draft_id": {"type": "string", "description": "Exact draft ID for a historical or current draft read."},
                "sha256": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1000},
            },
            "required": ["path"],
        },
        execute=read_child_artifact,
        read_only=True,
        effect=ToolEffect.READ,
        idempotent=True,
    )


    ctx.owned_child = _owned_child
