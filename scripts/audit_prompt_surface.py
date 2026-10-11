"""Read existing SQLite/CAS provider snapshots and report prompt surface sizes.

Does not print conversation text, create storage, or mutate runtime databases.
Counts Unicode characters/UTF-8 bytes, not provider tokens.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.usability.projection import measure_prompt


def audit(project_root: Path) -> dict:
    def blob(reference):
        digest = reference.split(":")[-1]
        return json.loads((project_root / "cas" / digest[:2] / digest[2:]).read_text(encoding="utf-8"))

    def snapshot(reference, visited=None):
        visited = set() if visited is None else visited
        if reference in visited or len(visited) >= 128:
            raise ValueError("Provider snapshot chain is cyclic or too deep")
        visited.add(reference)
        detail = blob(reference)
        if detail.get("snapshot_mode") == "prefix_delta":
            base = snapshot(detail["base_detail_ref"], visited)
            detail = {**detail, "messages": base["messages"][:detail["common_prefix_messages"]] + detail["appended_messages"],
                      "tools": base["tools"] if detail.get("tools_reused") else detail.get("tools", [])}
        return detail

    rows = []
    with closing(sqlite3.connect((project_root / "observability.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        for (raw,) in connection.execute("SELECT record_json FROM trace_events WHERE event_type='provider_request_started' ORDER BY occurred_at"):
            record = json.loads(raw)
            detail = snapshot(record["detail_ref"])
            messages = detail.get("messages", [])
            system = [m for m in messages if m.get("role") == "system"]
            rows.append({"time": record["time"], "task_kind": record.get("task_kind"),
                         **measure_prompt(messages, detail.get("tools", [])),
                         "system_sections": [[line[3:] for line in m.get("content", "").splitlines() if line.startswith("## ")] for m in system],
                         "snapshot_bytes": record.get("detail_bytes"),
                         "snapshot_mode": detail.get("snapshot_mode")})
    return {"units": "unicode_characters_and_utf8_bytes_not_tokens", "provider_requests": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = audit(args.project_root.resolve())
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
