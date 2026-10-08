from __future__ import annotations

import ctypes as C
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.core.sandbox import (
    SANDBOXED_HANDLERS, SandboxDenied, SandboxPolicy,
    sandbox_environment, sandbox_popen,
)
from backend.core.process_control import close_job, terminate_tree
from backend.core.sandbox_io import BoundedCommunication


def test_sensitive_handlers_have_no_model_selectable_opt_out():
    assert {"shell_script", "exec_command", "run_command", "run_test",
            "authored_python", "authored_privileged_python"} <= SANDBOXED_HANDLERS


def test_environment_is_an_allowlist():
    env = sandbox_environment({"SystemRoot": "system", "LOCALAPPDATA": "local",
                               "MY_OPAQUE_CREDENTIAL": "secret", "PYTHONPATH": "evil",
                               "GITGO_TOOL_REGISTRY_MODULE": "evil"})
    assert env["SystemRoot"] == "system"
    assert "MY_OPAQUE_CREDENTIAL" not in env
    assert "PYTHONPATH" not in env
    assert "GITGO_TOOL_REGISTRY_MODULE" not in env


def test_no_backend_fails_closed(monkeypatch, tmp_path_factory):
    monkeypatch.setattr("backend.core.sandbox.sys.platform", "unsupported")
    with pytest.raises(SandboxDenied, match="No native sandbox"):
        sandbox_popen(["untrusted"], SandboxPolicy(tmp_path_factory))
    result = SandboxDenied("SANDBOX_UNAVAILABLE", "blocked").result()
    assert result["effect_state"] == "not_committed"
    assert result["error_info"]["catalog_id"] == "GITGO-E3601"


def test_root_workspace_is_rejected():
    with pytest.raises(SandboxDenied):
        SandboxPolicy(Path(Path.cwd().anchor))


