"""Paid E2E for a human-requested B Agent through ordinary native chat.

The prompt deliberately omits capability profile names, task-kind enums and
tool protocol details.  It exercises semantic task-contract declaration,
Host-guided delegation, B execution, A review, worktree promotion and the
supervisor completion gate.  Credentials stay in the user-scoped provider
configuration and the promoted fixture is removed after verification.
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


FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "gitgo_human_delegate_acceptance.html"
MARKER = "GITGO_HUMAN_DELEGATION_OK_20260902"
PROMPT = f"""请开启一个子代理，让它制作一份 HTML 格式的 Gitgo 自我介绍，并把文件写入
tests/fixtures/gitgo_human_delegate_acceptance.html。文件必须是可离线打开的完整 HTML，
并且正文中包含精确标记 {MARKER}。

这次任务明确要求子代理实际完成并生成工作区文件：不能由你直接写，也不能只在聊天中
粘贴代码。请等待子代理完成，检查它的结构化结果和实际交付物，批准后再把变更晋升到
工作区并向我交付；如果子代理没有真正启动或文件没有产生，必须如实失败，不能报告完成。
"""


def _ledger_path() -> Path:
    root = PROJECT_ROOT / ".gitgo" / "real_api_acceptance"
    root.mkdir(parents=True, exist_ok=True)
    for number in range(1, 100):
        suffix = "" if number == 1 else f"_run{number}"
        candidate = root / f"case10_human_delegation{suffix}.jsonl"
        if not candidate.exists():
            return candidate
    raise RuntimeError("acceptance ledger slots exhausted")


def _unique_events(events: list[dict]) -> list[dict]:
    """Merge Native stream + persisted Trace without counting one event twice."""
    seen: set[tuple] = set()
    result = []
    for item in events:
        if item.get("trace_id") and item.get("seq") is not None:
            key = ("trace", str(item["trace_id"]), int(item["seq"]))
        else:
            key = (
                "event", str(item.get("event", "")),
                str(item.get("process_id", "")),
                str(item.get("tool_call_id", "")),
                str(item.get("time", item.get("timestamp", ""))),
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
            )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


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
    trace_snapshot = None
    raised = None
    promoted_content = ""
    cleanup_error = None
    try:
        # Deliberately use the ordinary answer admission path.  The model must
        # declare the semantic contract and choose the Host workflow itself.
        result = host._runtime_chat(
            "realapi-human-delegation",
            {"project": "gitgo", "message": PROMPT, "max_steps": 24},
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
    combined = _unique_events(payloads + trace_events)
    event_counts = Counter(str(item.get("event", "")) for item in combined)
    tool_counts = Counter(
        str(item.get("tool_name") or item.get("name"))
        for item in combined
        if item.get("event") == "toolcall_done"
        and (item.get("tool_name") or item.get("name"))
    )
    errors = [
        item for item in combined
        if item.get("event") in {"error", "completion_rejected", "tool_error"}
        or item.get("is_error") is True
        or bool(item.get("trace_error"))
    ]
    outcome = (result or {}).get("outcome") or {}
    metadata = outcome.get("metadata") or {}
    delegated = list(metadata.get("delegated_outcomes") or [])
    verified = (
        raised is None
        and str(result.get("status")) == "completed"
        and MARKER in promoted_content
        and len(delegated) >= 1
        and event_counts["worktree_leased"] >= 1
        and event_counts["worktree_sealed"] >= 1
        and event_counts["worktree_promoted"] >= 1
        and len(errors) <= 7
        and cleanup_error is None
    )
    summary = {
        "verified": verified,
        "result": None if result is None else {
            "status": result.get("status"),
            "task_id": result.get("task_id"),
            "process_id": result.get("process_id"),
            "steps_used": result.get("steps_used"),
            "response_excerpt": str(result.get("response", ""))[:2000],
            "delegated_outcome_count": len(delegated),
            "cache_summary": metadata.get("cache_summary"),
        },
        "raised": raised,
        "event_counts": dict(event_counts),
        "tool_counts": dict(tool_counts),
        "marker_promoted": MARKER in promoted_content,
        "error_count": len(errors),
        "zero_score_stop": len(errors) > 7,
        "cleanup_error": cleanup_error,
        "ledger": str(ledger.relative_to(PROJECT_ROOT)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not verified:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
