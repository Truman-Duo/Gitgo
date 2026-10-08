"""Task-scoped declarative composite tools.

``define_tool`` never evaluates model-authored Python.  It composes tools that
the current process already owns, preserving the union of their effects,
resources and cancellation constraints.
"""

from __future__ import annotations

import hashlib
import ast
import json
import re
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from backend.core.loop.agent_tool import normalize_tool_parameters


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_PLACEHOLDER_RE = re.compile(r"^\$\{(input|steps)\.([A-Za-z0-9_.-]+)\}$")
_SAFE_CALLS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
    "int", "len", "list", "map", "max", "min", "range", "reversed",
    "round", "set", "sorted", "str", "sum", "tuple", "zip",
}


def validate_authored_definition(raw: dict, workspace: str) -> dict:
    """Compile one pure, task-scoped Python shortcut without adding authority."""
    name = str(raw.get("name", "")).strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError("tool name must match ^[a-z][a-z0-9_]{2,63}$")
    description = str(raw.get("description", "")).strip()
    if not description:
        raise ValueError("tool description is required")
    parameters = normalize_tool_parameters(raw.get("parameters"))
    root = __import__("pathlib").Path(workspace).resolve()
    source_path = (root / str(raw.get("source_path", ""))).resolve()
    try:
        source_path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("source_path must remain inside the execution workspace") from exc
    if source_path.suffix.lower() != ".py" or not source_path.is_file():
        raise ValueError("source_path must reference an existing .py file")
    source = source_path.read_text(encoding="utf-8-sig")
    if len(source.encode("utf-8")) > 64_000:
        raise ValueError("authored tool source exceeds 64KB")
    tree = ast.parse(source, filename=str(source_path))
    functions = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    if "run" not in functions:
        raise ValueError("authored tool source must define run(args)")
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if not isinstance(node, ast.FunctionDef):
            raise ValueError("authored tool module may contain only function definitions")
    forbidden = (
        ast.Import, ast.ImportFrom, ast.Attribute, ast.ClassDef, ast.AsyncFunctionDef,
        ast.Lambda, ast.With, ast.AsyncWith, ast.Global, ast.Nonlocal, ast.Delete,
        ast.Try, ast.Raise, ast.Yield, ast.YieldFrom, ast.Await,
    )
    for node in ast.walk(tree):
        if isinstance(node, forbidden):
            raise ValueError(f"authored tool uses forbidden syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and str(node.id).startswith("__"):
            raise ValueError("dunder names are forbidden in authored tools")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in (_SAFE_CALLS | functions):
                raise ValueError("authored tools may call only safe builtins or local functions")
    tests = list(raw.get("tests") or [])
    if not 1 <= len(tests) <= 20:
        raise ValueError("authored tools require between 1 and 20 registration tests")
    for test in tests:
        if not isinstance(test, dict) or not isinstance(test.get("input"), dict) or "expected" not in test:
            raise ValueError("each registration test requires object input and expected")
    relative = source_path.relative_to(root).as_posix()
    spec = {
        "name": name, "description": description, "parameters": parameters,
        "source_path": relative, "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "tests": tests, "effect": "read", "resources": [],
        "timeout": max(1.0, min(float(raw.get("timeout", 30) or 30), 120.0)),
        "version": max(1, int(raw.get("_version", 1) or 1)),
        "execution_mode": "authored_pure_python",
    }
    canonical = json.dumps(spec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    spec["digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # Runtime-only registration payload. Persistent catalogs store this body
    # separately in CAS; process checkpoints retain only the immutable ref.
    spec["_source"] = source
    return spec


def validate_privileged_authored_definition(raw: dict, workspace: str) -> dict:
    """Compile an unrestricted Python tool whose authority comes from the user.

    ProcessToolRunner applies native isolation. The immutable source digest,
    purpose and declared resource/effect contract are approval and audit data;
    every registration and every invocation is explicitly approved.
    """
    name = str(raw.get("name", "")).strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError("tool name must match ^[a-z][a-z0-9_]{2,63}$")
    description = str(raw.get("description", "")).strip()
    purpose = str(raw.get("purpose", "")).strip()
    if not description or not purpose:
        raise ValueError("description and purpose are required")
    parameters = normalize_tool_parameters(raw.get("parameters"))
    root = Path(workspace).resolve()
    source_path = (root / str(raw.get("source_path", ""))).resolve()
    try:
        source_path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("source_path must remain inside the execution workspace") from exc
    if source_path.suffix.lower() != ".py" or not source_path.is_file():
        raise ValueError("source_path must reference an existing .py file")
    source = source_path.read_text(encoding="utf-8-sig")
    if len(source.encode("utf-8")) > 256_000:
        raise ValueError("privileged authored tool source exceeds 256KB")
    expected_source = str(raw.get("source_sha256") or "").lower()
    actual_source = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if expected_source != actual_source:
        raise ValueError("source_sha256 is required and must match the current source")
    tree = ast.parse(source, filename=str(source_path))
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
               for node in tree.body):
        raise ValueError("authored tool source must define run(args)")
    tests = list(raw.get("tests") or [])
    if not 1 <= len(tests) <= 20:
        raise ValueError("authored tools require between 1 and 20 registration tests")
    for test in tests:
        if not isinstance(test, dict) or not isinstance(test.get("input"), dict) or "expected" not in test:
            raise ValueError("each registration test requires object input and expected")
    effect = str(raw.get("effect") or "process")
    if effect not in {"workspace_write", "process", "external"}:
        raise ValueError("privileged effect must be workspace_write, process, or external")
    resources = [str(item).strip() for item in list(raw.get("resources") or []) if str(item).strip()]
    if not resources:
        raise ValueError("privileged tools require at least one declared resource")
    relative = source_path.relative_to(root).as_posix()
    spec = {
        "name": name, "description": description, "purpose": purpose,
        "parameters": parameters, "source_path": relative,
        "source_sha256": actual_source, "tests": tests, "effect": effect,
        "resources": resources,
        "timeout": max(1.0, min(float(raw.get("timeout", 30) or 30), 1800.0)),
        "version": max(1, int(raw.get("_version", 1) or 1)),
        "execution_mode": "authored_privileged_python",
        "requires_exact_approval": True,
    }
    canonical = json.dumps(spec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    spec["digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    spec["_source"] = source
    return spec


def execute_authored_python(args: dict) -> dict:
    """Run a statically constrained authored shortcut in the isolated runner."""
    spec = dict(args.get("_dynamic_spec") or {})
    workspace = __import__("pathlib").Path(str(args.get("_workspace") or "")).resolve()
    source = spec.get("_source")
    source_path = (workspace / str(spec.get("source_path") or "")).resolve()
    if not isinstance(source, str):
        # Compatibility for task-scoped definitions written before the
        # versioned catalog existed.
        try:
            source_path.relative_to(workspace)
        except ValueError:
            return {"error": "AUTHORED_TOOL_SOURCE_OUTSIDE_WORKSPACE"}
        source = source_path.read_text(encoding="utf-8-sig")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if digest != spec.get("source_sha256"):
        return {"error": "AUTHORED_TOOL_SOURCE_CHANGED", "expected": spec.get("source_sha256"), "actual": digest}
    safe_builtins = {name: __builtins__[name] for name in _SAFE_CALLS} if isinstance(__builtins__, dict) else {
        name: getattr(__builtins__, name) for name in _SAFE_CALLS
    }
    namespace: dict[str, Any] = {"__builtins__": safe_builtins}
    exec(compile(source, str(source_path or spec.get("name") or "authored_tool"), "exec"), namespace, namespace)
    public = {key: value for key, value in args.items() if not str(key).startswith("_")}
    value = namespace["run"](public)
    # The runner protocol requires an object and JSON serialisability.
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) > 200_000:
        return {"error": "AUTHORED_TOOL_OUTPUT_TOO_LARGE", "chars": len(encoded)}
    return value if isinstance(value, dict) else {"result": value}


def execute_authored_privileged_python(args: dict) -> dict:
    """Execute an approved immutable source version in the killable runner."""
    spec = dict(args.get("_dynamic_spec") or {})
    source = spec.get("_source")
    if not isinstance(source, str):
        return {"error": "AUTHORED_TOOL_SOURCE_MISSING"}
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if digest != spec.get("source_sha256"):
        return {"error": "AUTHORED_TOOL_SOURCE_CHANGED", "expected": spec.get("source_sha256"), "actual": digest}
    workspace = Path(str(args.get("_workspace") or "")).resolve()
    public = {key: value for key, value in args.items() if not str(key).startswith("_")}
    namespace: dict[str, Any] = {
        "__name__": f"gitgo_authored_{spec.get('name') or 'tool'}",
        "__file__": str(workspace / str(spec.get("source_path") or "tool.py")),
    }
    previous = Path.cwd()
    try:
        os.chdir(workspace)
        exec(compile(source, namespace["__file__"], "exec"), namespace, namespace)
        value = namespace["run"](public)
    finally:
        os.chdir(previous)
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) > 200_000:
        return {"error": "AUTHORED_TOOL_OUTPUT_TOO_LARGE", "chars": len(encoded)}
    return value if isinstance(value, dict) else {"result": value}


def validate_definition(raw: dict, available_tools: dict) -> dict:
    name = str(raw.get("name", "")).strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError("tool name must match ^[a-z][a-z0-9_]{2,63}$")
    if name in available_tools:
        raise ValueError(f"tool name already exists: {name}")
    description = str(raw.get("description", "")).strip()
    if not description:
        raise ValueError("tool description is required")
    parameters = normalize_tool_parameters(raw.get("parameters"))
    steps = raw.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        raise ValueError("steps must contain between 1 and 20 entries")

    normalised_steps = []
    seen_ids = set()
    effects = []
    resources = set()
    timeout = 0.0
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"step {index} must be an object")
        step_id = str(step.get("id") or f"step_{index + 1}")
        if not _NAME_RE.fullmatch(step_id) or step_id in seen_ids:
            raise ValueError(f"invalid or duplicate step id: {step_id}")
        tool_name = str(step.get("tool", ""))
        tool = available_tools.get(tool_name)
        if tool is None:
            raise PermissionError(f"composite step is not currently authorized: {tool_name}")
        if not getattr(tool, "composable", False):
            raise PermissionError(
                f"tool {tool_name} is not declared safe for composite execution"
            )
        runner_name = str(getattr(tool, "runner_name", ""))
        arguments = step.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError(f"step {step_id} arguments must be an object")
        _validate_placeholders(
            arguments,
            input_fields=set((parameters.get("properties") or {}).keys()),
            prior_steps=seen_ids,
        )
        _validate_step_contract(arguments, tool.parameters, parameters)
        _validate_effect_target_contract(tool, arguments)
        seen_ids.add(step_id)
        effects.append(getattr(getattr(tool, "effect", "read"), "value", "read"))
        resources.update(getattr(tool, "resources", None) or [])
        timeout += float(getattr(tool, "timeout", 60.0))
        normalised_steps.append({
            "id": step_id,
            "tool": tool_name,
            "runner_name": runner_name,
            "arguments": arguments,
        })

    # A composite may perform one effectful operation.  Read-only verification
    # may follow it (for example write -> read-back); the Host pipeline marks a
    # later failure rollback-required so the outer execution restores its
    # workspace snapshot.
    from backend.core.loop.operation_policy import is_effectful_mutation
    effectful = [
        i for i, effect in enumerate(effects) if is_effectful_mutation(effect)
    ]
    if len(effectful) > 1:
        raise ValueError("a composite may contain at most one effectful step")

    spec = {
        "name": name,
        "description": description,
        "parameters": parameters,
        "steps": normalised_steps,
        "effect": _strongest_effect(effects),
        "resources": sorted(resources),
        "timeout": min(max(timeout, 1.0), 1800.0),
        "version": max(1, int(raw.get("_version", 1) or 1)),
        "execution_mode": "host_pipeline",
        "requires_host_transaction": bool(
            effectful and effectful[0] != len(effects) - 1
        ),
    }
    canonical = json.dumps(spec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    spec["digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return spec


def execute_composite(args: dict) -> dict:
    definition = args.get("_dynamic_spec")
    workspace = args.get("_workspace")
    if not isinstance(definition, dict) or not workspace:
        return {"error": "DYNAMIC_TOOL_SPEC_MISSING"}
    if definition.get("requires_host_transaction"):
        # This compatibility subprocess has no access to ToolExecution's
        # content snapshot.  Never run a post-effect verification sequence
        # where it could leave a partial write behind.
        return {"error": "DYNAMIC_HOST_TRANSACTION_REQUIRED"}
    inputs = {
        key: value for key, value in args.items()
        if not key.startswith("_")
    }
    results: dict[str, dict] = {}
    files: set[str] = set()
    handlers = _handlers()
    for step in definition.get("steps", []):
        runner_name = str(step.get("runner_name", ""))
        handler = handlers.get(runner_name)
        if handler is None:
            return {"error": "DYNAMIC_HANDLER_UNAVAILABLE", "handler": runner_name}
        try:
            step_args = _resolve_value(
                deepcopy(step.get("arguments", {})), inputs=inputs, steps=results,
            )
        except ValueError as exc:
            return {
                "error": "DYNAMIC_ARGUMENT_RESOLUTION_FAILED",
                "step": step.get("id"),
                "detail": str(exc),
                "steps": results,
                "files": sorted(files),
            }
        if not isinstance(step_args, dict):
            return {"error": "DYNAMIC_ARGUMENT_RESOLUTION_FAILED", "step": step.get("id")}
        step_args["_workspace"] = workspace
        result = handler(step_args)
        if not isinstance(result, dict):
            result = {"result": result}
        step_id = str(step.get("id"))
        results[step_id] = result
        files.update(_result_files(result))
        if result.get("error"):
            return {
                "error": "DYNAMIC_STEP_FAILED",
                "failed_step": step_id,
                "step_error": result,
                "steps": results,
                "files": sorted(files),
            }
    return {"steps": results, "files": sorted(files), "completed": True}


def _handlers() -> dict:
    # Compatibility runner for old persisted definitions.  Reuse the canonical
    # registration function instead of maintaining a second handler map.
    from backend.core.tools.registrations import register_all

    handlers: dict[str, Any] = {}
    register_all(lambda name, fn: handlers.__setitem__(name, fn))
    handlers.pop("dynamic_composite", None)
    return handlers


def resolve_step_arguments(arguments: dict, *, inputs: dict, steps: dict) -> dict:
    """Resolve one compiled step without evaluating code or templates."""
    resolved = _resolve_value(deepcopy(arguments), inputs=inputs, steps=steps)
    if not isinstance(resolved, dict):
        raise ValueError("resolved step arguments must be an object")
    return resolved


def _resolve_value(value: Any, *, inputs: dict, steps: dict) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_value(item, inputs=inputs, steps=steps) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, inputs=inputs, steps=steps) for item in value]
    if not isinstance(value, str):
        return value
    match = _PLACEHOLDER_RE.fullmatch(value)
    if not match:
        return value
    root = inputs if match.group(1) == "input" else steps
    current: Any = root
    for part in match.group(2).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise ValueError(f"unresolved placeholder: {value}")
    return current


def _validate_placeholders(
    value: Any, *, input_fields: set[str], prior_steps: set[str],
) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _validate_placeholders(
                item, input_fields=input_fields, prior_steps=prior_steps,
            )
        return
    if isinstance(value, list):
        for item in value:
            _validate_placeholders(
                item, input_fields=input_fields, prior_steps=prior_steps,
            )
        return
    if not isinstance(value, str) or "${" not in value:
        return
    match = _PLACEHOLDER_RE.fullmatch(value)
    if not match:
        raise ValueError(f"placeholder must occupy the complete value: {value}")
    path = match.group(2).split(".")
    if match.group(1) == "input":
        if path[0] not in input_fields:
            raise ValueError(f"placeholder references undeclared input: {value}")
    elif path[0] not in prior_steps:
        raise ValueError(f"placeholder references a future or unknown step: {value}")


def _validate_step_contract(arguments: dict, step_schema: dict, input_schema: dict) -> None:
    """Compile-time checks that the Host can prove without executing the step."""
    required = set(step_schema.get("required", []) or [])
    missing = sorted(required - set(arguments))
    if missing:
        raise ValueError(f"composite step omits required arguments: {', '.join(missing)}")
    step_properties = step_schema.get("properties", {}) or {}
    input_properties = input_schema.get("properties", {}) or {}
    input_required = set(input_schema.get("required", []) or [])
    for field_name, value in arguments.items():
        expected = (step_properties.get(field_name) or {}).get("type", "")
        if not expected:
            continue
        if isinstance(value, str):
            match = _PLACEHOLDER_RE.fullmatch(value)
            if match and match.group(1) == "input":
                input_name = match.group(2).split(".")[0]
                actual = (input_properties.get(input_name) or {}).get("type", "")
                if field_name in required and input_name not in input_required:
                    raise ValueError(
                        f"required step argument {field_name} uses optional input {input_name}"
                    )
                if actual and actual != expected and not (
                    expected == "number" and actual == "integer"
                ):
                    raise ValueError(
                        f"input {input_name} type {actual} is incompatible with "
                        f"step argument {field_name} type {expected}"
                    )
                continue
            if match:
                continue
        actual = _json_type(value)
        if actual != expected and not (expected == "number" and actual == "integer"):
            raise ValueError(
                f"step argument {field_name} type {actual} is incompatible with {expected}"
            )


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _validate_effect_target_contract(tool, arguments: dict) -> None:
    effect = getattr(getattr(tool, "effect", "read"), "value", "read")
    if effect != "workspace_write":
        return
    # Transaction snapshots are created before execution.  Addresses may be
    # literals or task inputs, but never outputs of earlier steps.
    address_fields = {"path", "file", "target", "source", "patch"}
    for field_name in address_fields:
        value = arguments.get(field_name)
        if isinstance(value, str) and value.startswith("${steps."):
            raise ValueError(
                f"effectful address {field_name} must be literal or input-addressable"
            )


def _strongest_effect(effects: list[str]) -> str:
    order = {
        "read": 0, "external_read": 0,
        "workspace_write": 1, "process": 2, "external": 3,
    }
    return max(effects or ["read"], key=lambda item: order.get(item, 3))


def _result_files(result: dict) -> set[str]:
    files = set()
    for key in ("path", "file"):
        if isinstance(result.get(key), str):
            files.add(result[key])
    if isinstance(result.get("files"), list):
        for item in result["files"]:
            if isinstance(item, str):
                files.add(item)
            elif isinstance(item, dict) and isinstance(item.get("path"), str):
                files.add(item["path"])
    return files
