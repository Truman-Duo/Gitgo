"""User-owned task restrictions on the shared mechanism lifecycle."""
import re
import json

from . import mechanism_lifecycle as lifecycle


_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,95}\Z")
_ACTIONS = {"deny", "ask", "manual_exempt"}
_CONTROL = frozenset({"complete_task", "complete_supervision", "complete_review", "status",
    "task_mechanism", "harness_mechanism", "request_permission", "request_user_decision",
    "decision_evidence", "tool_result_open", "capability_status", "declare_task_contract",
    "request_self_execute", "send_feedback", "wait_agents"})


class InvalidPolicyTarget(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def _name(value):
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def _literal(value):
    return (type(value) in {type(None), bool, int} or (type(value) is str and len(value) <= 2000)
            or (type(value) is list and len(value) <= 32
                and all(type(item) is str and len(item) <= 512 for item in value)))


def _validate(parameters):
    fields = {"include_children", "allow_only", "rules"}
    if (type(parameters) is not dict or not fields <= parameters.keys() or parameters.keys() - fields - {"status"}
            or type(parameters["include_children"]) is not bool or type(parameters["rules"]) is not list
            or len(parameters["rules"]) > 32):
        raise ValueError("TASK_EXECUTION_POLICY_INVALID")
    if parameters.get("status", "enabled") not in ("enabled", "disabled", "deleted"):
        raise ValueError("TASK_EXECUTION_POLICY_INVALID_STATUS")
    allowed = parameters["allow_only"]
    if allowed is not None and (type(allowed) is not list or len(allowed) > 80
            or not all(map(_name, allowed)) or len(set(allowed)) != len(allowed)):
        raise ValueError("TASK_EXECUTION_POLICY_INVALID_ALLOWLIST")
    for rule in parameters["rules"]:
        if (type(rule) is not dict or not {"tool", "action"} <= set(rule)
                or set(rule) - {"tool", "action", "arguments"} or not _name(rule["tool"])
                or rule["tool"] in _CONTROL or type(rule["action"]) is not str or rule["action"] not in _ACTIONS):
            raise ValueError("TASK_EXECUTION_POLICY_INVALID_RULE")
        arguments = rule.get("arguments", {})
        if (type(arguments) is not dict or len(arguments) > 8
                or not all(_name(key) and _literal(value) for key, value in arguments.items())):
            raise ValueError("TASK_EXECUTION_POLICY_INVALID_ARGUMENTS")


def _validate_targets(parameters, catalog):
    from jsonschema import Draft202012Validator

    def require_tool(name):
        if name not in catalog:
            raise InvalidPolicyTarget("TASK_EXECUTION_POLICY_UNKNOWN_TOOL",
                f"{name} is not in the installed tool catalog. Check the exact name through capability_status.",
                tool=name)
        return catalog[name]

    for name in parameters["allow_only"] or ():
        require_tool(name)
    for rule in parameters["rules"]:
        name = rule["tool"]
        schema = require_tool(name).parameters
        validator = Draft202012Validator(schema)
        properties = schema.get("properties", {})
        for key, value in rule.get("arguments", {}).items():
            if key not in properties:
                raise InvalidPolicyTarget("TASK_EXECUTION_POLICY_UNKNOWN_ARGUMENT",
                    f"{name} has no declared public argument named {key}.", tool=name, argument=key,
                    public_arguments=sorted(properties))
            if not validator.evolve(schema=properties[key]).is_valid(value):
                raise InvalidPolicyTarget("TASK_EXECUTION_POLICY_INVALID_MATCH_VALUE",
                    f"The exact match value for {name}.{key} does not fit its registered argument schema.",
                    tool=name, argument=key)


DEFINITION = lifecycle.Definition("task.execution_policy", 1,
    (("include_children", False), ("allow_only", None), ("rules", [])),
    ("tool_admission", "permission_broker", "manual_unit_continuation", "dashboard"),
    (("tool_operation_policy", 1), ("permission_broker", 1)), _validate, "invocation_admission")


def activate(process, task_id, parameters, expected_revision, store, *, decision_id=""):
    if not isinstance(task_id, str) or not task_id or process.active_task_id != task_id or process.parent_id:
        raise ValueError("MECHANISM_TARGET_CHANGED")
    return lifecycle.activate(process, DEFINITION, parameters, expected_revision, store, task_id=task_id,
        decision_id=decision_id)


def manage(process, task_id, operation, expected_revision, store):
    with lifecycle.fence(process):
        record = current(process)
        if not record or record["task_id"] != task_id or record["parameters"].get("status") == "deleted":
            raise ValueError("TASK_EXECUTION_POLICY_NOT_FOUND")
        status = {"enable": "enabled", "disable": "disabled", "delete": "deleted"}[operation]
        params = (dict(DEFINITION.default_parameters) if status == "deleted" else record["parameters"])
        return describe(activate(process, task_id, {**params, "status": status}, expected_revision, store))


def propose(process, change, catalog):
    if process.parent_id or process.actor_kind != "supervisor" or not process.active_task_id:
        raise ValueError("TASK_EXECUTION_POLICY_OWNER_REQUIRED")
    from .decision_support import create_user_decision
    with lifecycle.fence(process):
        previous = lifecycle.snapshot(process, DEFINITION, task_id=process.active_task_id)
        if type(change) is not dict or not change or set(change) - {"include_children", "allow_only", "rules"}:
            raise ValueError("TASK_EXECUTION_POLICY_INVALID")
        original = previous["parameters"]
        retained = original if original.get("status") != "deleted" else dict(DEFINITION.default_parameters)
        candidate = {**retained, **change, "status": "enabled"}
        _validate(candidate)
        if candidate == {"status": "enabled", **original}:
            return {"execution_policy": describe(previous), "changed": False}
        _validate_targets(candidate, catalog)
        from .tool_surface import _select_authorized_tools
        mounted, _ = _select_authorized_tools(process, catalog, issue_protocol_scopes=False)
        names = set(candidate["allow_only"] or ()) | {rule["tool"] for rule in candidate["rules"]}
        not_mounted = sorted(names.difference(mounted))
        scope = "This task and its subtasks" if candidate["include_children"] else "This task only"
        def lines(params):
            allowed = params["allow_only"]
            summary = ["This task and its subtasks" if params["include_children"] else "This task only",
                       "Allowed business tools: " + ("default tool range" if allowed is None else ", ".join(allowed) or "none")]
            if allowed is not None and params["rules"]:
                summary.append("Rules are evaluated within the allowed tool range.")
            summary.extend(f'{r["tool"]}: {r["action"]}' + (" " + json.dumps(r["arguments"], ensure_ascii=False)
                if r.get("arguments") else "") for r in params["rules"])
            return "\n".join(summary)
        labels = {"label": "Apply rules", "action": "apply", "recommended": True,
                  "principle": "Apply these exact task restrictions.", "immediate_effect": scope,
                  "downstream_effect": "Queued calls are checked again.", "risks": "Some calls may stop or require approval.",
                  "reversibility": "Disable from task controls or revise in the conversation."}
        cancel = {**labels, "label": "Keep current rules", "action": "cancel", "recommended": False,
                  "immediate_effect": "Keep the current revision.", "downstream_effect": "Continue under the current rules.",
                  "risks": "The requested revision will not take effect."}
        before = "No active rules" if original.get("status") in {"disabled", "deleted"} else lines(original)
        visible_rules = [rule for rule in candidate["rules"]
            if candidate["allow_only"] is None or rule["tool"] in candidate["allow_only"]]
        sample_rule = next((rule for rule in visible_rules if rule.get("arguments")),
                           visible_rules[0] if visible_rules else None)
        example = ({"tool": sample_rule["tool"], "arguments": sample_rule.get("arguments") or None} if sample_rule else None)
        question = f"Apply these execution rules?\n\nCurrent:\n{before}\n\nProposed:\n{lines(candidate)}"
        if example:
            match = json.dumps(example["arguments"], ensure_ascii=False) if example["arguments"] else "(any arguments)"
            question += f'\nExample matching one rule: {example["tool"]} {match}'
        if not_mounted:
            question += ("\nUnavailable on the current process: " + ", ".join(not_mounted)
                + ". These rules apply if the tool is mounted in the selected task scope.")
        if len(question) > 6000:
            raise ValueError("TASK_EXECUTION_POLICY_REVIEW_TOO_LONG")
        decision = create_user_decision(process, {"kind": "execution_policy", "state_topic": "task.execution_policy",
            "question": question, "why_user_must_decide": "Confirm how your request applies to this task's tools.",
            "options": [labels, cancel], "allow_free_form": False}, display_template="task_execution_policy",
            display_context={"task_id": process.active_task_id, "revision": previous["revision"],
                "parameters": candidate, "previous_parameters": original,
                "not_mounted_tools": not_mounted, "match_example": example})
        return {"status": "awaiting_user", "decision_id": decision["decision_id"], "changed": False}


def current(process):
    if not process or not hasattr(process, "session"):
        return None
    root = lifecycle.owner(process)
    task_id = root.active_task_id
    if not task_id or f"{DEFINITION.identifier}/{task_id}" not in root.session.mechanism_instances:
        return None
    record = lifecycle.snapshot(root, DEFINITION, task_id=task_id)
    return record if process is root or record["parameters"]["include_children"] else None


def projection(process):
    try:
        state = current(process)
    except ValueError as error:
        return {"phase": "blocked", "error": str(error)}
    if not state:
        return None
    return describe(state)


def describe(state):
    return {"task_id": state["task_id"], "revision": state["revision"], "phase": state["phase"],
            "parameters": state["parameters"]}


def hidden_tools(process, names):
    state = current(process)
    if not state or state["parameters"].get("status", "enabled") != "enabled":
        return set()
    params = state["parameters"]
    denied = {r["tool"] for r in params["rules"] if r["action"] == "deny" and not r.get("arguments")}
    allowed = params["allow_only"]
    if allowed is not None:
        denied.update(set(names) - set(allowed) - _CONTROL)
    return denied


def matches(process, tool_name, arguments):
    if tool_name in _CONTROL:
        return None, frozenset()
    state = current(process)
    if not state or state["parameters"].get("status", "enabled") != "enabled":
        return None, frozenset()
    params = state["parameters"]
    if params["allow_only"] is not None and tool_name not in params["allow_only"]:
        return state, frozenset({"deny"})
    return state, frozenset(rule["action"] for rule in params["rules"] if rule["tool"] == tool_name
        and all(key in arguments and type(arguments[key]) is type(value) and arguments[key] == value
                for key, value in rule.get("arguments", {}).items()))
