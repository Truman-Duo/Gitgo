"""Drive the installed Dashboard through a PTY with a loopback fixture provider."""
import argparse
import base64
import errno
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    from backend.core.config import Config, ConfigManager, ProjectConfig
    from backend.core.storage import close_owned_storage, get_storage

    if not getattr(sys, "frozen", False):
        raise RuntimeError("Acceptance requires the packaged frozen Host")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--command", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    args = parser.parse_args()
    name, workspace, commit = args.service, args.workspace.resolve(strict=True), args.commit
    root = args.artifact_dir.resolve(strict=True)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid fixture service or snapshot identity")
    if root == workspace or root.is_relative_to(workspace):
        raise ValueError("Artifacts must be outside the fixture project")
    if os.getuid() < 501 or os.getuid() != os.geteuid():
        raise RuntimeError("Acceptance requires an ordinary login account")
    if subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).strip().startswith("VirtualMac") is False:
        raise RuntimeError("Acceptance runs only in the disposable macOS VM")
    if subprocess.check_output(["/usr/bin/csrutil", "status"], text=True).strip() != "System Integrity Protection status: enabled.":
        raise RuntimeError("SIP must be fully enabled")
    target = workspace / "fixture.txt"
    before = b"sealed fixture\n"
    after = b"approved dashboard publication\n"
    require(target.read_bytes() == before, "Fixture initial contents differ")

    project = ProjectConfig(name="Approval lab")
    project.workspace_path = str(workspace)
    project.macos_worktree = {"service": name, "snapshot_commit": commit}
    config = Config(projects=[project])
    config.launcher["terminal"] = "current"
    ConfigManager.save(config)
    get_storage(workspace)
    close_owned_storage()

    guard = threading.Lock()
    steps = {"root": 0, "child": 0}
    child_id = ""
    root_scope = ""
    root_task_id = ""
    uuid_pattern = r'"process_id"\s*:\s*"([0-9a-f-]{36})"'
    prompt = "Create a new B worker to replace fixture.txt with the approved fixture, then verify publication approval."
    logs = root / "provider.jsonl"

    def choose(body):
        nonlocal child_id, root_scope
        tools = {item.get("function", {}).get("name") for item in body.get("tools", [])}
        messages = body.get("messages", [])
        transcript = "\n".join(str(item.get("content") or "") for item in messages
                               if item.get("role") == "tool")
        framed = "gitgo_call" in tools
        if framed:
            context = next((str(m.get("content") or "") for m in reversed(messages)
                            if str(m.get("content") or "").startswith("[HOST CURRENT CONTEXT]")), "")
            scopes = re.findall(r'"scope"\s*:\s*"([^"]+)"', context)
            if not scopes:
                return None, {}
            if not root_scope:
                root_scope = scopes[-1]
            tools.update({"delegate_task", "promote_agent_changes"} if scopes[-1] == root_scope else {"write_file"})
        if "declare_task_contract" in tools and "delegate_task" not in tools and not steps["root"]:
            return "declare_task_contract", {
                "goal": "Validate publication approval in the isolated fixture",
                "execution_mode": "delegate", "delegation_required": True,
                "deliverables": [{"kind": "workspace_file", "path": "fixture.txt", "required": True}],
                "acceptance_criteria": ["Replace the exact fixture content only after approval"],
                "uncertainties": [], "requires_user_decision": False,
                "user_requested_delegation": True, "user_request_evidence": prompt,
            }
        if "delegate_task" in tools or ("promote_agent_changes" in tools and "write_file" not in tools):
            role = "root"
            step = steps[role]
            if step == 0:
                action, args = "delegate_task", {
                    "task_description": "Replace fixture.txt with the exact approved dashboard publication fixture.",
                    "capability_profile_id": "development.workspace", "task_kind": "action",
                    "target_files": ["fixture.txt"], "acceptance_criteria": ["One committed fixture file replacement"],
                    "allowed_tools": ["write_file"], "max_steps": 6,
                }
            else:
                if not child_id:
                    ids = re.findall(uuid_pattern, transcript)
                    if not ids:
                        raise RuntimeError("No delegated child id: " + transcript[-1400:])
                    child_id = ids[0]
                if step == 1:
                    action, args = "wait_agents", {"process_ids": [child_id], "timeout": 35}
                elif step == 2:
                    action, args = "review_child_outcome", {"process_id": child_id, "verdict": "approved",
                                                            "summary": "Reviewed completed fixture replacement"}
                elif step in (3, 4, 5):
                    action, args = "promote_agent_changes", {"process_ids": [child_id]}
                else:
                    action, args = "complete_supervision", {"result": "Dashboard publication fixture checked."}
        elif "write_file" in tools:
            role = "child"
            step = steps[role]
            if step == 0:
                action, args = "write_file", {"path": "fixture.txt", "content": after.decode(),
                                              "create_only": False, "expected_sha256": hashlib.sha256(before).hexdigest()}
            else:
                action, args = "complete_task", {"result": "Fixture updated", "verification": [],
                                                  "files": ["fixture.txt"], "diff_summary": "Replace one fixture line."}
        else:
            return None, {}
        if not framed and action not in tools:
            raise RuntimeError(f"Missing expected {action}; offered {sorted(tools)}")
        steps[role] += 1
        return action, args

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            nonlocal root_task_id
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(length))
            with guard:
                try:
                    action, args = choose(body)
                    with logs.open("a") as log:
                        print(json.dumps({"action": action, "args": args, "steps": steps, "offered": [v.get("function", {}).get("name") for v in body.get("tools", [])], "request_tail": str(body.get("messages", [])[-1:])[:800],
                            "tool_results": [m.get("content") for m in body.get("messages", []) if m.get("role") == "tool"][-4:]}), file=log)
                except Exception as exc:
                    with logs.open("a") as log:
                        print(json.dumps({"error": str(exc), "steps": steps}), file=log)
                    self.send_error(400)
                    return
            message = {"role": "assistant"}
            if action:
                if any(x.get("function", {}).get("name") == "gitgo_call" for x in body.get("tools", [])):
                    function = {"name": "gitgo_call", "arguments": json.dumps({"name": action, "arguments": args})}
                else:
                    function = {"name": action, "arguments": json.dumps(args)}
                call = {"index": 0, "id": f"fixture-{steps['root']}-{steps['child']}",
                        "type": "function", "function": function}
                message["tool_calls"] = [call]
            else:
                from backend.core.loop.alignment_payload import decode
                try:
                    alignment = decode(str(body["messages"][-1]["content"]))
                except (KeyError, TypeError, ValueError):
                    alignment = {}
                if isinstance(alignment, dict) and alignment.get("stage"):
                    maintenance = alignment.get("task_maintenance") or {}
                    if alignment.get("original_request") == prompt and maintenance.get("task_id"):
                        root_task_id = maintenance["task_id"]
                    quote = alignment.get("original_request") or alignment["request"]
                    assessment = {"status": "ready", "interpretations": [], "questions": [],
                                  "assumptions": [], "request_frame": {"kind": "deliverable",
                                  "source_quote": quote[:1200], "outcome": "Complete the isolated fixture acceptance"}}
                    if alignment["stage"] == "delivery":
                        candidate = alignment.get("candidate")
                        evidence = candidate if isinstance(candidate, str) else (
                            "Fixture updated" if (alignment.get("request") or "").startswith("Replace fixture.txt")
                            else "Dashboard publication fixture checked.")
                        assessment["delivery_check"] = {"complete": True, "evidence": [evidence]}
                    message["content"] = json.dumps(assessment)
                else:
                    message["content"] = "Fixture provider ready."
            reason = "tool_calls" if action else "stop"
            if body.get("stream"):
                chunks = [
                    {"id": "fixture", "choices": [{"index": 0, "delta": message}]},
                    {"id": "fixture", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                     "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}},
                ]
                wire = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                wire = json.dumps({"id": "fixture", "choices": [{"message": message, "finish_reason": reason}],
                                  "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}}).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Path(os.environ["GITGO_LLM_CONFIG_PATH"]).write_text(json.dumps({"providers": [{
        "id": "fixture", "name": "Loopback acceptance provider", "kind": "local", "auth_mode": "none",
        "base_url": f"http://127.0.0.1:{server.server_port}/v1", "model_id": "fixture", "protocol": "openai_chat",
    }], "active_provider": "fixture"}))

    def interrupted(_signum, _frame):
        raise RuntimeError("Dashboard acceptance interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 68, 155, 0, 0))
    cmd = [str(args.command.absolute()), "--attached", "--smoke-project", project.name,
           "--smoke-manual-delegation", "--smoke-task-b64", base64.b64encode(prompt.encode()).decode()]
    terminal = bytearray()
    ansi = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
    screen_log = root / "dashboard.terminal"
    events = []

    with (root / "dashboard.stderr").open("wb") as err:
        proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=err,
                                env=dict(os.environ, TERM="xterm-256color", FORCE_COLOR="1"), start_new_session=True)
        os.close(slave)

        def poll():
            if select.select([master], [], [], .15)[0]:
                try:
                    terminal.extend(os.read(master, 65536))
                    if len(terminal) > 16 * 1024 * 1024:
                        raise RuntimeError("Dashboard terminal output exceeded acceptance budget")
                    screen_log.write_bytes(terminal)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if proc.poll() is not None:
                raise RuntimeError(f"Dashboard exited: {proc.returncode}")

        def wait_for(condition, label, timeout=45):
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                poll()
                if condition():
                    return
            raise RuntimeError("Timed out waiting for " + label)

        card = b"Allow Gitgo to publish the selected child changes into your project?"
        def card_ready(offset, step):
            rendered = ansi.sub(b"", terminal[offset:])
            return (steps["root"] >= step and card in rendered and b"Allow once" in rendered
                    and b"Deny" in rendered and b"Permission required" in rendered)

        def settle(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                poll()

        def choose_option(index):
            settle(1.4)
            os.write(master, str(index).encode())
            settle(.5)
            offset = len(terminal)
            os.write(master, b"\r")
            return offset

        def stat():
            data = target.read_bytes()
            info = target.stat()
            return data, info.st_ino, info.st_mtime_ns

        baseline = stat()
        try:
            wait_for(lambda: card_ready(0, 4), "first permission card", 65)
            require(stat() == baseline, "Unapproved publication changed the fixture")
            events.append("paused with project unchanged")
            offset = choose_option(2)
            # The next model invocation creates its own permission decision.
            wait_for(lambda: card_ready(offset, 5), "second permission card after denial", 30)
            require("TOOL_PERMISSION_DENIED" in logs.read_text(), "No permission denial receipt")
            require(stat() == baseline, "Unapproved publication changed the fixture")
            events.append("denied with project unchanged")
            offset = choose_option(1)
            wait_for(lambda: target.read_bytes() == after, "authorized write", 30)
            published = stat()
            require(published != baseline, "Approved publication did not replace the fixture")
            events.append("approved and published")
            wait_for(lambda: card_ready(offset, 6), "repeat permission", 30)
            require(stat() == published, "Repeat publication rewrote the fixture")
            choose_option(1)
            wait_for(lambda: steps["root"] >= 7, "repeat finished", 30)
            require(stat() == published, "Repeat publication rewrote the fixture")
            require("already_promoted" in logs.read_text(), "No idempotent publication receipt")
            events.append("repeat verified without a second write")
            result = {"passed": True, "events": events, "before": baseline[1:], "after": published[1:],
                      "promotion_id": root_task_id, "child_id": child_id}
        finally:
            screen_log.write_bytes(terminal)
            (root / "dashboard.events.json").write_text(json.dumps(events, indent=2))
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            os.close(master)
            server.shutdown()
            server.server_close()

    print(json.dumps(result))


if __name__ == "__main__":
    main()
