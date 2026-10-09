"""Bounded paid search acceptance through Native Host's read-only BTW path.

Uses an existing project and configured provider, with separate runtime state.
Only allowlisted receipt/coverage metadata is saved; credentials and provider
reasoning are never printed or persisted by this script.
"""
from __future__ import annotations

import argparse
from collections import Counter
import io
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.config import ConfigManager
from backend.core.llm_config import LLMConfigManager
from backend.core.native_host import NativeHost


PROMPT = """Perform this bounded, read-only search acceptance in the current project.
Do not write files, execute commands, delegate, invent tools or change settings.
Make exactly these three inspection calls, then answer briefly:
1. search_text on backend/core/tools/catalog.py for the literal string
   def build_workspace_tools, literal=true, context_lines=1, max_results=2.
2. search_text on the same file for the same literal string, literal=true,
   output_mode=count, max_results=2.
3. list_files on backend/core/tools with pattern=*.py and max_results=2.
Report the actual definition line, per-file count, listing pagination and search
engine. If the tool reports degraded/fallback or incomplete coverage, explicitly
report that limitation; do not silently present it as the normal engine.
Do not read additional files. Stop after these calls. TASK_COMPLETE.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="gitgo")
    parser.add_argument("--engine", choices=["ripgrep", "fallback"], default="ripgrep")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = args.report.resolve()
    report.relative_to(ROOT)
    config = ConfigManager.load()
    project = next(p for p in config.projects if p.name == args.project)
    workspace = Path(project.workspace_path).resolve()
    if workspace != ROOT:
        raise RuntimeError("This acceptance contract targets the existing Gitgo source project")
    active = LLMConfigManager.get_active()
    if active is None or active.model_id != "deepseek-v4-flash":
        raise RuntimeError("This paid acceptance is bounded to the configured deepseek-v4-flash")
    report.parent.mkdir(parents=True, exist_ok=True)
    # Host/Daemon/isolated tool processes inherit this namespace. Existing user
    # conversations and checkpoints remain outside the acceptance state.
    os.environ["GITGO_STATE_HOME"] = str(report.parent / f"state-{uuid.uuid4().hex[:12]}")
    if args.engine == "fallback":
        os.environ["GITGO_RIPGREP_PATH"] = str(report.parent / "intentionally-unavailable-rg.exe")
    else:
        os.environ.pop("GITGO_RIPGREP_PATH", None)
    sink = io.StringIO()
    host = NativeHost(stdout=sink)
    result = {}
    raised = None
    try:
        result = host._runtime_btw(args.project, {
            "question": PROMPT, "history": [], "process_id": "",
            "sidecar_id": "search-acceptance-" + uuid.uuid4().hex[:12],
        }, request_id="search-acceptance")
    except Exception as exc:
        # A code/type suffices for recovery without risking credentials in an
        # HTTP exception's arbitrary message.
        raised = type(exc).__name__
    finally:
        host.close()
    events = [json.loads(line).get("payload", {})
              for line in sink.getvalue().splitlines() if line.strip()]
    results = [e for e in events if e.get("event") == "tool_result"]
    inspections = [e for e in results if e.get("tool_name") in {"search_text", "list_files"}]
    receipts = [e.get("receipt") or {} for e in inspections]
    engine = "python" if args.engine == "fallback" else "ripgrep"
    notice_codes = sorted({e.get("code") for e in events
                           if e.get("phase") == "tool_notice" and e.get("code")})
    failures = []
    if raised or result.get("status") != "completed":
        failures.append("native_readonly_task_did_not_complete")
    if not result.get("isolated") or not result.get("read_only_tools"):
        failures.append("readonly_isolation_not_confirmed")
    if ([e.get("tool_name") for e in inspections].count("search_text") != 2
            or [e.get("tool_name") for e in inspections].count("list_files") != 1):
        failures.append("expected_inspection_calls_missing_or_repeated")
    if any(e.get("is_error") for e in results):
        failures.append("tool_error")
    if any(r.get("search_engine") != engine or r.get("search_partial") is not False
           or (r.get("tool_name") == "search_text" and r.get("search_complete") is not True)
           or not r.get("committed") or not r.get("succeeded") for r in receipts):
        failures.append("actual_engine_coverage_or_receipt_not_confirmed")
    pages = []
    for event in inspections:
        try:
            # Public preview retains the standard human-readable tool prefix.
            raw = str(event.get("result_preview") or "")
            pages.append(json.loads(raw[raw.index("{"):]))
        except (ValueError, json.JSONDecodeError):
            pages.append({})
    evidence = next((page for page in pages if page.get("matches")), {})
    expected_line = next(i for i, line in enumerate(
        (ROOT / "backend/core/tools/catalog.py").read_text(encoding="utf-8").splitlines(), 1)
        if line.startswith("def build_workspace_tools"))
    if not any(row.get("line") == expected_line and "def build_workspace_tools" in row.get("text", "")
               for row in evidence.get("matches", [])):
        failures.append("definition_evidence_missing")
    listing = next((page for page in pages if "files" in page), {})
    if listing.get("next_offset") != 2 or listing.get("truncated") is not True:
        failures.append("actual_listing_pagination_missing")
    if args.engine == "fallback" and "SEARCH_FALLBACK" not in notice_codes:
        failures.append("fallback_not_visible_on_public_stream")
    answer = str(result.get("answer") or "").casefold()
    if args.engine == "fallback" and not any(s in answer for s in ("fallback", "python", "降级", "回退")):
        failures.append("model_did_not_report_fallback")
    summary = {
        "verified": not failures, "failures": failures, "raised_type": raised,
        "project": args.project, "model": active.model_id, "engine": engine,
        "status": result.get("status"), "notice_codes": notice_codes,
        "event_counts": dict(Counter(str(e.get("event") or "") for e in events)),
        "failure_diagnostics": [
            {k: str(e.get(k) or "").replace(active.api_key, "[redacted]")[:600]
             for k in ("event", "code", "error", "message")}
            for e in events if e.get("event") in {"error", "stream_recovery", "stream_error"}
            or (e.get("event") == "btw_complete" and e.get("error"))
        ],
        "result_error": str(result.get("error") or "").replace(active.api_key, "[redacted]")[:600],
        "failed_answer": (str(result.get("answer") or "").replace(active.api_key, "[redacted]")[:600]
                          if result.get("status") != "completed" else ""),
        "tool_error_count": sum(bool(e.get("is_error")) for e in results),
        "streamed_text_events": sum(e.get("event") == "text_delta" for e in events),
        "receipts": [{k: r.get(k) for k in (
            "receipt_id", "tool_name", "committed", "succeeded", "search_engine",
            "search_complete", "search_partial", "search_warning_codes",
        )} for r in receipts],
        "definition_evidence": evidence.get("matches", []),
        "listing_pagination": {k: listing.get(k) for k in ("count", "truncated", "next_offset")},
    }
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
