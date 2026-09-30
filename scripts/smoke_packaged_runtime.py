"""Exercise the frozen Host, Daemon and isolated tool runner as one product."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from backend.core.child_process import INTERNAL_ROLE_FLAG, TOOL_RUNNER_ROLE
from backend.core.config import Config, ConfigManager, ProjectConfig


def _read_lines(stream, output: queue.Queue) -> None:
    for line in stream:
        if line.strip():
            output.put(line)


class ProtocolClient:
    def __init__(self, executable: Path, env: dict[str, str]):
        self.process = subprocess.Popen(
            [str(executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.reader = threading.Thread(
            target=_read_lines, args=(self.process.stdout, self.lines), daemon=True,
        )
        self.reader.start()
        started = self._next_message(30)
        if started.get("type") != "host_started":
            raise RuntimeError(f"packaged Host handshake failed: {started}")

    def _next_message(self, timeout: float) -> dict:
        try:
            return json.loads(self.lines.get(timeout=timeout))
        except queue.Empty as exc:
            stderr = self.process.stderr.read(2000) if self.process.poll() is not None else ""
            raise TimeoutError(f"packaged Host response timed out; stderr={stderr}") from exc

    def call(self, operation: str, arguments: dict | None = None, timeout: float = 30) -> dict:
        request_id = f"smoke-{time.monotonic_ns()}"
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({
            "protocol_version": 1,
            "type": "request",
            "request_id": request_id,
            "operation": operation,
            "arguments": arguments or {},
        }) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"packaged operation timed out: {operation}")
            message = self._next_message(remaining)
            if message.get("type") == "response" and message.get("request_id") == request_id:
                return message

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise RuntimeError("packaged Host did not terminate its Daemon tree")


def smoke(executable: Path) -> None:
    executable = executable.resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)

    with tempfile.TemporaryDirectory(prefix="gitgo-packaged-smoke-") as raw:
        root = Path(raw)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "smoke.txt").write_text("packaged runtime\n", encoding="utf-8")
        config_path = root / "config.json"
        project = ProjectConfig(name="Packaged Smoke")
        project.workspace_path = str(workspace)
        ConfigManager.save(Config(projects=[project]), config_path, allow_empty=True)

        env = os.environ.copy()
        env.update({
            "GITGO_CONFIG_PATH": str(config_path),
            "GITGO_STATE_HOME": str(root / "state"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        })

        tool = subprocess.run(
            [str(executable), INTERNAL_ROLE_FLAG, TOOL_RUNNER_ROLE],
            input=json.dumps({"tool_name": "file_list", "args": {"path": "."}}),
            cwd=workspace,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
        if tool.returncode != 0:
            raise RuntimeError(f"packaged tool runner exited {tool.returncode}: {tool.stderr}")
        tool_result = json.loads(tool.stdout)
        names = {item["name"] for item in tool_result.get("data", {}).get("files", [])}
        if not tool_result.get("success") or "smoke.txt" not in names:
            raise RuntimeError(f"packaged tool runner returned invalid result: {tool_result}")

        nested_python = subprocess.run(
            [str(executable), INTERNAL_ROLE_FLAG, TOOL_RUNNER_ROLE],
            input=json.dumps({
                "tool_name": "exec_command",
                "args": {
                    "_workspace": str(workspace),
                    "cwd": ".",
                    "argv": ["python", "-B", "-c", "print('packaged-python-ok')"],
                },
            }),
            cwd=workspace,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
        nested_result = json.loads(nested_python.stdout)
        nested_data = nested_result.get("data", {})
        if (
            nested_python.returncode != 0
            or not nested_result.get("success")
            or not nested_data.get("success")
            or nested_data.get("stdout") != "packaged-python-ok\n"
        ):
            raise RuntimeError(
                "packaged nested Python execution failed: "
                f"returncode={nested_python.returncode} result={nested_result} "
                f"stderr={nested_python.stderr}"
            )

        client = ProtocolClient(executable, env)
        try:
            overview = client.call("project.overview", timeout=10)
            projects = overview.get("result", {}).get("projects", [])
            if not overview.get("ok") or not any(p.get("name") == project.name for p in projects):
                raise RuntimeError(f"packaged project overview failed: {overview}")

            # These requests must fail before a Provider or Daemon is touched.
            # They prove that the frozen Host contains the same Unicode and
            # byte-integrity boundary as the source runtime.
            corrupt = client.call(
                "runtime.chat",
                {"project": project.name, "message": "bad\udcaf"},
            )
            corrupt_code = str((corrupt.get("error") or {}).get("code") or "")
            if corrupt.get("ok") or corrupt_code != "INPUT_ENCODING_CORRUPTED":
                raise RuntimeError(f"packaged Host accepted corrupt Unicode: {corrupt}")

            canary = "请解释罕见字符𠮷"
            digest_mismatch = client.call(
                "runtime.chat",
                {
                    "project": project.name,
                    "message": canary,
                    "message_utf8_sha256": hashlib.sha256(b"different").hexdigest(),
                },
            )
            mismatch_code = str((digest_mismatch.get("error") or {}).get("code") or "")
            if digest_mismatch.get("ok") or mismatch_code != "INPUT_INTEGRITY_MISMATCH":
                raise RuntimeError(f"packaged Host accepted a mismatched prompt digest: {digest_mismatch}")

            # Manual compaction starts an offline project's Daemon before it
            # checks the process id. PROCESS_NOT_FOUND is expected; a Host or
            # Daemon startup failure is not.
            compact = client.call(
                "runtime.compact",
                {"project": project.name, "process_id": "smoke-missing"},
                timeout=60,
            )
            error_code = str((compact.get("error") or {}).get("code") or "")
            if error_code == "DAEMON_START_FAILED":
                raise RuntimeError(f"packaged Daemon failed to start: {compact}")

            status = client.call("runtime.status", {"project": project.name}, timeout=15)
            if not status.get("ok") or not status.get("result", {}).get("daemon_online"):
                raise RuntimeError(f"packaged runtime status is not online: {status}")
        finally:
            client.close()

        project_ids = list((root / "state" / "projects").glob("*/state.sqlite3"))
        if len(project_ids) != 1:
            raise RuntimeError(f"packaged Daemon did not create one state database: {project_ids}")

    print("Packaged Host/Daemon/tool-runner smoke passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    args = parser.parse_args()
    smoke(args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
