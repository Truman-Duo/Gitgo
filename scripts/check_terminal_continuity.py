"""Check frozen Host continuity using a local HTTP provider and isolated data.

This is a packaged protocol acceptance, not a visible terminal rendering test.
No user credentials, existing project data, or installed settings are changed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.config import Config, ConfigManager, ProjectConfig
from scripts.smoke_packaged_runtime import ProtocolClient


def check(host: Path, dashboard: Path | None = None, *, source=False) -> dict:
    requests: list[dict] = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            reply = f"continuity-answer-{len(requests)}"
            if body.get("stream"):
                chunks = [
                    {"id": "continuity", "choices": [{"index": 0, "delta": {"content": reply}}]},
                    {"id": "continuity", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}},
                ]
                wire = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
                content_type = "text/event-stream"
            else:
                wire = json.dumps({"id": "continuity", "choices": [{"message": {"role": "assistant", "content": reply},
                                    "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(wire.encode())))
            self.end_headers()
            self.wfile.write(wire.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="gitgo-continuity-") as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            config_path = root / "config.json"
            project = ProjectConfig(name="Continuity")
            project.workspace_path = str(workspace)
            ConfigManager.save(Config(projects=[project]), config_path, allow_empty=True)
            llm_path = root / "llm_config.json"
            llm_path.write_text(json.dumps({"providers": [{
                "id": "continuity", "name": "Local acceptance", "api_key": "local-test-only",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "model_id": "continuity", "protocol": "openai_chat",
            }], "active_provider": "continuity"}), encoding="utf-8")
            env = os.environ.copy()
            env.update({"GITGO_CONFIG_PATH": str(config_path), "GITGO_STATE_HOME": str(root / "state"),
                        "GITGO_LLM_CONFIG_PATH": str(llm_path),
                        "GITGO_LLM_SECRET_PATH": str(root / "provider_secrets.json"),
                        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})

            def start(terminal):
                current = dict(env)
                current["GITGO_FRONTEND_ORIGIN"] = json.dumps({"version": 1, "terminal": terminal,
                    "platform": "win32", "source": "packaged_protocol_acceptance"})
                return ProtocolClient(host, current, arguments=["-u", "-m", "backend.core.native_host"] if source else None)

            def call(client, operation, args=None):
                response = client.call(operation, args, timeout=60)
                if not response.get("ok"):
                    raise RuntimeError(f"{operation}: {response.get('error')}")
                return response["result"]

            first = start("windows_console")
            try:
                inventory = call(first, "config.terminals")
                git_bash_detected = any(option["id"] == "git_bash" and option["available"]
                                        for option in inventory["options"])
                # A generic Windows build worker need not have Git installed.
                # This machine exercises the discovered/verified Git Bash choice.
                preference = "git_bash" if git_bash_detected else "current"
                a = call(first, "runtime.chat", {"project": project.name, "message": "history-marker-one"})
                assert a["status"] == "completed" and a["llm_used"], a
                duplicate = subprocess.run([str(host), *(["-u", "-m", "backend.core.native_host"] if source else [])], input="", capture_output=True, text=True,
                                           encoding="utf-8", env=env, timeout=15)
                assert duplicate.returncode != 0 and "HOST_PROFILE_IN_USE" in duplicate.stdout, duplicate.stdout
                if dashboard:
                    frontend = subprocess.run([str(dashboard), "--attached"], input="", capture_output=True,
                                              text=True, encoding="utf-8", env=env, timeout=15)
                    assert frontend.returncode != 0 and "HOST_PROFILE_IN_USE" in frontend.stderr, frontend.stderr
                pid = first.process.pid
                call(first, "config.set", {"key": "launcher.terminal", "value": preference})
                assert first.process.pid == pid and first.process.poll() is None
                assert ConfigManager.load(config_path).launcher["terminal"] == preference
                ongoing = call(first, "runtime.chat", {"project": project.name, "message": "history-marker-still-current"})
                assert ongoing["status"] == "completed" and ongoing["session_id"] == a["session_id"]
            finally:
                first.close()
            second = start("git_bash")
            try:
                deadline = time.monotonic() + 15
                while True:
                    loaded = call(second, "runtime.status", {"project": project.name})
                    if loaded.get("session_id"):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Saved conversation was not restored")
                    time.sleep(.05)
                assert loaded["session_id"] == a["session_id"]
                assert "history-marker-one" in json.dumps(loaded.get("main_conversation"), ensure_ascii=False)
                b = call(second, "runtime.chat", {"project": project.name, "message": "history-marker-two"})
                assert b["status"] == "completed" and b["session_id"] == a["session_id"], b
                restored = call(second, "runtime.status", {"project": project.name})
                assert all(marker in json.dumps(restored["main_conversation"]) for marker in
                           ("history-marker-one", "history-marker-still-current", "history-marker-two",
                            "continuity-answer-1", "continuity-answer-2", "continuity-answer-3"))
                assert "history-marker-one" in json.dumps(requests[-1]["messages"])
            finally:
                second.close()
            stores = list((root / "state" / "projects").iterdir())
            assert len(stores) == 1
            origins = []
            for database in stores[0].glob("*.sqlite3"):
                connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
                try:
                    if not connection.execute("SELECT name FROM sqlite_master WHERE name='trace_events'").fetchone():
                        continue
                    for (raw_record,) in connection.execute("SELECT record_json FROM trace_events WHERE event_type='task_admitted'"):
                        record = json.loads(raw_record)
                        reference = record.get("detail_ref", "").split(":")[-1]
                        blob = stores[0] / "cas" / reference[:2] / reference[2:]
                        detail = json.loads(blob.read_text(encoding="utf-8"))
                        origins.append(detail.get("frontend_origin", {}).get("terminal"))
                finally:
                    connection.close()
            assert sorted(origins) == ["git_bash", "windows_console", "windows_console"], origins
            return {"status": "passed", "protocol_host": str(host), "host_kind": "source" if source else "packaged", "provider_requests": len(requests),
                    "shared_session": True, "shared_store_count": len(stores), "origins": origins,
                    "duplicate_host_rejected": True, "save_kept_current_host": True,
                    "git_bash_detected_and_verified": git_bash_detected, "saved_preference": preference,
                    "packaged_frontend_rejection_verified": bool(dashboard),
                    "visible_terminal_rendering_verified": False}
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--dashboard", type=Path)
    parser.add_argument("--source", action="store_true", help="Use a Python executable as Host")
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    result = check(arguments.host.resolve(), arguments.dashboard.resolve() if arguments.dashboard else None, source=arguments.source)
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
