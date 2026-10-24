"""Run the production Bun frontend transport against a real Native Host/API."""

import sys as _development_sys
from pathlib import Path as _DevelopmentPath
_development_sys.path.insert(0, str(_DevelopmentPath(__file__).resolve().parents[1]))
from scripts.development_paths import development_root, test_state_home
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.config import ConfigManager
from backend.core.llm_config import LLMConfigManager
from scripts.evaluate_alignment_behavior import digest_production

if __name__ == "__main__":
    source = digest_production()
    installed = LLMConfigManager._config_path().read_bytes()
    cfg = copy.deepcopy(ConfigManager.load())
    project = next(p for p in cfg.projects if p.name == "2920")
    home = development_root(ROOT) / 'runtime-hardening' / ("frontend-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    home.mkdir(parents=True)
    workspace = home / "workspace"
    workspace.mkdir()
    project.workspace_path = str(workspace)
    cfg.projects = [project]
    ConfigManager.save(cfg, home / "config.json", allow_empty=True)
    llm = LLMConfigManager.load()
    selected = next(p for p in llm["providers"] if "deepseek" in p.get("model_id", "").lower() and p.get("api_key"))
    llm = {**llm, "providers": [copy.deepcopy(selected)], "active_provider": selected["id"], "failover_enabled": False, "failover_order": []}
    saved = {key: os.environ.get(key) for key in ("GITGO_LLM_CONFIG_PATH", "GITGO_LLM_SECRET_PATH")}
    try:
        os.environ["GITGO_LLM_CONFIG_PATH"] = str(home / "llm.json")
        os.environ["GITGO_LLM_SECRET_PATH"] = str(home / "secrets.json")
        LLMConfigManager.save(llm)
    finally:
        for key, value in saved.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
    env = {**os.environ, "GITGO_CONFIG_PATH": str(home / "config.json"), "GITGO_STATE_HOME": str(test_state_home(workspace)),
        "GITGO_LLM_CONFIG_PATH": str(home / "llm.json"), "GITGO_LLM_SECRET_PATH": str(home / "secrets.json"),
        "GITGO_ACCEPTANCE_PROFILE": str(home), "GITGO_PYTHON": sys.executable, "GITGO_ROOT": str(ROOT), "PYTHONUTF8": "1"}
    for key in ("GITGO_INSTALL_ROOT", "GITGO_HOST_EXECUTABLE", "GITGO_LLM_BASE_URL", "GITGO_LLM_API_KEY", "GITGO_LLM_MODEL", "GITGO_LLM_PROTOCOL"):
        env.pop(key, None)
    print(json.dumps({"run": str(home), "model": selected["model_id"]}), flush=True)
    code = subprocess.call([str(Path.home() / ".bun/bun.exe"), "run", "scripts/evaluateTransport.ts"], cwd=ROOT / "cli/dashboard", env=env)
    (home / "verification.json").write_text(json.dumps({"exit_code": code, "sources_unchanged": source == digest_production(),
        "installed_config_unchanged": installed == LLMConfigManager._config_path().read_bytes(),
        "source_version": hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(), "visual_dashboard_verified": False}, indent=2))
    sys.exit(code)
