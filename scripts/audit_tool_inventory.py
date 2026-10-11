"""Export public source contracts and Host profiles without loading credentials."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.loop.capabilities import CapabilityProfiles


def inventory():
    definitions = []
    for path in sorted((ROOT / "backend/core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "AgentTool":
                continue
            fields = {kw.arg: kw.value for kw in call.keywords if kw.arg}
            name = fields.get("name")
            if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
                continue
            row = {"name": name.value, "source": path.relative_to(ROOT).as_posix(), "line": call.lineno}
            for key in ("effect", "read_only", "approval", "approval_per_invocation", "runner_name", "resources", "idempotent"):
                if key in fields:
                    try:
                        row[key] = ast.literal_eval(fields[key])
                    except (ValueError, TypeError):
                        row[key] = ast.unparse(fields[key])
            definitions.append(row)
    profiles = CapabilityProfiles.describe()
    for profile in profiles:
        profile["tools"] = list(CapabilityProfiles.get(profile["profile_id"]).tool_names)
    names = sorted({n for p in profiles for n in p["tools"]})
    return {"schema_version": 1, "scope": "source contracts; runtime actor/profile/task leases filter exposure",
            "profiles": profiles, "profile_tool_names": names, "definitions": definitions,
            "limitations": ["Variable-name custom tools are catalog entries, not statically enumerable.",
                            "This inventory is not permission to execute a listed tool.",
                            "Some profile tools are closure wrappers rather than AgentTool literals; audit report identifies those families."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = inventory()
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
        print(json.dumps({"profiles": len(result["profiles"]), "profile_tools": len(result["profile_tool_names"]), "definitions": len(result["definitions"])}))
    else:
        print(encoded)