def test_grant_is_bound_to_source_resources_process_and_expiry(tmp_path_factory):
    from backend.core.loop.agent_tool import AgentTool
    from backend.core.loop.permission_broker import matching_grant, tool_contract_digest
    tool = AgentTool("sensitive", "test", {}, lambda args: {},
                     approval_per_invocation=True, resources=["process:python"],
                     composite_spec={"version": 1, "source_sha256": "first"})
    from backend.core.loop.permission_broker import arguments_digest
    grant = {"tool_name": tool.name, "task_id": "task", "process_id": "worker",
             "arguments_digest": arguments_digest({"value": 1}),
             "tool_contract_digest": tool_contract_digest(tool),
             "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
             "remaining_uses": 1}
    process = SimpleNamespace(active_task_id="task", process_id="worker", approval_grants=[grant])
    def match():
        return matching_grant(process, tool.name, {"value": 1}, per_invocation=True, tool=tool)
    assert match() is grant
    assert matching_grant(process, tool.name, {"value": 2}, per_invocation=True, tool=tool) is None
    tool.composite_spec["source_sha256"] = "replacement"
    assert match() is None
    tool.composite_spec["source_sha256"] = "first"
    tool.resources = ["process:other"]
    assert match() is None
    tool.resources = ["process:python"]
    process.process_id = "other-worker"
    assert match() is None
    process.process_id = "worker"
    grant["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert match() is None


def test_output_budget_terminates_capture_without_claiming_rollback():
    proc = subprocess.Popen([sys.executable, "-c", "print('x'*100000)"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        capture = BoundedCommunication(proc, limit=1000)
        with pytest.raises(SandboxDenied) as error:
            capture.communicate("", timeout=10)
        assert error.value.result()["effect_state"] == "ambiguous"
        assert len(capture.output[0]) <= 1000
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


@pytest.fixture(scope="module")
def isolated_python():
    """Copy only stdlib and interpreter into a disposable ACL-test runtime."""
    if sys.platform != "win32":
        pytest.skip("Windows AppContainer integration")
    with tempfile.TemporaryDirectory(prefix="gitgo-native-runtime-") as raw:
        root = Path(raw)
        base = Path(sys.base_prefix)
        shutil.copy2(base / "python.exe", root / "python.exe")
        for source in base.glob("*.dll"):
            shutil.copy2(source, root / source.name)
        if (base / "DLLs").is_dir():
            shutil.copytree(base / "DLLs", root / "DLLs")
        for pattern in ("sqlite3.dll", "*ffi*.dll", "libcrypto*.dll", "libssl*.dll"):
            for source in (base / "Library" / "bin").glob(pattern):
                shutil.copy2(source, root / "DLLs" / source.name)
        archive = root / f"python{sys.version_info.major}{sys.version_info.minor}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for source in (base / "Lib").rglob("*.py"):
                relative = source.relative_to(base / "Lib")
                if relative.parts[0] not in {"site-packages", "test", "idlelib", "tkinter", "ensurepip"}:
                    bundle.write(source, relative.as_posix())
        (root / f"python{sys.version_info.major}{sys.version_info.minor}._pth").write_text(
            archive.name + "\nDLLs\n.\n", encoding="utf-8")
        yield root


@pytest.fixture
def native_box(isolated_python, tmp_path_factory):
    from backend.core.sandbox_windows import WindowsApi
    from ctypes import wintypes as W
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    policy = SandboxPolicy(workspace)
    api = WindowsApi()
    sid = api.profile_sid(policy.profile_name)
    value = W.LPWSTR()
    convert = api.advapi.ConvertSidToStringSidW
    convert.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    convert.restype = W.BOOL
    api.check(convert(sid, C.byref(value)))
    sid_string = value.value
    free = api.kernel.LocalFree
    free.argtypes = [C.c_void_p]
    free.restype = C.c_void_p
    free(C.cast(value, C.c_void_p))
    api.free_sid(sid)
    for path, access in ((isolated_python, "RX"), (workspace, "M")):
        subprocess.run(["icacls", str(path), "/grant", f"*{sid_string}:(OI)(CI){access}"],
                       check=True, capture_output=True)
    subprocess.run(["icacls", str(workspace), "/setintegritylevel", "(OI)(CI)L"],
                   check=True, capture_output=True)
    def spawn(source, *, override=None):
        return sandbox_popen([str(isolated_python / "python.exe"), "-I", "-X", "utf8", "-c", source],
                             override or policy, cwd=str(workspace),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             env=sandbox_environment(os.environ))
    try:
        yield workspace, policy, spawn
    finally:
        subprocess.run(["icacls", str(isolated_python), "/remove:g", f"*{sid_string}"],
                       check=True, capture_output=True)
        delete = api.userenv.DeleteAppContainerProfile
        delete.argtypes = [W.LPCWSTR]
        delete.restype = C.c_long
        delete(policy.profile_name)


def finished(proc, timeout=10):
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    finally:
        terminate_tree(proc, getattr(proc, "_gitgo_job_handle", None))
        proc._gitgo_job_handle = None


def test_windows_filesystem_escape_and_workspace_write(native_box):
    workspace, policy, spawn = native_box
    secret = workspace.parent / "private.txt"
    secret.write_text("private")
    # Positive control proves the child really ran, rather than loader denial.
    code, out, err = finished(spawn(
        "from pathlib import Path;Path('allowed.txt').write_text('ok');print('ran')"))
    assert code == 0, err
    assert out.strip() == "ran"
    assert (workspace / "allowed.txt").read_text() == "ok"
    for action in (f"print(open({str(secret)!r}).read())",
                   f"open({str(secret)!r},'w').write('escaped')",
                   "print(open('../private.txt').read())"):
        code, out, err = finished(spawn(action))
        assert code != 0
        assert "PermissionError" in err
        assert "private" not in out
    assert secret.read_text() == "private"


def test_windows_junction_cannot_bypass_acl(native_box):
    workspace, policy, spawn = native_box
    outside = workspace.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J",
                    str(workspace / "link"), str(outside)], check=True, capture_output=True)
    code, out, err = finished(spawn("print(open('link/secret.txt').read())"))
    assert code != 0
    assert "PermissionError" in err
    assert "secret" not in out


def test_windows_network_loopback_is_denied(native_box):
    workspace, policy, spawn = native_box
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        # Positive control: this endpoint is reachable outside AppContainer.
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            peer, _ = listener.accept()
            peer.close()
        code, out, err = finished(spawn(
            f"import socket;s=socket.socket();s.settimeout(1);s.connect(('127.0.0.1',{port}));print('escaped')"))
        assert code != 0
        listener.settimeout(0.1)
        with pytest.raises(socket.timeout):
            listener.accept()
    assert "PermissionError" in err or "WinError 10013" in err or "TimeoutError" in err
    assert "escaped" not in out


def test_windows_memory_limit(native_box):
    workspace, policy, spawn = native_box
    limited = SandboxPolicy(workspace, memory_bytes=128*1024*1024)
    code, out, err = finished(spawn("x=bytearray(256*1024*1024);print('escaped')", override=limited))
    assert code != 0
    assert "escaped" not in out


def test_windows_process_limit_and_breakaway(native_box):
    workspace, policy, spawn = native_box
    limited = SandboxPolicy(workspace, process_limit=1)
    code, out, err = finished(spawn(
        "import subprocess,sys;subprocess.Popen([sys.executable,'-c','print(1)']);print('escaped')",
        override=limited))
    assert code != 0
    assert "escaped" not in out
    code, out, err = finished(spawn(
        "import subprocess,sys;"
        "subprocess.Popen([sys.executable,'-c','print(1)'],creationflags=0x01000000);print('escaped')"))
    assert code != 0
    assert "escaped" not in out


def test_windows_owner_close_kills_grandchild(native_box):
    workspace, policy, spawn = native_box
    marker = workspace / "survived.txt"
    # Child and grandchild run in a writable scope; ACL denial cannot make this pass.
    grandchild = "import time;time.sleep(1.5);open('survived.txt','w').write('escaped')"
    source = (
        "import subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]);"
        "print('ready',flush=True);time.sleep(60)"
    )
    proc = spawn(source)
    try:
        assert proc.stdout.readline().strip() == "ready"
        close_job(proc._gitgo_job_handle)
        proc._gitgo_job_handle = None
        proc.wait(timeout=5)
        time.sleep(1.8)
        assert not marker.exists()
    finally:
        terminate_tree(proc, proc._gitgo_job_handle)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()

def test_windows_real_runner_exec_and_privileged_tool(native_box, isolated_python, monkeypatch):
    """Exercise JSON protocol and handlers inside the real native boundary."""
    import hashlib
    from backend.core.loop.process_tool_runner import ProcessToolRunner
    source = isolated_python / "source"
    if not source.exists():
        shutil.copytree(Path(__file__).resolve().parents[1] / "backend", source / "backend",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(Path(sys.prefix) / "Lib" / "site-packages",
                        isolated_python / "packages",
                        ignore=shutil.ignore_patterns("__pycache__", "*.dist-info"))
    workspace, policy, spawn = native_box
    bootstrap = (
        f"import sys;sys.path.extend({[str(source),str(isolated_python / 'packages')]!r});"
        "from backend.core.tools.runner import main;main()"
    )
    monkeypatch.setattr("backend.core.loop.process_tool_runner.tool_runner_command",
                        lambda: [str(isolated_python / "python.exe"), "-X", "utf8", "-c", bootstrap])
    monkeypatch.setattr("backend.core.loop.process_tool_runner.owned_child_cwd", lambda _: source)
    # Cold PowerShell/module initialization on hosted Windows can exceed 10s.
    # Keep a finite acceptance budget without changing production limits.
    runner = ProcessToolRunner(timeout=90)
    result = runner.run("exec_command", {
        "_workspace": str(workspace),
        "argv": ["python", "-c", "open('runner.txt','w').write('ok');print('executed')"],
    })
    assert result.success, result
    assert result.data.get("success"), result
    assert result.data["stdout"].strip() == "executed"
    assert (workspace / "runner.txt").read_text() == "ok"
    shell = runner.run("shell_script", {
        "_workspace": str(workspace),
        "script": "Set-Content -LiteralPath shell.txt -Value native-file;Write-Output native-shell",
        "purpose": "verify native PowerShell execution", "timeout": 60,
    })
    assert shell.success, shell
    if not shell.data.get("success"):
        diagnostic = runner.run("shell_script", {
            "_workspace": str(workspace), "timeout": 60,
            "purpose": "diagnose native PowerShell module initialization",
            "script": ('[Console]::WriteLine($env:PSModulePath);'
                       '[Console]::WriteLine($PSHOME);'
                       'Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop;'
                       'Write-Output module-loaded'),
        })
        pytest.fail(json.dumps({"shell": shell.data, "diagnostic": diagnostic.data}))
    assert shell.data["stdout"].strip() == "native-shell"
    assert not shell.data["stderr"], json.dumps(shell.data)
    assert (workspace / "shell.txt").read_text().strip() == "native-file"
    authored = "def run(args):\n    return {'native': args['value']}\n"
    result = runner.run("authored_privileged_python", {
        "_workspace": str(workspace), "value": 7,
        "_dynamic_spec": {"_source": authored, "name": "native_echo",
                          "source_sha256": hashlib.sha256(authored.encode()).hexdigest()},
    })
    assert result.success, result
    assert result.data == {"native": 7}, result

@pytest.fixture
def linux_box(tmp_path_factory):
    if sys.platform != "linux":
        pytest.skip("Linux bubblewrap integration")
    if not shutil.which("bwrap"):
        pytest.fail("bubblewrap must be installed for Linux sandbox acceptance")
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    policy = SandboxPolicy(workspace)
    def spawn(source):
        return sandbox_popen([sys.executable, "-I", "-c", source], policy,
                             cwd=str(workspace), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", start_new_session=True,
                             env=sandbox_environment(os.environ))
    return workspace, spawn


def test_linux_filesystem_symlink_and_memory_limits(linux_box):
    workspace, spawn = linux_box
    secret = workspace.parent / "private.txt"
    secret.write_text("private")
    code, out, err = finished(spawn("open('allowed.txt','w').write('ok');print('ran')"))
    assert code == 0, err
    assert out.strip() == "ran"
    (workspace / "escape").symlink_to(secret)
    for target in (str(secret), "escape", "../private.txt"):
        code, out, err = finished(spawn(f"print(open({target!r}).read())"))
        assert code != 0
        assert "private" not in out
    code, out, err = finished(spawn("print('ran',flush=True);data=bytearray(1024**3)"))
    assert code != 0
    assert out.strip() == "ran"
    assert "MemoryError" in err


def test_linux_network_namespace_is_private(linux_box):
    workspace, spawn = linux_box
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            peer, _ = listener.accept()
            peer.close()
        code, out, err = finished(spawn(
            f"import socket;print('ran',flush=True);socket.create_connection(('127.0.0.1',{port}),timeout=1);print('escaped')"))
        listener.settimeout(0.1)
        with pytest.raises(socket.timeout):
            listener.accept()
    assert code != 0
    assert out.strip() == "ran"
    assert "escaped" not in out


def test_linux_cancel_kills_detached_descendant(linux_box):
    workspace, spawn = linux_box
    marker = workspace / "survived.txt"
    child = "import os,time;os.setsid();time.sleep(1.5);open('survived.txt','w').write('escaped')"
    proc = spawn("import subprocess,sys,time;"
                 f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
                 "print('ready',flush=True);time.sleep(60)")
    try:
        assert proc.stdout.readline().strip() == "ready"
        terminate_tree(proc)
        time.sleep(1.8)
        assert not marker.exists()
    finally:
        terminate_tree(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()

def test_external_resource_grant_cannot_survive_tool_replacement(tmp_path_factory):
    from backend.core.loop.agent_tool import AgentTool
    from backend.core.loop.permission_broker import (
        arguments_digest, authorize_external_resources, tool_contract_digest,
    )
    target = (tmp_path_factory / "outside").resolve(strict=False)
    target.mkdir()
    tool = AgentTool("write_external", "test", {}, lambda args: {}, resources=[str(target)])
    grant = {
        "tool_name": tool.name, "effect": "read", "task_id": "task", "process_id": "worker",
        "resource": str(target), "scope": "task", "remaining_uses": None,
        "tool_contract_digest": tool_contract_digest(tool),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        "arguments_digest": arguments_digest({}),
    }
    process = SimpleNamespace(active_task_id="task", process_id="worker", approval_grants=[grant])
    allowed, error = authorize_external_resources(process, tool.name, "read", {}, [str(target)], tool=tool)
    assert error is None
    assert allowed == [str(target)]
    tool.parameters = {"type": "object", "properties": {"replacement": {"type": "boolean"}}}
    allowed, error = authorize_external_resources(process, tool.name, "read", {}, [str(target)], tool=tool)
    assert allowed == []
    assert error is not None

def test_missing_workspace_is_a_stable_policy_denial(tmp_path_factory):
    with pytest.raises(SandboxDenied) as error:
        SandboxPolicy(tmp_path_factory / "missing")
    assert error.value.code == "SANDBOX_POLICY_INVALID"


def test_model_cannot_inject_host_resource_roots(tmp_path_factory, monkeypatch):
    from backend.core.loop.event_bus import EventBus
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry
    from backend.core.tools.catalog import build_workspace_tools
    workspace = tmp_path_factory / "project"
    workspace.mkdir()
    (tmp_path_factory / "private.txt").write_text("private")
    # This admission test does not exercise history/storage. Keep the SQLite
    # safety guard intact while avoiding an unrelated persistence side effect.
    monkeypatch.setattr("backend.core.history.HistoryManager.add_operation",
                        lambda *args, **kwargs: None)
    process = AgentProcessManager().fork(
        parent_id=None, role="worker", tool_registry=ToolRegistry(["read_file"]),
        max_steps=2, ring_level=RingLevel.RING_3, actor_kind="worker",
        workspace_path=str(workspace), task_id="private-input",
    )
    tool = build_workspace_tools(workspace)["read_file"]
    result = ToolPipeline().execute({
        "name": "read_file",
        "args": {"path": "../private.txt", "_workspace": str(tmp_path_factory),
                 "_allowed_roots": [str(tmp_path_factory)]},
    }, tool, ExecutionContext(process=process, session=process.session,
                              workspace_path=str(workspace), event_bus=EventBus(),
                              cancellation=process.cancellation_event), "escape", 0)
    assert result.is_error
    assert not result.data or result.data.get("content") != "private"

def test_exact_grant_can_be_consumed_by_only_one_thread():
    from backend.core.loop.agent_tool import AgentTool
    from backend.core.loop.permission_broker import arguments_digest, matching_grant, tool_contract_digest
    tool = AgentTool("sensitive", "test", {}, lambda args: {}, approval_per_invocation=True)
    grant = {"tool_name": tool.name, "task_id": "task", "process_id": "worker",
             "arguments_digest": arguments_digest({}), "remaining_uses": 1,
             "tool_contract_digest": tool_contract_digest(tool),
             "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
    process = SimpleNamespace(active_task_id="task", process_id="worker", approval_grants=[grant])
    barrier = threading.Barrier(8)
    results = []
    def consume():
        barrier.wait()
        results.append(matching_grant(process, tool.name, {}, per_invocation=True,
                                      consume=True, tool=tool))
    workers = [threading.Thread(target=consume) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert sum(result is not None for result in results) == 1
    assert grant["remaining_uses"] == 0


def test_pipeline_rechecks_consumption_before_execution(tmp_path_factory, monkeypatch):
    from backend.core.loop.agent_tool import AgentTool, ApprovalMode
    from backend.core.loop.event_bus import EventBus
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry
    # Test only approval admission; persistence has separate runtime tests.
    monkeypatch.setattr("backend.core.history.HistoryManager.add_operation",
                        lambda *args, **kwargs: None)
    process = AgentProcessManager().fork(
        parent_id=None, role="worker", tool_registry=ToolRegistry(["sensitive"]),
        max_steps=2, ring_level=RingLevel.RING_3, actor_kind="worker",
        workspace_path=str(tmp_path_factory), task_id="grant-race")
    calls = []
    tool = AgentTool("sensitive", "test", {}, lambda args: calls.append(args),
                     approval=ApprovalMode.ASK, approval_per_invocation=True)
    def match(*args, **kwargs):
        return None if kwargs.get("consume") else {"grant_id": "already-consumed"}
    monkeypatch.setattr("backend.core.loop.permission_broker.matching_grant", match)
    result = ToolPipeline().execute({"name": tool.name, "args": {}}, tool,
        ExecutionContext(process=process, session=process.session,
                         workspace_path=str(tmp_path_factory), event_bus=EventBus(),
                         cancellation=process.cancellation_event), "race", 0)
    assert result.is_error
    assert result.diagnostics["code"] == "APPROVAL_GRANT_INVALID"
    assert calls == []

def test_native_child_home_and_caches_stay_inside_workspace(tmp_path_factory, monkeypatch):
    from backend.core.sandbox import prepare_child_environment
    for key in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                "TMPDIR", "PSModuleAnalysisCachePath"):
        # Restore the process environment after exercising the child bootstrap.
        monkeypatch.setenv(key, os.environ.get(key, ""))
    prepare_child_environment(str(SandboxPolicy(tmp_path_factory).workspace))
    for key in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                "TMPDIR", "PSModuleAnalysisCachePath"):
        assert Path(os.environ[key]).resolve().is_relative_to(tmp_path_factory.resolve())
