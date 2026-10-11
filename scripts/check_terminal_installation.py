"""Real installation/Host-save checks, explicitly not visible UI acceptance.

Discovery supplies every path. Only an isolated preference is written. A copy
of the real package with one substituted image must fail before any probe.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.config import ConfigManager
from backend.core.executable_identity import verify_git_bash, windows_system_executable
from backend.core.native_host import NativeHost


def check(report):
    report = report.resolve()
    report.relative_to(ROOT)
    directory = report.parent
    directory.mkdir(parents=True, exist_ok=False)
    original = ConfigManager.find_config()
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    config = copy.deepcopy(ConfigManager.load(strict=True))
    config.launcher = {"terminal": "auto", "configured": False, "command": "", "args": []}
    isolated = ConfigManager.save(config, directory / "config.json")
    os.environ["GITGO_CONFIG_PATH"] = str(isolated)
    os.environ["GITGO_STATE_HOME"] = str(directory / "state")
    host = NativeHost(stdout=io.StringIO())
    try:
        inventory = host._dispatch({"operation": "config.terminals", "arguments": {}, "request_id": "terminal-check"})
        chosen = next(option for option in inventory["options"] if option["id"] == "git_bash" and option["available"])
        host._dispatch({"operation": "config.set", "request_id": "terminal-save", "arguments": {
            "key": "launcher.terminal", "value": chosen["id"]}})
        saved = ConfigManager.load(strict=True).launcher
        if saved.get("command") != chosen["command"] or not saved.get("identity", {}).get("verified"):
            raise RuntimeError("Host did not populate the discovered verified preference")
        root = Path(chosen["command"]).parents[2]
        fake = directory / "substituted Git"
        for identity in chosen["identity"]["files"]:
            source = Path(identity["path"])
            target = fake / source.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        shutil.copyfile(windows_system_executable("System32/cmd.exe"), fake / "usr/bin/mintty.exe")
        probes = []
        def never_execute(*args, **kwargs):
            probes.append(True)
            raise RuntimeError("Substituted package must not execute")
        counterfeit = verify_git_bash(fake, probe_runner=never_execute)
        if counterfeit["verified"] or probes or "mintty.exe" not in counterfeit.get("message", ""):
            raise RuntimeError("Substituted unsigned companion was not rejected before probing")
        unchanged = hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
        if not unchanged:
            raise RuntimeError("Original configuration changed")
        result = {"verified": True, "initial_launcher": config.launcher,
                  "automatic_options": [{"id": o["id"], "label": o["label"]} for o in inventory["options"] if o["available"]],
                  "saved_terminal": saved["terminal"], "saved_command": saved["command"],
                  "verified_images": len(chosen["identity"]["files"]), "provenance": chosen["identity"]["provenance"],
                  "counterfeit_executed": bool(probes), "counterfeit_rejection": counterfeit,
                  "original_config_unchanged": unchanged, "warnings": inventory["warnings"],
                  "visible_window_verified": False, "api_task_executed": False}
        report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        host.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    check(parser.parse_args().report)
