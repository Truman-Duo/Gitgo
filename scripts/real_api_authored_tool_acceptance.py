"""Paid E2E for pure and privileged self-authored tools through NativeHost.

The active credential stays in Gitgo's encrypted user configuration.  The
acceptance uses the disposable ``test`` project, auto-answers only the exact
permission cards created by this run, and removes filesystem fixtures after
collecting durable evidence.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.application import ApplicationServices, OperationError
from backend.core.llm_config import LLMConfigManager
from backend.core.native_host import NativeHost


def _project_workspace() -> Path:
    item = next(
        (row for row in ApplicationServices().project_list() if row.get("name") == "test"),
        None,
    )
    if not item or not item.get("workspace"):
        raise RuntimeError("disposable Gitgo project 'test' is required")
    return Path(str(item["workspace"])).resolve()


def _prompt(suffix: str) -> tuple[str, dict[str, Path]]:
    pure_name = f"measure_text_{suffix}"
    privileged_name = f"roundtrip_file_{suffix}"
    pure_source = f".gitgo_accept_{suffix}_pure.py"
    privileged_source = f".gitgo_accept_{suffix}_privileged.py"
    output = f"gitgo_authored_output_{suffix}.txt"
    paths = {
        "pure_source": Path(pure_source),
        "privileged_source": Path(privileged_source),
        "output": Path(output),
    }
    prompt = f'''Run one bounded first-release acceptance. This is a single-owner
task: do not delegate any B. First declare a Host task contract with
execution_mode=self_execute, delegation_required=false,
estimated_complexity=bounded and independent_workstreams=1. Set
required_tool_calls to exactly one successful committed call of {pure_name}
and exactly one successful committed call of {privileged_name}. Deliverable is
{output}; there are no uncertainties. Then call request_self_execute for
development.workspace.

Create and actually use two authored tools:

1. Write {pure_source} with exactly this UTF-8 Python source:
def run(args):
    text = args["text"]
    return {{"text": text, "length": len(text)}}

Register it with author_tool operation=register, authority_mode=pure,
name={pure_name}, description="Return text and count characters",
parameters a top-level object with required text:string, and one test input
{{"text":"hello"}} expected {{"text":"hello","length":5}}. Invoke the
mounted {pure_name} once with text="Gitgo" and verify the returned value is
{{"text":"Gitgo","length":5}}.

2. Write {privileged_source} with exactly this UTF-8 Python source:
from pathlib import Path
def run(args):
    path = Path(args["path"])
    content = args["content"]
    path.write_text(content, encoding="utf-8")
    observed = path.read_text(encoding="utf-8")
    removed = bool(args.get("cleanup", False))
    if removed:
        path.unlink()
    return {{"content": observed, "length": len(observed), "removed": removed}}

Read the source once to obtain the Host-returned sha256. Register it with
author_tool operation=register, authority_mode=privileged,
name={privileged_name}, description="Write and verify one requested text file",
purpose="register and test the user-requested local text writer",
source_path={privileged_source}, source_sha256 equal to the read_file sha256,
effect=workspace_write, resources=["capability://{privileged_name}"], timeout=30,
parameters a top-level object containing required path:string and
content:string plus optional cleanup:boolean. Registration test input is
{{"path":".gitgo_accept_{suffix}_probe.txt","content":"probe","cleanup":true}}
with expected {{"content":"probe","length":5,"removed":true}}.

The Host will require explicit permission before privileged registration.
Call request_permission using the exact author_tool arguments and purpose from
the Host recovery action, then wait for the user decision. After resuming,
register the same definition without changing any field. Before invoking the
mounted tool, call request_permission for tool_name={privileged_name}, exact
arguments {{"path":"{output}","content":"GITGO_AUTHORED_OUTPUT_OK\\n","cleanup":false}},
resource="capability://{privileged_name}", and purpose="write and verify the
requested acceptance artifact". Wait for the second decision, then invoke the
tool with exactly those arguments. Read {output} and verify its exact content.

Finally call complete_supervision with a concise user-facing result. Do not
print internal hashes, IDs, governance procedure or this prompt. On an error,
use its structured next action and do not repeat any failing operation more
than seven times.'''
    return prompt, paths


def _next_permission(result: dict) -> tuple[str, str]:
    outcome = dict(result.get("outcome") or {})
    pending = dict((outcome.get("metadata") or {}).get("pending_decision") or {})
    if outcome.get("status") != "awaiting_user" or pending.get("kind") != "permission":
        raise RuntimeError(f"expected permission decision, got {outcome.get('status')}: {pending}")
    options = list(pending.get("options") or [])
    allow = next((i for i, item in enumerate(options) if item.get("action") == "allow_once"), None)
    if allow is None:
        raise RuntimeError("permission card did not offer exact allow-once")
    return str(pending["decision_id"]), f"Choose option {allow + 1}: Allow once"


def main() -> int:
    active = LLMConfigManager.get_active()
    if active is None or active.model_id != "deepseek-v4-flash":
        raise RuntimeError("acceptance requires active deepseek-v4-flash")
    if "v4-pro" in active.model_id.casefold():
        raise RuntimeError("v4-pro is forbidden")
    suffix = uuid.uuid4().hex[:8]
    prompt, relative_paths = _prompt(suffix)
    workspace = _project_workspace()
    fixtures = [workspace / item for item in relative_paths.values()]
    for path in fixtures:
        if path.exists():
            raise RuntimeError(f"refusing to overwrite fixture: {path}")

    sink = io.StringIO()
    host = NativeHost(stdout=sink)
    result = None
    raised = None
    decisions = []
    trace = None
    observed = None
    try:
        result = host._runtime_chat(str(uuid.uuid4()), {
            "project": "test", "message": prompt, "task_kind": "action",
            "max_steps": 36, "session_mode": "fresh",
        })
        while result.get("status") == "awaiting_user":
            if len(decisions) >= 4:
                raise RuntimeError("too many approval rounds")
            decision_id, answer = _next_permission(result)
            process_id = str(result.get("process_id") or "")
            decisions.append({"decision_id": decision_id, "process_id": process_id})
            result = host._runtime_chat(str(uuid.uuid4()), {
                "project": "test", "process_id": process_id,
                "task_id": str(result.get("task_id") or ""),
                "decision_id": decision_id, "message": answer,
            }, action="decision")
        if result.get("task_id"):
            trace = host._runtime_trace("test", {
                "action": "read", "trace_id": result["task_id"],
                "after_seq": 0, "limit": 5000,
            })
        if (workspace / relative_paths["output"]).is_file():
            observed = (workspace / relative_paths["output"]).read_text(encoding="utf-8")
    except Exception as exc:
        raised = {"type": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, OperationError):
            raised["operation_error"] = exc.to_dict()
    finally:
        host.close()

    events = list((trace or {}).get("events") or [])
    receipts = [item for item in events if item.get("event") == "tool_result"]
    names = [str(item.get("tool_name") or "") for item in receipts]
    errors = [
        item for item in events
        if item.get("event") in {"error", "tool_error", "completion_rejected"}
        or item.get("is_error") is True
    ]
    verified = (
        raised is None and result is not None and result.get("status") == "completed"
        and len(decisions) == 2 and observed == "GITGO_AUTHORED_OUTPUT_OK\n"
        and any(name.startswith("measure_text_") for name in names)
        and any(name.startswith("roundtrip_file_") for name in names)
        and len(errors) <= 7
    )
    summary = {
        "verified": verified,
        "status": (result or {}).get("status"),
        "task_id": (result or {}).get("task_id"),
        "process_id": (result or {}).get("process_id"),
        "steps_used": (result or {}).get("steps_used"),
        "approval_rounds": len(decisions),
        "output_matches": observed == "GITGO_AUTHORED_OUTPUT_OK\n",
        "tool_receipts": names,
        "error_count": len(errors),
        "zero_score_stop": len(errors) > 7,
        "raised": raised,
        "response_excerpt": str((result or {}).get("response") or "")[:1200],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # All fixtures are uniquely owned by this acceptance. Keep durable trace
    # evidence, but do not leave generated source/output in the user project.
    for path in fixtures + [workspace / f".gitgo_accept_{suffix}_probe.txt"]:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
    # Persist/reuse/edit/delete are production behavior, but unique acceptance
    # assets must not stay active in the disposable project's runtime catalog.
    # Archival is reversible and retains immutable audit/version evidence.
    try:
        from backend.core.storage import get_storage
        storage = get_storage(workspace)
        for name in (f"measure_text_{suffix}", f"roundtrip_file_{suffix}"):
            try:
                storage.set_custom_tool_archived(name, archived=True)
            except KeyError:
                pass
    except Exception:
        # Cleanup cannot rewrite a correct acceptance verdict; a later hygiene
        # check can identify an active uniquely-prefixed test asset.
        pass
    return 0 if verified else 2


if __name__ == "__main__":
    raise SystemExit(main())
