"""Short-lived investigation and patch preparation within one parent turn."""
from pathlib import Path

from backend.core.errors import error_payload
from backend.core.loop.agent_tool import AgentTool
from backend.core.loop.assignment_handoff import sealed_delivery_target, subagent_scope_digest
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.task_contract import _relative_workspace_path, get_task_contract
from backend.core.loop.task_maintenance import guard
from backend.core.tools.file_operations import protected_metadata_part


_READ_TOOLS = ("read_file", "list_files", "search_text")
_WRITE_TOOLS = ("edit_file", "write_file")
_MAX_STEPS = 12


def can_launch(process, manager) -> bool:
    if manager is None or process.role == "subagent":
        return False
    if not process.parent_id:
        return process.actor_kind == "supervisor"
    owner = manager.get(process.parent_id)
    return (process.actor_kind in {"worker", "reviewer"} and owner is not None
            and owner.actor_kind == "supervisor" and not owner.parent_id)


def _paths(root: Path, values: list) -> list[str]:
    if not isinstance(values, list) or len(values) > 12:
        raise ValueError("Provide up to 12 exact workspace file paths.")
    paths = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError("Each file must be a nonempty workspace path.")
        portable = value.replace("\\", "/")
        if any(char in portable for char in "*?[]:\0\r\n"):
            raise ValueError("Use concrete workspace-relative paths.")
        if Path(portable).is_absolute() or ".." in portable.split("/"):
            raise ValueError("Use concrete workspace-relative paths.")
        relative = _relative_workspace_path(root, value)
        if (relative == "." or (root / relative).is_dir()
                or any(protected_metadata_part(part, portable=True) for part in relative.split("/"))):
            raise ValueError("Select business files in the workspace.")
        paths.append(relative)
    return list(dict.fromkeys(paths))


