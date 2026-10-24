"""Mount trusted definitions separately from granting execution authority."""
from .agent_tool import AgentTool, ToolEffect, ApprovalMode, CancellationMode
from .execution_contract import NATIVE_PROCESS, NATIVE_WORKSPACE_PRODUCER, data_broker


def mounted_tool(spec):
    spec = dict(spec)
    from backend.core.tools.dynamic_tools import validate_custom_tool_name
    validate_custom_tool_name(str(spec.get("name") or ""))
    mode = spec.get("execution_mode")
    if mode in {"host_pipeline", "authored_host_python"}:
        contract = data_broker("host.composite")
    elif mode in {"authored_pure_python", "authored_privileged_python"}:
        contract = NATIVE_WORKSPACE_PRODUCER if mode == "authored_privileged_python" else NATIVE_PROCESS
    else:
        raise ValueError("Unknown dynamic execution mode")
    privileged = mode == "authored_privileged_python"
    effect = ToolEffect(spec.get("effect") or "read")
    parameters = spec.get("parameters")
    description = str(spec.get("description") or "")
    if privileged:
        from copy import deepcopy
        from .agent_tool import normalize_tool_parameters
        parameters = deepcopy(normalize_tool_parameters(parameters))
        fields = parameters.setdefault("properties", {})
        if {"candidate_ref", "lesson_feedback_id"}.intersection(fields):
            raise ValueError("CUSTOM_TOOL_INVALID: candidate_ref and lesson_feedback_id are reserved native continuation parameters")
        fields.update(candidate_ref={"type": "string"}, lesson_feedback_id={"type": "string"})
        from backend.core.prompt_templates import load_prompt
        description += " " + load_prompt("tools/privileged_custom_output").strip()

    return AgentTool(spec["name"], description, parameters,
        lambda args: {"error": "DYNAMIC_TOOL_REQUIRES_PIPELINE"}, read_only=effect == ToolEffect.READ,
        execution_contract=contract,
        effect=effect, resources=list(spec.get("resources") or []), timeout=float(spec.get("timeout") or 30),
        cancellation=CancellationMode.COOPERATIVE if mode in {"host_pipeline", "authored_host_python"} else CancellationMode.ISOLATED_PROCESS,
        approval=ApprovalMode.ASK if privileged else ApprovalMode.ALLOW,
        approval_per_invocation=privileged, composite_spec=spec)


def restore_pending_definitions(process, catalog):
    recovery = process.runtime_preferences.get("pending_definition_recovery")
    if not recovery or not process.pending_tool_batch:
        return
    if process.pending_tool_batch.get("decision_action") == "deny":
        process.runtime_preferences.pop("pending_definition_recovery", None)
        return
    from .composite_runtime import state_for, call_key
    definitions, prepared = recovery.get("definitions") or {}, {}
    for name, spec in definitions.items():
        mode = spec.get("execution_mode")
        if mode not in {"host_pipeline", "authored_host_python"}:
            continue
        if mode == "authored_host_python":
            storage = process.bound_storage
            if storage is None:
                raise ValueError("COMPOSITE_RECOVERY_STORAGE_UNAVAILABLE")
            saved = storage.load_custom_tool(name, version=int(spec["version"]))
            if saved["spec"].get("digest") != spec.get("digest") or saved["source_ref"] != spec.get("source_ref"):
                raise ValueError("COMPOSITE_RECOVERY_DEFINITION_CHANGED")
        prepared[name] = mounted_tool(spec)
    combined, batch = {**catalog, **prepared}, process.pending_tool_batch
    for index, call in enumerate(batch["tool_calls"]):
        name = call.get("name")
        if name in definitions and name not in prepared:
            raise ValueError("COMPOSITE_RECOVERY_REQUIRES_NEW_AUTHORIZATION")
        if name in prepared:
            state = state_for(process, call_key(call, batch["execution_id"], index), prepared[name], call.get("args") or {}, combined)
            if batch.get("nested_permission") and state is None:
                raise ValueError("COMPOSITE_RECOVERY_CONTINUATION_MISSING")
    if process.actor_kind == "supervisor" and prepared:
        old = recovery.get("lease") or {}
        task_id = process.active_task_id or process.process_id
        source = next((e for e in reversed(process.session.host_ledger)
            if e.get("event") == "user_decision_received" and e.get("task_id") == task_id
            and e.get("process_id") == process.process_id and e.get("decision_id") == batch.get("decision_id")
            and e.get("selected_action") in {"allow_once", "allow_task"}), None)
        if not source or old.get("task_id") != task_id or old.get("requested_by") != process.process_id:
            raise ValueError("COMPOSITE_RECOVERY_USER_SOURCE_REQUIRED")
        from .capabilities import CapabilityProfiles
        from .tools import ToolRegistry
        process.capability_lease = CapabilityProfiles.issue_self_execute_lease(actor_kind=process.actor_kind,
            task_id=task_id, requested_by=process.process_id, profile_id=old["profile_id"],
            reason="Explicit user continuation of the original bounded Host tool calls",
            intended_actions=list(old["intended_actions"]))
        process.tool_registry = ToolRegistry(CapabilityProfiles.resolve_tools(process.capability_profile_id, lease=process.capability_lease))
        process.session.host_ledger.append({"event": "recovery_capability_rebound", "task_id": task_id,
            "process_id": process.process_id, "decision_id": source["decision_id"],
            "previous_lease_id": old["lease_id"], "lease_id": process.capability_lease.lease_id,
            "profile_id": process.capability_lease.profile_id})
    process.dynamic_tools.update(prepared)
    for name in prepared:
        process.tool_registry.add_scoped(name)
    process.runtime_preferences.pop("pending_definition_recovery", None)
