"""Launch the complete formal dashboard with a repeatable alignment test profile."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.llm_config import LLMConfigManager
from scripts.terminal_test_launcher import launch, write_json


def initialize(root: Path, _session: dict):
    _session['preserve_runtime_evidence'] = True
    write_json(root / 'session.json', _session)
    original = LLMConfigManager.load()
    selected = next((p for p in original["providers"] if "deepseek" in p.get("model_id", "").lower() and p.get("api_key")), None)
    if selected is None:
        selected = next(p for p in original["providers"] if p["id"] == original["active_provider"])
    saved = {key: os.environ.get(key) for key in ("GITGO_LLM_CONFIG_PATH", "GITGO_LLM_SECRET_PATH")}
    try:
        os.environ["GITGO_LLM_CONFIG_PATH"] = str(root / "llm_config.json")
        os.environ["GITGO_LLM_SECRET_PATH"] = str(root / "provider_secrets.json")
        LLMConfigManager.save({**original, "providers": [copy.deepcopy(selected)], "active_provider": selected["id"], "failover_enabled": False, "failover_order": []})
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("[gitgo-test] Formal alignment UI test: see docs/alignment-workflow.md for the exact task.", flush=True)
    print("[gitgo-test] Select a terminal, save, enter project 2920. Reply in NORMAL; each answer goes to Host before another question is selected.", flush=True)
    print("[gitgo-test] This profile uses the existing model credential; original state is unchanged.", flush=True)


if __name__ == "__main__":
    try:
        for key in ("GITGO_LLM_BASE_URL", "GITGO_LLM_API_KEY", "GITGO_LLM_MODEL", "GITGO_LLM_PROTOCOL"):
            os.environ.pop(key, None)
        sys.exit(launch(profile_initializer=initialize))
    except Exception as exc:
        print(f"[gitgo-test] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