def install(ctx):
    parent = ctx.process
    manager = ctx.manager
    subprocess = bool(parent.parent_id)
    batch_size, max_batches = (3, 2) if subprocess else (5, 3)

    def helper_result(child, state, basis, summary_limit):
        result_id = {"process_id": child.process_id}
        outcome = state.get("result") or {}
        if state["status"] != "completed" or not state.get("lifecycle_finalized"):
            error = outcome.get("error")
            reason = outcome.get("code") or (error.get("code") if isinstance(error, dict) else "") or ""
            return {**result_id, "status": state["status"], "child_error_code": reason,
                    **error_payload("SUBAGENT_FAILED", details={"closeout_state": state["closeout_state"]})}
        if subagent_scope_digest(parent) != basis:
            return {**result_id, **error_payload("SUBAGENT_RESULT_STALE")}
        response = str(outcome.get("response") or "")
        text_result = {**result_id, "status": "completed", "summary": response[:summary_limit]}
        if len(response) > summary_limit:
            text_result["summary_truncated"] = True
        if child.task_kind == "action":
            selection = sealed_delivery_target(parent, child, manager)
            if selection is None:
                return {**result_id, **error_payload("SUBAGENT_RESULT_STALE")}
            text_result.update(
                result_ref=selection["result_ref"],
                changed_files=child.worktree["changed_files"],
                review_path=(
                    "Read the sealed result with read_child_artifact, then use your authorized "
                    "file tools to integrate the reviewed change into your workspace."
                    if subprocess else "Inspect with read_child_artifact using result_ref and path. If approved, "
                    "use promote_agent_changes with the same result_ref and a review summary."
                ),
            )
        return text_result

    def run_subagent(args):
        if not can_launch(parent, manager) or (subprocess and not parent.tool_registry.has("run_subagent")):
            return error_payload("SUBAGENT_START_FAILED", message="This execution cannot launch a helper.")
        # The pipeline rejects model-supplied private keys before adding Host metadata.
        args = {key: value for key, value in args.items() if not str(key).startswith("_")}
        batch_call = "tasks" in args
        tasks = args.get("tasks") if batch_call else [args]
        if (not isinstance(tasks, list) or not tasks
                or (batch_call and set(args) != {"tasks"})):
            return error_payload("INVALID_TOOL_ARGUMENTS", message="Provide either one task or a nonempty tasks array.")
        if len(tasks) > batch_size:
            return error_payload("SUBAGENT_BATCH_SIZE_LIMIT", details={"maximum": batch_size, "requested": len(tasks)})
        workspace = ctx.workspace_path or parent.worktree_path
        if not workspace:
            return error_payload("SUBAGENT_START_FAILED", message="The parent has no project workspace.")
        root = Path(workspace).resolve()
        contract = get_task_contract(parent)
        if subprocess:
            permitted = set(contract.get("target_files") or [])
            granted = set(parent.tool_registry.list_all())
        else:
            permitted = {item.get("path") for item in contract.get("deliverables", [])
                         if item.get("kind") == "workspace_file"}
            granted = set(_READ_TOOLS + _WRITE_TOOLS)
        plans = []
        for index, item in enumerate(tasks):
            task = item.get("task") if isinstance(item, dict) else None
            if (not isinstance(task, str) or not task.strip() or len(task) > 4000
                    or set(item) - {"task", "inputs", "write_files"}):
                return error_payload("INVALID_TOOL_ARGUMENTS", message="Each task must describe the work in 1–4000 characters.",
                                     details={"task_index": index})
            try:
                inputs = _paths(root, item.get("inputs", []))
                targets = _paths(root, item.get("write_files", []))
            except (ValueError, OSError, RuntimeError) as exc:
                return error_payload("INVALID_TOOL_ARGUMENTS", message=str(exc), details={"task_index": index})
            names = [name for name in _READ_TOOLS + (_WRITE_TOOLS if targets else ()) if name in granted]
            if targets:
                authorized = (
                    parent.actor_kind == "worker" and parent.task_kind == "action"
                    and "read_child_artifact" in granted and any(name in granted for name in _WRITE_TOOLS)
                    if subprocess else contract.get("revision") and contract.get("execution_mode") in {"delegate", "either"}
                )
                if not authorized or ("*" not in permitted and not set(targets).issubset(permitted)):
                    return error_payload("SUBAGENT_WRITE_SCOPE_REQUIRED", details={"requested_files": targets, "task_index": index})
                if blocked := guard(parent, inherited=True):
                    return error_payload(blocked[0], message=blocked[1])
                if manager.worktree_manager is None or (subprocess and not parent.worktree.get("isolated")):
                    return error_payload("SUBAGENT_WORKTREE_REQUIRED")
            instructions = [task.strip()]
            if inputs:
                instructions.append("Input files: " + ", ".join(inputs))
            if targets:
                instructions.append("Prepare an isolated candidate patch. Edit only these paths: " + ", ".join(targets))
            instructions.append("Return a concise result with evidence (such as file and line) and any unresolved limits.")
            plans.append(dict(task_description="\n\n".join(instructions),
                profile_id="development.workspace" if targets else "governance.observe",
                task_kind="action" if targets else "answer", target_files=targets, allowed_tools=names,
                display_name="Patch subagent" if targets else "Investigation subagent"))

        if not parent._subagent_batch_lock.acquire(blocking=False):
            return error_payload("SUBAGENT_BATCH_IN_PROGRESS")
        try:
            return execute_batch(plans, batch_call)
        finally:
            parent._subagent_batch_lock.release()

    def execute_batch(plans, batch_call):
        budget = parent.task_budget
        task_id = parent.active_task_id or parent.process_id
        quota = parent.runtime_preferences.get("subagent_batches") or {}
        used = quota.get("used", 0) if quota.get("task_id") == task_id else 0
        if used >= max_batches:
            return error_payload("SUBAGENT_TURN_BATCH_LIMIT", details={"maximum": max_batches, "used": used})
        if parent.cancellation_event.is_set():
            return error_payload("SUBAGENT_CANCELLED")
        prior_helpers = [child.process_id for child in manager.children_of(parent.process_id)
                         if child.role == "subagent" and not child.lifecycle_finalized]
        if prior_helpers and not manager.wait_many(prior_helpers, timeout=0)["all_terminal"]:
            return error_payload("SUBAGENT_BATCH_IN_PROGRESS")
        wait_seconds = min(120, budget.remaining_seconds()) if budget is not None else 120
        if wait_seconds <= 0:
            return error_payload("SUBAGENT_WAIT_EXPIRED")
        basis = subagent_scope_digest(parent)
        prepared = []
        started = []
        try:
            request = budget.subagent_batch_request(parent.process_id, len(plans), _MAX_STEPS) if budget else {}
            if budget is not None:
                budget.preflight_children(
                    [{"max_steps": _MAX_STEPS, "budget_request": request, "temporary": True} for _ in plans],
                    parent_id=parent.process_id,
                )
            for plan in plans:
                child = ctx.spawn_child(**plan, max_steps=_MAX_STEPS, acceptance_criteria=[],
                    required_for_parent_completion=False, budget_request=request,
                    start_immediately=False, temporary=True)
                prepared.append(child)
            if parent.cancellation_event.is_set() or subagent_scope_digest(parent) != basis:
                for child in prepared:
                    ctx.cancel_prepared_child(child.process_id)
                return error_payload("SUBAGENT_CANCELLED" if parent.cancellation_event.is_set() else "SUBAGENT_RESULT_STALE")
            parent.runtime_preferences["subagent_batches"] = {"task_id": task_id, "used": used + 1}
            for child in prepared:
                ctx.start_prepared_child(child.process_id)
                started.append(child)
        except (ValueError, RuntimeError, OSError, TypeError) as exc:
            started_ids = [child.process_id for child in started]
            for child in prepared:
                if child.process_id in started_ids:
                    manager.kill(child.process_id, reason="subagent_batch_admission_failed")
                elif not child.lifecycle_finalized:
                    ctx.cancel_prepared_child(child.process_id)
            if not started:
                parent.runtime_preferences["subagent_batches"] = {"task_id": task_id, "used": used}
            if started_ids:
                manager.wait_many(started_ids, timeout=5)
            return {**error_payload(getattr(exc, "code", None) or "SUBAGENT_START_FAILED",
                                    message=str(exc), details=getattr(exc, "details", None)),
                    **({"process_ids": started_ids, "batches_remaining": max_batches - used - 1} if started_ids else {})}

        ids = [child.process_id for child in started]
        closeout = manager.wait_many(ids, timeout=wait_seconds, cancel_event=parent.cancellation_event)
        cancelled = parent.cancellation_event.is_set()
        if cancelled or not closeout["all_terminal"]:
            reason = "parent_cancelled" if cancelled else "subagent_wait_expired"
            for child_id in ids:
                manager.kill(child_id, reason=reason)
            terminal = manager.wait_many(ids, timeout=5)
            return {**({"process_ids": ids} if batch_call else {"process_id": ids[0]}),
                    "closeout_pending": not terminal["all_terminal"], "batches_remaining": max_batches - used - 1,
                    **error_payload("SUBAGENT_CANCELLED" if cancelled else "SUBAGENT_WAIT_EXPIRED")}
        results = [helper_result(child, closeout["processes"][child.process_id], basis, min(3600, 9000 // len(ids)))
                   for child in started]
        remaining = {"batches_remaining": max_batches - used - 1}
        if not batch_call:
            return {**results[0], **remaining}
        completed = sum(result.get("status") == "completed" for result in results)
        return {"status": "completed" if completed == len(results) else "partial" if completed else "failed",
                "results": results, **remaining, **({} if completed else error_payload("SUBAGENT_FAILED"))}

    file_paths = {"type": "array", "maxItems": 12, "items": {"type": "string"}}
    properties = {
        "task": {"type": "string", "description": "Question or work to perform and expected output."},
        "inputs": {**file_paths, "description": "Optional relevant workspace paths to consult."},
        "write_files": {**file_paths, "description": "Exact authorized output paths for a patch. Omit for investigation."},
    }
    ctx.tools["run_subagent"] = AgentTool(
        execution_contract=data_broker("host.runtime.run_subagent"),
        name="run_subagent",
        immediate_interface=True,
        description=(
            f"Run one temporary helper with task, or launch up to {batch_size} helpers concurrently in one tasks array. "
            f"Each call starts one batch, with at most {max_batches} batches in the current task. "
            "Use independent questions or bounded patches. Each helper sees its own instructions and authorized "
            "workspace. For a patch, choose exact files from your issued scope and inspect the returned sealed reference."
        ),
        parameters={
            "type": "object",
            "properties": {**properties, "tasks": {
                "type": "array", "minItems": 1, "maxItems": batch_size,
                "items": {"type": "object", "properties": properties, "required": ["task"], "additionalProperties": False},
            }},
            "additionalProperties": False,
        },
        execute=run_subagent,
        read_only=True,
    )
