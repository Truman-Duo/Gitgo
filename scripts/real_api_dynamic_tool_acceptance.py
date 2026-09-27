"""Paid, bounded acceptance for Host-managed dynamic tools.

Credentials are loaded from the user-scoped Gitgo provider configuration and
are never written to this script or its ledger.
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


PROMPT = """Act as the A-level supervisor for a bounded dynamic-tool acceptance.
Do not inspect files or request self-execution as A. Delegate exactly one required
development.workspace B worker with task_kind=action and max_steps=12.

Give B this exact contract:
Set allowed_tools exactly to define_tool, read_file, search_text, and run_test.
Set required_tool_calls to exactly these three top-level requirements:
- define_tool: min_calls=1, max_calls=1
- inspect_runtime_contract: min_calls=1, max_calls=1
- run_test: min_calls=1, max_calls=1
1. Define one task-scoped tool by calling define_tool once with operation=define,
   name=inspect_runtime_contract, description="Read a file and locate one exact
   runtime contract marker", and this object schema: path:string and pattern:string,
   both required. Its steps must be:
   - id=read_current, tool=read_file, arguments path=${input.path}
   - id=find_contract, tool=search_text, arguments path=${input.path},
     pattern=${input.pattern}, literal=true
2. Invoke inspect_runtime_contract exactly once with
   path=backend/core/loop/tool_pipeline.py and pattern=_execute_composite_plan.
3. Call run_test exactly once with test_id=realapi:dynamic-tool-phase,
   target=tests/test_p0_development_runtime.py, seeds=[17], timeout=120.
   Do not edit any file and do not run shell commands.
4. Submit complete_task with a concise result naming the dynamic definition digest,
   the two component receipt ids, the exact search evidence, and the test evidence.

The child contract must require test id realapi:dynamic-tool-phase and acceptance:
- dynamic definition is Host-compiled and task-scoped
- both component steps have committed receipts
- the registered test passes
- no workspace source file is modified

Wait for B. Inspect its structured outcome and call review_child_outcome exactly once.
Approve only if the Host outcome contains the required test and committed composite
receipt lineage. Then call complete_supervision once with a concise synthesis. If B
fails, report the real failure; do not create replacement workers and do not retry the
paid task more than seven times.
"""


def main() -> None:
    active = LLMConfigManager.get_active()
    if active is None or active.model_id != "deepseek-v4-flash":
        raise RuntimeError("acceptance requires active deepseek-v4-flash")
    if "v4-pro" in active.model_id.casefold():
        raise RuntimeError("v4-pro is forbidden")

    sink = io.StringIO()
    host = NativeHost(stdout=sink)
    result = None
    raised = None
    trace_snapshot = None
    try:
        result = host._runtime_chat(
            "realapi-dynamic-tool",
            {
                "project": "gitgo",
                "message": PROMPT,
                "task_kind": "supervisor",
                "max_steps": 20,
                # An acceptance case must never inherit a human conversation,
                # pending decision, prior B owner or stale tool contract.
                "session_mode": "fresh",
            },
        )
        if result and result.get("task_id"):
            trace_snapshot = host._runtime_trace("gitgo", {
                "action": "read", "trace_id": result["task_id"],
                "after_seq": 0, "limit": 5000,
            })
    except Exception as exc:
        raised = {"type": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, OperationError):
            raised["operation_error"] = exc.to_dict()
    finally:
        host.close()

    ledger_root = Path(".gitgo/real_api_acceptance")
    ledger = ledger_root / "case8_dynamic_tool.jsonl"
    if ledger.exists():
        for run_number in range(2, 100):
            candidate = ledger_root / f"case8_dynamic_tool_run{run_number}.jsonl"
            if not candidate.exists():
                ledger = candidate
                break
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(sink.getvalue(), encoding="utf-8")
    envelopes = [
        json.loads(line) for line in sink.getvalue().splitlines() if line.strip()
    ]
    payloads = [item.get("payload", {}) for item in envelopes]
    trace_events = list((trace_snapshot or {}).get("events", []))
    event_counts = Counter(str(item.get("event", "")) for item in payloads)
    tool_counts = Counter(
        str(item.get("tool_name") or item.get("name"))
        for item in payloads
        if item.get("tool_name") or item.get("name")
    )
    composite_events = [
        item for item in payloads if item.get("event") == "composite_step_result"
    ]
    if not composite_events:
        composite_events = [
            item for item in trace_events
            if item.get("event") == "composite_step_result"
        ]
    errors = [
        item for item in payloads
        if item.get("event") in {"error", "completion_rejected", "tool_error"}
        or item.get("is_error") is True
        or bool(item.get("trace_error"))
    ]
    outcome = (result or {}).get("outcome") or {}
    metadata = outcome.get("metadata") or {}
    delegated = list(metadata.get("delegated_outcomes") or [])
    child = delegated[0] if len(delegated) == 1 else {}
    child_receipts = list(child.get("tool_receipts") or [])
    receipt_counts = Counter(
        str(item.get("tool_name") or "") for item in child_receipts
        if item.get("succeeded") is True and item.get("committed") is True
    )
    acceptance_failures = []
    if (result or {}).get("status") != "completed":
        acceptance_failures.append("root_not_completed")
    if len(delegated) != 1 or child.get("status") != "completed":
        acceptance_failures.append("single_child_not_completed")
    expected_calls = {
        "define_tool": 1, "inspect_runtime_contract": 1, "run_test": 1,
    }
    for name, count in expected_calls.items():
        if receipt_counts.get(name, 0) != count:
            acceptance_failures.append(
                f"{name}_successful_calls={receipt_counts.get(name, 0)}"
            )
    actual_steps = {
        (str(item.get("step_id") or ""), str(item.get("tool_name") or ""))
        for item in composite_events
        if item.get("is_error") is not True and (item.get("receipt") or {}).get("receipt_id")
    }
    if actual_steps != {("read_current", "read_file"), ("find_contract", "search_text")}:
        acceptance_failures.append("composite_step_lineage_mismatch")
    test_receipts = [
        item for item in child_receipts
        if item.get("tool_name") == "run_test"
        and item.get("test_id") == "realapi:dynamic-tool-phase"
        and item.get("test_passed") is True
    ]
    if len(test_receipts) != 1:
        acceptance_failures.append("current_test_receipt_missing")
    if errors:
        acceptance_failures.append(f"trace_errors={len(errors)}")
    summary = {
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
        "composite_steps": [
            {
                "tool_name": item.get("tool_name"),
                "step_id": item.get("step_id"),
                "is_error": item.get("is_error"),
                "receipt_id": (item.get("receipt") or {}).get("receipt_id"),
            }
            for item in composite_events
        ],
        "receipt_counts": dict(receipt_counts),
        "acceptance_passed": not acceptance_failures,
        "acceptance_failures": acceptance_failures,
        "trace_event_total": len(trace_events),
        "error_count": len(errors),
        "zero_score_stop": len(errors) > 7,
        "ledger": str(ledger),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if acceptance_failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
