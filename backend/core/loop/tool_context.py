"""Progressive tool information at existing model/call boundaries.

The live tool surface remains the only execution authority. These views are
read-only information, never a lease, approval, receipt or schema validator.
"""
from copy import deepcopy
import json
from backend.core.prompt_templates import load_prompt

from .provider_protocol import deterministic_hash

COMPACT_CONTRACT_CHARS = 2048


def tool_behavior(tool):
    behavior = {
        "effect": getattr(tool.effect, "value", tool.effect),
        "cancellation": getattr(tool.cancellation, "value", tool.cancellation),
    }
    if tool.immediate_interface:
        behavior["immediate_interface"] = True
    return behavior


def tool_definition(tool):
    value = deepcopy(tool.to_openai_function()["function"])
    value["runtime_behavior"] = tool_behavior(tool)
    return value


def tool_detail_definition(tool, args=None):
    definition = tool_definition(tool)
    if tool.detail_description is not None:
        definition["description"] = tool.detail_description(args)
    return definition


def _call_shape(schema):
    shape = {key: schema[key] for key in ("type", "required", "enum", "additionalProperties") if key in schema}
    if "properties" in schema:
        shape["properties"] = {name: _call_shape(item) for name, item in schema["properties"].items()}
    if isinstance(schema.get("items"), dict):
        shape["items"] = _call_shape(schema["items"])
    return shape


def contract_overview(definition):
    """A bounded description is explicitly a preview, not an exact schema."""
    encoded = json.dumps(definition, ensure_ascii=False, separators=(",", ":"))
    behavior = definition.get("runtime_behavior") or {}
    immediate = behavior.get("immediate_interface")
    if not immediate and len(encoded) <= COMPACT_CONTRACT_CHARS:
        return deepcopy(definition)
    schema = definition.get("parameters") or {}
    properties = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    primary = list(dict.fromkeys(required + (["operation"] if "operation" in properties else [])))
    basics = {}
    for name in primary:
        item = properties.get(name) or {}
        basics[name] = {"type": deepcopy(item["type"])} if "type" in item else {}
        if "enum" in item and len(json.dumps(item["enum"], ensure_ascii=False)) <= 256:
            basics[name]["enum"] = deepcopy(item["enum"])
    purpose = str(definition.get("description") or "").split("\n", 1)[0][:240]
    if immediate:
        # Preserve the input shape. The exact limits and manual remain
        # discoverable and the original pipeline still checks every argument.
        compact = {**definition, "description": purpose, "parameters": _call_shape(schema)}
        if len(json.dumps(compact, ensure_ascii=False, separators=(",", ":"))) <= COMPACT_CONTRACT_CHARS:
            return compact
    return {"name": definition["name"], "description": purpose,
            "parameters": {"type": "object", "properties": basics, "required": required},
            "optional_parameters": [name for name in properties if name not in primary],
            "runtime_behavior": deepcopy(definition.get("runtime_behavior") or {}),
            "detail_state": "on_demand", "contract_digest": deterministic_hash(definition)}


def visible_tool_contracts(messages, session_id):
    """Identify exact contracts in the selected request, including discovery."""
    contracts = set()
    for message in messages:
        if (message.get("host_authority") is not True or message.get("context_purpose") != "tool_contract"
                or message.get("provider_visible") is False):
            continue
        data = message.get("data") or {}
        if (not isinstance(data, dict) or data.get("session_id") != session_id
                or not data.get("contract_digest")):
            continue
        name = message.get("_tool_name")
        if name == "capability_status" and data.get("kind") == "tool_discovery":
            name = (data.get("definition") or {}).get("name")
        elif data.get("kind") != "tool_contract_details":
            continue
        if name:
            contracts.add((name, data["contract_digest"]))
    return contracts


def required_tool_context(process, calls, tools):
    session = process.session
    snapshot = getattr(session, "provider_context_cache", {}).get("snapshot") or {}
    if "runtime.tool_contracts" not in snapshot:
        return {}  # No projected progressive interface in this legacy caller.
    required = {}
    visible = session.request_tool_contracts
    for index, call in enumerate(calls):
        tool = tools.get(call["name"])
        if tool is None or tool.immediate_interface:
            continue  # Direct invocations use the ordinary Pipeline.
        definition = tool_detail_definition(tool, call.get("args") or {})
        if tool.detail_description is None:
            overview = contract_overview(definition)
            exposed = snapshot['runtime.tool_contracts'].get(tool.name)
            grouped = 'runtime.tool_groups' in snapshot
            compact_current = bool(exposed and 'detail_state' not in exposed and all(
                definition.get(key) == value for key, value in exposed.items()))
            if (not grouped and overview.get("detail_state") != "on_demand") or compact_current:
                continue
        identity = deterministic_hash(definition)
        if visible is None:
            # Direct callers may not have provider transactions to pair.
            from .provider_history import project_tool_history
            visible = visible_tool_contracts(project_tool_history(session.messages), session.session_id)
        if (tool.name, identity) not in visible:
            required[index] = (definition, identity)
    return required


def provide_tool_context(process, calls, required, emit=None):
    """Pair an unexecuted intent with details before requesting refinement."""
    session = process.session
    supplied = set()
    instruction = load_prompt("runtime/tool_context_wait").strip()
    for index, call in enumerate(calls):
        name = call["name"]
        data = {"kind": "tool_contract_wait", "tool_name": name,
                "execution_state": "not_started", "executed": False, "authority_granted": False,
                "session_id": session.session_id, "task_id": process.active_task_id or process.process_id,
                "instruction": instruction}
        if index in required:
            definition, identity = required[index]
            if (name, identity) not in supplied:
                data.update(kind="tool_contract_details", contract_digest=identity, definition=definition)
                supplied.add((name, identity))
        session.append_tool_result(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            tool_name=name, tool_call_id=call.get("id", ""), data=data)
        message = session.messages[-1]
        message.update(host_authority=True, context_purpose="tool_contract",
            prompt_sections=[{"name": "Tool details: " + name, "provenance": "runtime:registered-tool",
                              "version": data.get("contract_digest", "not-requested")}])
        if emit:
            emit({"event": "tool_context_prepared", "tool_name": name,
                  "tool_call_id": call.get("id", ""), "executed": False,
                  "contract_digest": data.get("contract_digest", "")})
    return {"context_refinement": True}
