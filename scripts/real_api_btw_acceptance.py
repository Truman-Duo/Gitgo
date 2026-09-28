"""Paid acceptance for the native, streaming, read-only BTW sidecar.

Credentials are read exclusively through Gitgo's configured provider. The
script emits metadata only; provider reasoning and response text are never
printed.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.native_host import NativeHost


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="gitgo")
    args = parser.parse_args()

    output = io.StringIO()
    host = NativeHost(stdout=output)
    try:
        result = host._runtime_btw(
            args.project,
            {
                "question": (
                    "Use the read-only project tools to inspect "
                    "backend/core/loop/tool_execution.py. Report the class defined "
                    "by @dataclass and the exact error token returned for an "
                    "unavailable tool. Do not infer without reading."
                ),
                "sidecar_id": "paid-btw-readonly-acceptance",
                "process_id": "",
                "history": [],
            },
            request_id="paid-btw-request",
        )
    finally:
        host.close()

    events = [
        json.loads(line).get("payload", {})
        for line in output.getvalue().splitlines()
        if line.strip()
    ]
    answer = str(result.get("answer") or "")
    error_events = [
        event for event in events
        if event.get("event") == "tool_result" and event.get("is_error")
    ]
    summary = {
        "verified": (
            result.get("status") == "completed"
            and result.get("isolated") is True
            and result.get("read_only_tools") is True
            and "ToolExecution" in answer
            and "TOOL_NOT_FOUND" in answer
            and any(event.get("event") == "text_delta" for event in events)
            and any(event.get("event") == "toolcall_start" for event in events)
            and any(event.get("event") == "tool_result" for event in events)
            and len(error_events) <= 7
        ),
        "status": result.get("status"),
        "streamed_text_events": sum(
            event.get("event") == "text_delta" for event in events
        ),
        "tool_start_events": sum(
            event.get("event") == "toolcall_start" for event in events
        ),
        "tool_result_events": sum(
            event.get("event") == "tool_result" for event in events
        ),
        "tool_error_events": len(error_events),
        "answer_contract_passed": (
            "ToolExecution" in answer and "TOOL_NOT_FOUND" in answer
        ),
        "active_sidecars_after_close": len(host._active_btw),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
