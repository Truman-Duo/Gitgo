"""Real API + Native Host alignment acceptance in a separate profile.

Does not modify installed configuration, print secrets, or claim visual QA.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.config import ConfigManager
from backend.core.llm_config import LLMConfigManager
from scripts.check_provider_failover import ObservedClient


def check(project="2920"):
    original_path = LLMConfigManager._config_path()
    original_bytes = original_path.read_bytes()
    provider_config = LLMConfigManager.load()
    providers = provider_config["providers"]
    selected = next((p for p in providers if "deepseek" in str(p.get("model_id", "")).lower() and p.get("api_key")), None)
    if selected is None:
        selected = next(p for p in providers if p["id"] == provider_config["active_provider"] and p.get("api_key"))
    with tempfile.TemporaryDirectory(prefix="gitgo-alignment-acceptance-") as raw:
        root = Path(raw)
        cfg = copy.deepcopy(ConfigManager.load())
        chosen = next(p for p in cfg.projects if p.name == project)
        cfg.projects = [chosen]
        ConfigManager.save(cfg, root / "config.json", allow_empty=True)
        provider_config = {**provider_config, "providers": [copy.deepcopy(selected)], "active_provider": selected["id"],
                           "failover_enabled": False, "failover_order": []}
        saved = {key: os.environ.get(key) for key in ("GITGO_LLM_CONFIG_PATH", "GITGO_LLM_SECRET_PATH")}
        try:
            os.environ["GITGO_LLM_CONFIG_PATH"] = str(root / "llm.json")
            os.environ["GITGO_LLM_SECRET_PATH"] = str(root / "secrets.json")
            LLMConfigManager.save(provider_config)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        env = os.environ.copy()
        for key in ("GITGO_LLM_BASE_URL", "GITGO_LLM_API_KEY", "GITGO_LLM_MODEL", "GITGO_LLM_PROTOCOL"):
            env.pop(key, None)
        env.update({"GITGO_CONFIG_PATH": str(root / "config.json"), "GITGO_STATE_HOME": str(root / "state"),
                    "GITGO_LLM_CONFIG_PATH": str(root / "llm.json"), "GITGO_LLM_SECRET_PATH": str(root / "secrets.json"),
                    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        client = ObservedClient(Path(sys.executable), env, arguments=["-u", "-m", "backend.core.native_host"])
        def invoke(operation, args=None, timeout=240):
            response = client.call(operation, args or {}, timeout=timeout)
            if not response.get("ok"):
                raise RuntimeError(f"{operation}: {response.get('error', {}).get('code')}")
            return response["result"]
        def pending():
            status = invoke("runtime.status", {"project": project}, 30)
            for p in status["processes"].values():
                if p.get("pending_decision"):
                    return p["pending_decision"]
            raise AssertionError("Host did not publish a pending question")
        try:
            prompt = (
                "这是需求对齐的真实验收，只做方案，不改文件，不启动子代理。"
                "我想做通知中心。请用 engineering_workflow configure profiles=[alignment] 建立三题："
                "scope(state_topic=scope)和audience(state_topic=audience)独立；"
                "roles(state_topic=roles)依赖scope，when={scope:[团队]}。"
                "每题request用kind=clarification，question和why_user_must_decide写正常中文，options=[]。"
                "scope问个人还是团队；audience问目标用户；roles问团队角色。"
                "然后用 ask_frontier 展示第一轮，等我回答后继续后续轮；不要提前回答或确认。"
            )
            initial = invoke("runtime.chat", {"project": project, "message": prompt})
            assert initial["status"] == "awaiting_user" and initial["llm_used"]
            first = pending()
            assert [q["node_id"] for q in first.get("questions", [])] == ["scope"], "first question frontier is incorrect"
            args = {"project": project, "task_id": first["task_id"], "process_id": first["process_id"], "decision_id": first["decision_id"],
                    "message": json.dumps({"answers": {"scope": "团队"}}, ensure_ascii=False)}
            continued = invoke("runtime.decision", args)
            assert continued["status"] == "awaiting_user"
            second = pending()
            assert [q["node_id"] for q in second.get("questions", [])] == ["audience"], "independent question was not recomputed after the first answer"
            continued = invoke("runtime.decision", {**args, "decision_id": second["decision_id"], "message": json.dumps({"answers": {"audience": "开发者"}}, ensure_ascii=False)})
            assert continued["status"] == "awaiting_user"
            third = pending()
            assert [q["node_id"] for q in third.get("questions", [])] == ["roles"], "dependent question was not asked after its prerequisite"
            final = invoke("runtime.decision", {**args, "decision_id": third["decision_id"], "message": json.dumps({"answers": {"roles": "维护者和成员"}}, ensure_ascii=False)})
            assert final["status"] == "completed" and final["llm_used"], final.get("status")
            status = invoke("runtime.status", {"project": project}, 30)
            assert not any(p.get("pending_decision") for p in status["processes"].values())
            assert status.get("main_conversation"), "answers were not persisted"
            assert len({row['decision']['decision_id'] for row in status["main_conversation"] if row.get('decision') and row['decision'].get('questions')}) >= 3, "individual questions are missing from persisted history"
            assert any(e.get("event") == "decision_required" for e in client.events)
            result = {"passed": True, "kind": "real_api_native_host", "project": project, "model": selected["model_id"],
                      "round_sizes": [1, 1, 1], "same_task": first["task_id"] == second["task_id"] == third["task_id"],
                      "events_reached_frontend_transport": True, "installed_configuration_unchanged": original_path.read_bytes() == original_bytes,
                      "visual_dashboard_verified": False, "completed": True, "rounds_in_persisted_history": True}
            assert result["installed_configuration_unchanged"]
            print(json.dumps(result, ensure_ascii=False))
            (ROOT / ".gitgo" / "alignment-regression-verified.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="2920")
    check(parser.parse_args().project)
