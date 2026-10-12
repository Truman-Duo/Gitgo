"""Read private test traces/state without starting or mutating a runtime."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.evaluate_alignment_behavior import read_evidence

if __name__ == "__main__":
    profile = Path(sys.argv[1]).resolve()
    evidence = read_evidence(profile / "state")
    for process in evidence["processes"]:
        outcome = process.get("outcome") or {}
        print(json.dumps({"process_id": process["process_id"], "task_id": process["task_id"],
            "status": process["status"], "pending": process["pending_decision"],
            "response": outcome.get("response"), "error": outcome.get("error"),
            "workflow": process["workflow"]}, ensure_ascii=True))
    for event in evidence["trace"][-35:]:
        print(json.dumps({key: event.get(key) for key in
            ("event", "trace_id", "seq", "stage", "status", "reason", "error", "detail")
            if event.get(key) is not None}, ensure_ascii=True))
