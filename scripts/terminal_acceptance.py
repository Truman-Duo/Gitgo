"""Prepare an isolated formal Dashboard first-run acceptance, without launcher paths.

Discovery supplies choices; key events select an ID; config.set derives the
launcher path. Provider secrets remain in their existing user-scoped store.
Real counterfeit images are inspected but never executed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import base64
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.config import ConfigManager
from backend.core.llm_config import LLMConfigManager
from backend.core.terminal_launcher import terminal_inventory
from backend.core.executable_identity import verify_git_bash


def prepare(directory):
    directory.mkdir(parents=True, exist_ok=False)
    original_path = ConfigManager.find_config()
    original_hash = hashlib.sha256(original_path.read_bytes()).hexdigest()
    config = copy.deepcopy(ConfigManager.load(strict=True))
    if not any(p.name == "gitgo" and Path(p.workspace_path).resolve() == ROOT for p in config.projects):
        raise RuntimeError("Acceptance requires the existing Gitgo source project")
    provider = LLMConfigManager.get_active()
    if provider is None or provider.model_id != "deepseek-v4-flash":
        raise RuntimeError("Paid acceptance is limited to the configured deepseek-v4-flash")
    inventory = terminal_inventory()
    available = [o for o in inventory["options"] if o["available"]]
    choice_index = next(i for i, option in enumerate(available) if option["id"] == "git_bash")
    # No discovered executable path is copied into this first-run config.
    config.launcher = {"terminal": "auto", "command": "", "args": [], "configured": False}
    config_path = ConfigManager.save(config, directory / "config.json")
    inputs = [{"afterMs": 12000 + i * 1000, "input": "", "key": {"rightArrow": True}} for i in range(choice_index)]
    inputs.append({"afterMs": 15000 + choice_index * 1000, "input": "", "key": {"return": True}})
    encode = lambda value: base64.b64encode(value.encode("utf-8")).decode("ascii")
    environment = {
        "GITGO_CONFIG_PATH": str(config_path), "GITGO_STATE_HOME": str(directory / "state"),
        "GITGO_LLM_CONFIG_PATH": str(LLMConfigManager._config_path()),
        "GITGO_LLM_SECRET_PATH": str(LLMConfigManager._secret_path()),
        "GITGO_PYTHON": sys.executable, "GITGO_BUN": str(Path.home() / ".bun/bun.exe"),
        "FORCE_COLOR": "3", "TERM": "xterm-256color", "PYTHONUTF8": "1",
    }
    prompt = """Perform a bounded read-only terminal acceptance in this existing project.
Do not write files, execute commands, delegate, invent tools or change settings.
Call search_text exactly once on backend/core/tools/catalog.py for the literal
string def build_workspace_tools, literal=true, max_results=1. Report the
actual definition line and engine. Stop after this call. TASK_COMPLETE.
"""
    literal = lambda value: "'" + str(value).replace("'", "''") + "'"
    runner = directory / "run.ps1"
    lines = [f"$env:{key} = {literal(value)}" for key, value in environment.items()]
    lines += ["Remove-Item Env:NO_COLOR -ErrorAction SilentlyContinue",
              f"Set-Location -LiteralPath {literal(ROOT)}",
              f"& {literal(ROOT / 'run_dashboard_native.bat')} --smoke-input-b64 {literal(encode(json.dumps(inputs)))} --smoke-project gitgo --smoke-task-b64 {literal(encode(prompt))}",
              "exit $LASTEXITCODE"]
    runner.write_text("\n".join(lines), encoding="utf-8")
    # Use a valid Microsoft-signed unrelated image with the deceptive Bash name.
    fake_root = directory / "counterfeit Git"
    system_cmd = Path(os.environ["SystemRoot"]) / "System32/cmd.exe"
    for relative in ["git-bash.exe", "bin/bash.exe", "usr/bin/bash.exe"]:
        target = fake_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(system_cmd, target)
    probes = []
    def never_execute(*args, **kwargs):
        probes.append(True)
        raise RuntimeError("Counterfeit probe must never execute")
    counterfeit = verify_git_bash(fake_root, probe_runner=never_execute)
    if counterfeit["verified"] or probes:
        raise RuntimeError("Counterfeit was not rejected before execution")
    report = {"original_config": str(original_path), "original_config_sha256": original_hash,
              "isolated_config": str(config_path), "runner": str(runner),
              "initial_launcher": config.launcher, "automatic_options": [o["id"] for o in available],
              "selected_by_key_events": "git_bash", "counterfeit_executed": False,
              "handoff_precedes_interactive_gate_fix": False,
              "counterfeit_rejection": counterfeit, "warnings": inventory["warnings"]}
    (directory / "preparation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"runner": str(runner), "automatic_options": report["automatic_options"],
                      "counterfeit_rejection": counterfeit["code"]}))


def collect(directory):
    prep = json.loads((directory / "preparation.json").read_text(encoding="utf-8"))
    def database(name):
        path = next((directory / "state").rglob(name))
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    with database("state.sqlite3") as state, database("observability.sqlite3") as trace:
        statuses = [row[0] for row in state.execute("select status from tasks")]
        records = [json.loads(row[0]) for row in trace.execute(
            "select record_json from trace_events where event_type='tool_result'")]
        receipts = []
        cas = next((directory / "state").rglob("cas"))
        for (reference,) in state.execute("select evidence_ref from receipts"):
            digest = reference.split(":")[-1]
            data = json.loads((cas / digest[:2] / digest[2:]).read_text(encoding="utf-8"))
            receipts.append({key: data.get(key) for key in (
                "tool_name", "succeeded", "committed", "search_engine", "search_complete")})
    launcher = json.loads(Path(prep["isolated_config"]).read_text(encoding="utf-8"))["launcher"]
    report = {"task_statuses": statuses, "saved_terminal": launcher["terminal"],
              "saved_verified_identity": bool((launcher.get("identity") or {}).get("verified")),
              "tool_results": [{key: record.get(key) for key in ("tool_name", "is_error")} for record in records],
              "receipts": receipts,
              "original_config_unchanged": hashlib.sha256(Path(prep["original_config"]).read_bytes()).hexdigest() == prep["original_config_sha256"],
              "visible_window_verified": False,
              "handoff_precedes_interactive_gate_fix": prep.get("handoff_precedes_interactive_gate_fix", True),
              "note": "Process/API evidence only. User did not see the window. Native UI tool blocks terminal apps; visible formal acceptance is pending."}
    (directory / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--collect", action="store_true")
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.relative_to(ROOT)
    collect(directory) if args.collect else prepare(directory)
