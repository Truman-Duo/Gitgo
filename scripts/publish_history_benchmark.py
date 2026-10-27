"""Create a disposable, clearly labeled acceptance dataset and time real reads.

Usage: python -m scripts.publish_history_benchmark --destination <new directory>
Writes only the new destination; never opens the user's live configuration.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time


def git(repo, *args, input_bytes=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=input_bytes, check=True,
                          capture_output=True).stdout.decode("utf8").strip()


def measure(function, samples):
    before = time.perf_counter()
    first_result = function()
    first = (time.perf_counter() - before) * 1000
    timings = []
    for _ in range(samples):
        before = time.perf_counter()
        function()
        timings.append((time.perf_counter() - before) * 1000)
    ordered = sorted(timings)
    return {"samples": samples, "first_read_ms": first,
            "first_read_cache": first_result.get("cache", "not_applicable"),
            "median_ms": statistics.median(timings),
            "p95_ms": ordered[max(0, __import__("math").ceil(samples * .95) - 1)], "max_ms": max(timings),
            "timings_ms": timings}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True)
    parser.add_argument("--samples", type=int, default=15)
    args = parser.parse_args()
    if not 5 <= args.samples <= 50:
        parser.error("samples must be 5..50")
    root = Path(args.destination).resolve()
    root.mkdir(parents=False, exist_ok=False)
    os.environ["GITGO_STATE_HOME"] = str(root / "state")
    os.environ["GITGO_CONFIG_PATH"] = str(root / "config.json")
    os.environ["GITGO_LLM_CONFIG_PATH"] = str(root / "llm_config.json")
    from backend.core.application.publish import list_items
    from backend.core.config import Config, ConfigManager, ProjectConfig
    from backend.core.publish.history import history_page, diff_view
    from backend.core.publish.service import PublishService
    from backend.core.publish.state import Repository, encoded
    from backend.core.storage import get_storage, close_owned_storage
    repo = root / "synthetic-10000-commits"
    repo.mkdir()
    git(repo, "init", "--initial-branch=benchmark")
    git(repo, "config", "user.name", "Gitgo Acceptance Fixture")
    git(repo, "config", "user.email", "fixture@local.gitgo.invalid")
    data = bytearray()
    for index in range(10000):
        message = f"Synthetic acceptance commit {index + 1:05d}\n".encode()
        content = f"Synthetic fixture revision {index + 1:05d}\n".encode()
        data.extend(b"commit refs/heads/benchmark\n")
        data.extend(f"committer Gitgo Acceptance Fixture <fixture@local.gitgo.invalid> {1700000000 + index} +0000\n".encode())
        data.extend(f"data {len(message)}\n".encode() + message)
        data.extend(b"M 100644 inline fixture.txt\n")
        data.extend(f"data {len(content)}\n".encode() + content + b"\n")
    git(repo, "fast-import", "--quiet", input_bytes=bytes(data))
    git(repo, "reset", "--hard", "HEAD")
    count = int(git(repo, "rev-list", "--count", "HEAD"))
    assert count == 10000
    project = ProjectConfig(name="Synthetic Publish 1000 items 10000 commits")
    project.workspace_path = str(repo)
    ConfigManager.save(Config(projects=[project]))
    service = PublishService(project, actor="local_operator")
    prepared = service.command("prepare", {"request_id": "benchmark_prepare", "source_role": "workspace",
        "source_ref": "HEAD", "target_branch": "publish/benchmark", "owner": "fixture",
        "title": "Synthetic acceptance item"})
    if "item" not in prepared:
        raise RuntimeError(prepared)
    store = Repository(str(repo), get_storage(repo))
    ledger, ref = store.load()
    template = next(iter(ledger["items"].values()))
    ledger["items"] = {}
    for index in range(1000):
        item = deepcopy(template)
        item["id"], item["title"] = f"benchmark_{index:04d}", f"Synthetic item {index:04d}"
        ledger["items"][item["id"]] = item
    store.save(ledger, ref)
    item_id = "benchmark_0000"
    from backend.core.publish.history import history_scope
    scope = history_scope(project, item_id)
    snapshot = scope["snapshot_id"]
    parent_oid = git(repo, "rev-parse", "HEAD^")
    middle = base64.urlsafe_b64encode(encoded({"snapshot": snapshot, "offset": 5000})).decode()
    last = base64.urlsafe_b64encode(encoded({"snapshot": snapshot, "offset": 9950})).decode()
    checks = {
        "items_50_of_1000": lambda: list_items(project, limit=50),
        "history_first_50_of_10000": lambda: history_page(project, item_id=item_id),
        "history_middle_50_of_10000": lambda: history_page(project, item_id=item_id, cursor=middle),
        "history_final_50_of_10000": lambda: history_page(project, item_id=item_id, cursor=last),
        "diff_fixed_first_parent": lambda: diff_view(project, item_id=item_id,
            base_oid=parent_oid, snapshot_id=snapshot),
    }
    results = {}
    for name, function in checks.items():
        results[name] = measure(function, args.samples)
        print(json.dumps({"measurement": name, **{k: v for k, v in results[name].items() if k != "timings_ms"}}), flush=True)
    assert list_items(project)["total"] == 1000
    report = {"schema": "gitgo.publish.acceptance.performance.v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {"root": str(root), "commits": count, "items": 1000, "ledger_bytes": len(encoded(ledger)),
                    "head_oid": scope["head_oid"], "snapshot_id": snapshot, "synthetic": True},
        "environment": {"os": platform.platform(), "architecture": platform.machine(),
                        "processor": platform.processor(), "python": platform.python_version(),
                        "git": git(repo, "--version")},
        "method": "Sequential in-process production read APIs, fixed Git OIDs, protected Git and real SQLite/CAS; no mocks. First read is reported separately from repeated warm queries. Query snapshot cache is enabled; OS file cache may be warm.",
        "measurements": results, "budgets_ms": {"warm_query_p95": 300, "first_screen": 1000, "key_feedback": 100},
        "limits": ["Backend API timings only; these are not terminal rendering or input latency measurements.",
                   "Synthetic linear Git ancestry; actual merge-parent behavior is covered by separate tests.",
                   "No remote requests, publication, private repository cloning or user configuration modification."],
        "warm_query_budget_pass": all(result["p95_ms"] <= 300 for result in results.values()),
        "first_read_budget_pass": all(result["first_read_ms"] <= 1000 for result in results.values())}
    (root / "performance-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    close_owned_storage()
    print(str(root / "performance-results.json"), flush=True)


if __name__ == "__main__":
    main()
