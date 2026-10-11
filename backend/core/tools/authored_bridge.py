"""Pure code may request Host calls; it cannot invoke them or mint receipts."""
from __future__ import annotations
import hashlib
import json
from .dynamic_tools import _SAFE_CALLS


class HostCallNeeded(BaseException):
    def __init__(self, name, args, index):
        self.name, self.arguments, self.index = name, args, index


def execute_authored_host_python(args):
    spec = dict(args.get("_dynamic_spec") or {})
    source = spec.get("_source")
    if not isinstance(source, str) or hashlib.sha256(source.encode()).hexdigest() != spec.get("source_sha256"):
        return {"error": "AUTHORED_TOOL_SOURCE_CHANGED"}
    results = args.get("_host_results") or []
    position = 0
    def call_tool(name, arguments):
        nonlocal position
        index = position; position += 1
        if index >= 20 or name not in spec["host_tools"] or not isinstance(arguments, dict):
            raise ValueError("AUTHORED_HOST_CALL_INVALID")
        from backend.core.loop.permission_broker import arguments_digest
        digest = arguments_digest(arguments)
        if index >= len(results):
            raise HostCallNeeded(name, arguments, index)
        record = results[index]
        if record["tool_name"] != name or record["arguments_digest"] != digest:
            raise ValueError("AUTHORED_HOST_SEQUENCE_CHANGED: inspect retained effects before retrying")
        return record["result"]
    import builtins
    namespace = {"__builtins__": {name: getattr(builtins, name) for name in _SAFE_CALLS}, "call_tool": call_tool}
    try:
        exec(compile(source, "authored_host_tool", "exec"), namespace, namespace)
        value = namespace["run"]({k: v for k, v in args.items() if not str(k).startswith("_")})
        if position != len(results):
            raise ValueError("AUTHORED_HOST_SEQUENCE_CHANGED")
        if len(json.dumps(value, ensure_ascii=False)) > 200_000:
            raise ValueError("AUTHORED_TOOL_OUTPUT_TOO_LARGE")
        return {"bridge_version": 1, "state": "completed", "output": value}
    except HostCallNeeded as call:
        return {"bridge_version": 1, "state": "needs_host_call", "call_index": call.index,
            "tool_name": call.name, "arguments": call.arguments}
