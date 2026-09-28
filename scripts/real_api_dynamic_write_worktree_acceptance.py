"""Paid Native E2E for dynamic write -> isolated worktree -> promotion.

The API credential is read only from the user-scoped Gitgo provider config.
The temporary promoted fixture is removed after verification; the private
JSONL ledger never contains the credential.
"""

from __future__ import annotations

from collections import Counter
import io
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.application import OperationError
from backend.core.llm_config import LLMConfigManager
from backend.core.native_host import NativeHost


FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "gitgo_dynamic_write_acceptance.txt"
MARKER = "GITGO_DYNAMIC_WRITE_OK=20260829\n"
PROMPT = f"""Act as the A-level supervisor for one bounded paid acceptance.
Never edit or inspect files yourself. Use the production supervisor tools.

Delegate exactly one required development.workspace B with task_kind=action,
max_steps=16, target_files=["tests/fixtures/gitgo_dynamic_write_acceptance.txt"],
tool_scope_mode="profile", and allowed_tools exactly ["define_tool",
"write_file", "read_file", "run_test"]. Require test id
realapi:dynamic-write-worktree and these acceptance criteria:
- one Host-compiled task-scoped dynamic tool performs the write and read-back
- the promoted file contains the exact marker
- the registered deterministic test passes

B must do exactly this:
1. Call define_tool once with operation=define, name=create_and_read_acceptance,
description="Create a bounded acceptance file and read it back", and an object
schema with required path:string and content:string. Define two steps:
   - id=create_fixture, tool=write_file, arguments path=${{input.path}},
     content=${{input.content}}, create_only=true
   - id=read_fixture, tool=read_file, arguments path=${{input.path}}
2. Invoke create_and_read_acceptance exactly once with path
tests/fixtures/gitgo_dynamic_write_acceptance.txt and content exactly
{MARKER.rstrip()} followed by one newline.
3. Call run_test exactly once with test_id=realapi:dynamic-write-worktree,
target=tests/test_p0_development_runtime.py::test_dynamic_write_snapshot_uses_effect_not_tool_name,
seeds=[29], timeout=120.
4. Call complete_task once with the definition digest, both component receipt
ids, read-back marker and registered test evidence. Do not use shell or any
other tools.

Wait for B and inspect the structured outcome. Call review_child_outcome once.
Approve only if the composite child receipts are committed, the test passed,
and B's worktree is sealed. Then call promote_agent_changes exactly once for
that approved B. Confirm promotion succeeded before complete_supervision.
If any hard fact fails, report it without replacement workers or paid retries.
"""


def _ledger_path() -> Path:
    root = PROJECT_ROOT / ".gitgo" / "real_api_acceptance"
    root.mkdir(parents=True, exist_ok=True)
    for number in range(1, 100):
        suffix = "" if number == 1 else f"_run{number}"
        path = root / f"case9_dynamic_write_worktree{suffix}.jsonl"
        if not path.exists():
            return path
    raise RuntimeError("acceptance ledger slots exhausted")


def main() -> None:
    active = LLMConfigManager.get_active()
    if active is None or active.model_id != "deepseek-v4-flash":
        raise RuntimeError("acceptance requires active deepseek-v4-flash")
    if "v4-pro" in active.model_id.casefold():
        raise RuntimeError("v4-pro is forbidden")
    if FIXTURE.exists():
        raise RuntimeError(f"refusing to overwrite existing fixture: {FIXTURE}")

    sink = io.StringIO()
    host = NativeHost(stdout=sink)
    result = None
    raised = None
    trace_snapshot = None
    promoted_content = None
    cleanup_error = None
    try:
        result = host._runtime_chat(
            "realapi-dynamic-write-worktree",
            {
                "project": "gitgo",
                "message": PROMPT,
                "task_kind": "supervisor",
                "max_steps": 24,
            },
        )
        if result and result.get("task_id"):
            trace_snapshot = host._runtime_trace("gitgo", {
                "action": "read", "trace_id": result["task_id"],
                "after_seq": 0, "limit": 5000,
            })
        if FIXTURE.is_file():
            promoted_content = FIXTURE.read_text(encoding="utf-8")
    except Exception as exc:
        raised = {"type": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, OperationError):
            raised["operation_error"] = exc.to_dict()
    finally:
        host.close()
        if FIXTURE.exists():
            try:
                FIXTURE.unlink()
            except OSError as exc:
                cleanup_error = str(exc)

    ledger = _ledger_path()
    ledger.write_text(sink.getvalue(), encoding="utf-8")
    envelopes = [
        json.loads(line) for line in sink.getvalue().splitlines() if line.strip()
    ]
    payloads = [item.get("payload", {}) for item in envelopes]
    trace_events = list((trace_snapshot or {}).get("events", []))
    combined = payloads + trace_events
    event_counts = Counter(str(item.get("event", "")) for item in combined)
    tool_counts = Counter(
        str(item.get("tool_name") or item.get("name"))
        for item in combined if item.get("tool_name") or item.get("name")
    )
    errors = [
        item for item in combined
        if item.get("event") in {"error", "completion_rejected", "tool_error"}
        or item.get("is_error") is True
        or bool(item.get("trace_error"))
    ]
    outcome = (result or {}).get("outcome") or {}
    metadata = outcome.get("metadata") or {}
    verified = (
        raised is None
        and promoted_content == MARKER
        and event_counts["worktree_leased"] > 0
        and event_counts["worktree_sealed"] > 0
        and event_counts["worktree_promoted"] > 0
        and cleanup_error is None
    )
    summary = {
        "verified": verified,
        "result": None if result is None else {
            "status": result.get("status"),
            "task_id": result.get("task_id"),
            "process_id": result.get("process_id"),
            "steps_used": result.get("steps_used"),
            "response_excerpt": str(result.get("response", ""))[:2500],
            "cache_summary": metadata.get("cache_summary"),
            "delegated_outcomes": metadata.get("delegated_outcomes"),
        },
        "raised": raised,
        "event_counts": dict(event_counts),
        "tool_counts": dict(tool_counts),
        "promoted_content_matches": promoted_content == MARKER,
        "cleanup_error": cleanup_error,
        "error_count": len(errors),
        "zero_score_stop": len(errors) > 7,
        "ledger": str(ledger.relative_to(PROJECT_ROOT)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not verified:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
