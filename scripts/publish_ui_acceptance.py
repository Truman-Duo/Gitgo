"""Exercise task navigation, durable drafts and a real merge graph in the Windows package."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from publish_acceptance_terminal import console_client
from publish_terminal_benchmark import LiveConsole


def run(package, root):
    package, root = Path(package).resolve(strict=True), Path(root).resolve(strict=True)
    desktop = (Path.home() / "Desktop").resolve()
    if not all(path.is_relative_to(desktop) for path in (package, root)):
        raise ValueError("Use the authorized Desktop acceptance directory")
    evidence = Path(tempfile.mkdtemp(prefix="publish-ui-", dir=root))
    print(json.dumps({"evidence": str(evidence)}), flush=True)
    workspace = evidence / "workspace"
    workspace.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(workspace), *args], capture_output=True, check=True, text=True, encoding="utf8").stdout.strip()
    git("init", "-b", "main"); git("config", "user.name", "UI Acceptance"); git("config", "user.email", "ui@gitgo.invalid")
    (workspace / "base.txt").write_text("base\n", encoding="utf8")
    git("add", "."); git("commit", "-m", "[UI-1] feat: base")
    git("checkout", "-b", "side")
    (workspace / "side.txt").write_text("side\n", encoding="utf8")
    git("add", "."); git("commit", "-m", "[UI-2] feat: side")
    git("checkout", "main")
    (workspace / "main.txt").write_text("main\n", encoding="utf8")
    git("add", "."); git("commit", "-m", "[UI-3] feat: main")
    git("merge", "--no-ff", "side", "-m", "[UI-4] feat: merge")
    head = git("rev-parse", "HEAD")
    os.environ.update(GITGO_STATE_HOME=str(evidence / "state"), GITGO_CONFIG_PATH=str(evidence / "config.json"),
        GITGO_LLM_CONFIG_PATH=str(evidence / "llm.json"))
    from backend.core.config import Config, ConfigManager, ProjectConfig
    from backend.core.application import ApplicationServices
    from backend.core.storage import close_owned_storage, get_storage
    project = ProjectConfig(name="publish-交互-🚀", workspace_path=str(workspace))
    ConfigManager.save(Config(projects=[project], language="zh", animation=False,
        launcher={"terminal": "current", "command": "", "args": []}))
    services = ApplicationServices()
    item = services.invoke("publish.prepare", {"project": project.name, "request_id": "ui-prepare", "source_role": "workspace",
        "source_ref": "HEAD", "target_branch": "publish/ui", "owner": "UI owner", "title": "Windows交互验收"})["item"]
    storage = get_storage(workspace)
    for process_id, task_id, parent, parent_task, response in (("main-ui", "main-ui-task", "", "", "UI-PARENT-PUBLIC-RESULT"),
        ("child-ui", "child-ui-task", "main-ui", "main-ui-task", "UI-CHILD-PUBLIC-RESULT")):
        storage.save_agent_checkpoint({"process_id": process_id, "session_id": process_id + "-session", "task_id": task_id,
            "parent_id": parent, "parent_task_id": parent_task, "actor_kind": "worker" if parent else "supervisor",
            "role": "worker" if parent else "main", "status": "completed", "messages": [{"role": "user", "content": "UI public request", "message_type": "conversation"}],
            "result": {"status": "completed", "response": response, "process_id": process_id}})
    item = services.invoke("publish.link_task", {"project": project.name, "request_id": "ui-link", "item_id": item["id"],
        "expected_revision": item["revision"], "process_id": "child-ui"})["item"]
    close_owned_storage()
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith(("GITGO_", "PYTHON"))}
    env.update(GITGO_INSTALL_ROOT=str(package), GITGO_STATE_HOME=str(evidence / "state"),
        GITGO_CONFIG_PATH=str(evidence / "config.json"), GITGO_LLM_CONFIG_PATH=str(evidence / "llm.json"), FORCE_COLOR="3", TERM="xterm-256color")
    env.pop("NO_COLOR", None)
    report = {"schema": "gitgo.publish.ui-acceptance.v1", "evidence": str(evidence), "package": str(package),
        "executable_sha256": hashlib.sha256((package / "gitgo.exe").read_bytes()).hexdigest(), "item_id": item["id"],
        "fixture_revision": item["revision"], "images": [], "passed": False, "visual_review": "pending"}
    console = None
    def launch(number):
        directory = evidence / ("process-" + str(number)); directory.mkdir()
        launcher = directory / "launch.py"
        launcher.write_text("import subprocess\nsubprocess.run(" + repr([str(package / "gitgo.exe"), "--attached"]) + ")\n", encoding="utf8")
        startup = subprocess.STARTUPINFO(); startup.dwFlags = subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = 4
        process = subprocess.Popen([str(Path(os.environ["SystemRoot"]) / "System32/conhost.exe"), sys.executable, str(launcher)],
            cwd=package, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
        result = LiveConsole(process, directory, console_client(process), visible=True)
        result.resize(120, 32)
        result.wait("publish-交互", "project", timeout=60)
        result.send("\r"); time.sleep(1); result.command("publish")
        return result
    def capture(name):
        console.capture(name); report["images"].append(console.screenshot(name))
    try:
        console = launch(1)
        console.wait("本地已保存记录", "publish")
        console.send("\r"); console.send("\r")
        console.wait("关联任务", "linked-item")
        for _ in range(5): console.key(virtual=40)
        time.sleep(.2); before_task = console.screen()[0]
        console.command("task 1"); console.wait("UI-CHILD-PUBLIC-RESULT", "child"); capture("child-conversation")
        console.send("p"); console.wait("UI-PARENT-PUBLIC-RESULT", "parent"); capture("parent-conversation")
        console.send("d"); console.wait("进程 ID", "details"); console.send("\x1b"); console.send("\x1b")
        console.wait("本地已保存记录", "return")
        assert console.screen()[0] == before_task, "Task navigation changed the Publish viewport"
        console.command("history"); console.wait("Git 分支 / 合并图", "graph")
        for width, height in ((120, 32), (80, 24), (40, 20)):
            console.resize(width, height); time.sleep(.3)
            screen, actual_w, actual_h = console.screen()
            assert (actual_w, actual_h) == (width, height)
            assert "02" in screen and "O" in screen and "+" in screen, "Merge lanes missing"
            capture(f"graph-{width}x{height}")
        console.send("\x1b"); console.resize(120, 32)
        console.command("plan"); console.send("\r"); console.send("\x15"); console.send("草稿负责人🚀")
        capture("draft-before-restart")
        state_files = list((evidence / "ui" / "publish").rglob("*.json"))
        assert any("草稿负责人🚀" in path.read_text(encoding="utf8") for path in state_files), "Draft was not durably stored"
        console.close(); console = None
        console = launch(2)
        console.wait("草稿负责人", "restored-draft"); capture("draft-after-restart")
        console.send("\x1b"); console.command("plan"); console.wait("草稿负责人", "reopened-draft")
        current = services.invoke("publish.item", {"project": project.name, "item_id": item["id"]})["item"]
        assert current["revision"] == item["revision"] and git("rev-parse", "HEAD") == head
        report.update(passed=True, task_navigation=True, parent_navigation=True, process_details=True,
            original_viewport_restored=True, real_git_merge_graph=True, draft_restored_in_fresh_process=True,
            no_business_operation_replayed=True, saved_draft="草稿负责人🚀")
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if console: capture("failure")
        raise
    finally:
        if console: console.close()
        (evidence / "result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf8")
        (root / "publish-ui-latest.txt").write_text(str(evidence), encoding="utf8")
    print(json.dumps({"result": str(evidence / "result.json"), "passed": report["passed"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--package", required=True); parser.add_argument("--root", required=True)
    args = parser.parse_args(); run(args.package, args.root)
