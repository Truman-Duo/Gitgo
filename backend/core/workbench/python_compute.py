"""One-shot Python in the existing native runner's disposable workspace."""
from contextlib import chdir, redirect_stderr, redirect_stdout
from decimal import Decimal
import io
import json

from .data import encode_json, parse_json


def _numeric_modes(value):
    modes, todo = set(), [value]
    while todo:
        item = todo.pop()
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("Output object keys must be strings")
            todo.extend(item.values())
        elif isinstance(item, list):
            todo.extend(item)
        elif type(item) in (int, float, Decimal):
            modes.add({int: "integer", float: "binary_float", Decimal: "decimal"}[type(item)])
        elif item is not None and type(item) not in (bool, str):
            raise ValueError("Outputs must be JSON values or Decimal numbers")
    return sorted(modes)


def execute_python(args):
    try:
        inputs = {name: json.loads(text, parse_float=Decimal)
                  for name, text in args["inputs_json"].items()}
        namespace = {"__name__": "gitgo_workbench_calculation"}
        with chdir(args["_workspace"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            exec(compile(args["code"], "<workbench>", "exec"), namespace)
            result = namespace["run"](inputs)
        if not isinstance(result, dict) or result.keys() != set(args["output_names"]):
            raise ValueError("run(inputs) must return exactly the declared output names")
        outputs = {}
        for name, value in result.items():
            encoded = encode_json(value)
            parse_json(encoded)
            outputs[name] = {"json": encoded, "numeric_modes": _numeric_modes(value)}
        return {"outputs": outputs}
    except (Exception, SystemExit) as exc:
        from backend.core.storage.redaction import redact_for_persistence
        return {"error": "WORKBENCH_INVALID_DATA",
                "detail": redact_for_persistence(f"{type(exc).__name__}: {exc}")[:500]}
