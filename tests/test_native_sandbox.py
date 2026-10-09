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
    runtime_paths = [isolated_python]
    packaged = os.environ.get("GITGO_SANDBOX_TEST_HOST")
    if packaged:
        runtime_paths.append(Path(packaged).resolve(strict=True).parent)
    for path, access in [*((path, "RX") for path in runtime_paths), (workspace, "M")]:
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
        for runtime in runtime_paths:
            subprocess.run(["icacls", str(runtime), "/remove:g", f"*{sid_string}"],
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


def test_linux_process_limit_is_per_invocation(linux_box):
    workspace, _ = linux_box
    limited = SandboxPolicy(workspace, process_limit=1)
    proc = sandbox_popen([sys.executable, "-I", "-c",
        "import subprocess,sys;print('ran',flush=True);"
        "subprocess.Popen([sys.executable,'-c','print(1)']);print('escaped')"],
        limited, cwd=str(workspace), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=sandbox_environment(os.environ),
        start_new_session=True)
    code, out, err = finished(proc)
    assert code != 0, (out, err)
    assert out.strip() == "ran"
    assert "escaped" not in out


def test_linux_invocation_memory_cannot_be_split_across_children(linux_box):
    workspace, _ = linux_box
    # Each child stays below RLIMIT_AS; together they exceed memory.max.
    child = "import time;x=bytearray(80*1024*1024);time.sleep(10)"
    source = ("import subprocess,sys,time;print('ran',flush=True);"
              f"children=[subprocess.Popen([sys.executable,'-c',{child!r}]) for _ in range(3)];"
              "[p.wait() for p in children];print('escaped')")
    proc = sandbox_popen([sys.executable, "-I", "-c", source],
        SandboxPolicy(workspace, memory_bytes=192*1024*1024), cwd=str(workspace),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=sandbox_environment(os.environ), start_new_session=True)
    code, out, err = finished(proc, timeout=20)
    assert code != 0, (out, err)
    assert out.strip() == "ran"
    assert "escaped" not in out


@pytest.fixture
def cross_platform_box(request):
    if sys.platform == "win32":
        workspace, policy, spawn = request.getfixturevalue("native_box")
        executable = request.getfixturevalue("isolated_python") / "python.exe"
        return workspace, policy, str(executable)
    if sys.platform == "linux":
        workspace, _ = request.getfixturevalue("linux_box")
        return workspace, SandboxPolicy(workspace), sys.executable
    pytest.skip("Native process lifecycle acceptance requires a supported backend")


def test_host_crash_kills_detached_descendants(cross_platform_box):
    workspace, policy, executable = cross_platform_box
    child = "import time;open('grandchild-ready.txt','w').write('ok');time.sleep(2);open('orphan.txt','w').write('escaped')"
    detach = "creationflags=0x00000200" if sys.platform == "win32" else "start_new_session=True"
    tool = ("import subprocess,time;from pathlib import Path;"
            f"subprocess.Popen([{executable!r},'-c',{child!r}],{detach});"
            "exec(\"while not Path('grandchild-ready.txt').exists(): time.sleep(0.01)\");"
            "open('started.txt','w').write('started');print('ready',flush=True);time.sleep(60)")
    host_code = (
        "import os,subprocess;from pathlib import Path;"
        "from backend.core.sandbox import SandboxPolicy,sandbox_popen,sandbox_environment;"
        f"p=sandbox_popen([{executable!r},'-I','-c',{tool!r}],SandboxPolicy(Path({str(workspace)!r})),"
        f"cwd={str(policy.workspace)!r},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,"
        "text=True,env=sandbox_environment(os.environ),start_new_session=os.name!='nt');"
        "print(p.stdout.readline().strip(),flush=True);p.wait()"
    )
    host = subprocess.Popen([sys.executable, '-B', '-c', host_code],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # Bounded read: a broken loader must fail, not hang the entire suite.
        ready = []
        reader = threading.Thread(target=lambda: ready.append(host.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout=15)
        assert ready and ready[0].strip() == 'ready', ready
        assert (workspace / 'started.txt').read_text() == 'started'
        # Kill only the Host, with no cooperative sandbox/job cleanup.
        host.kill()
        host.wait(timeout=5)
        time.sleep(2.4)
        assert not (workspace / 'orphan.txt').exists()
    finally:
        if host.poll() is None:
            host.kill()
            host.wait(timeout=5)
        for stream in (host.stdout, host.stderr):
            stream.close()


def test_packaged_host_executes_inside_native_boundary(cross_platform_box):
    workspace, policy, _ = cross_platform_box
    configured = os.environ.get('GITGO_SANDBOX_TEST_HOST')
    if not configured:
        pytest.skip('Source job; packaged acceptance runs with an actual built Host')
    host = Path(configured).resolve(strict=True)
    # This driver runs INSIDE the real frozen executable. Patching sys.frozen
    # in a source interpreter would not exercise bootloader/child-role behavior.
    invocation = {"_workspace": str(policy.workspace), "argv": ["python", "-c",
        "open('packaged.txt','w').write('ok');print('packaged')"]}
    source = (
        "import json;from backend.core.loop.process_tool_runner import ProcessToolRunner;"
        f"r=ProcessToolRunner(timeout=30).run('exec_command',{invocation!r});"
        "print(json.dumps({'success':r.success,'data':r.data,'error':r.error,'stderr':r.stderr}))"
    )
    result = subprocess.run([str(host), '--gitgo-internal-role', 'python', '-c', source],
        capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['success'], payload
    assert payload['data'].get('success'), payload
    assert payload['data']['stdout'].strip() == 'packaged'
    assert (workspace / 'packaged.txt').read_text() == 'ok'


def test_linux_cpu_budget_counts_all_descendants(linux_box):
    workspace, _ = linux_box
    child = ("import sys,time;open(sys.argv[1],'w').write('ready');t=time.process_time();"
             "exec('while time.process_time()-t<0.7: pass');time.sleep(60)")
    source = ("import subprocess,sys,time;print('ran',flush=True);"
              f"[subprocess.Popen([sys.executable,'-c',{child!r},'cpu-'+str(i)]) for i in range(2)];"
              "time.sleep(60)")
    # Neither child reaches its inherited per-process one-second rlimit. Only
    # the aggregate budget can stop the sleeping root before the timeout.
    proc = sandbox_popen([sys.executable, '-I', '-c', source],
        SandboxPolicy(workspace, cpu_seconds=1), cwd=str(workspace),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=sandbox_environment(os.environ), start_new_session=True)
    code, out, err = finished(proc, timeout=10)
    assert code != 0, (out, err)
    assert out.strip() == 'ran'
    assert all((workspace / f'cpu-{i}').read_text() == 'ready' for i in range(2))


@pytest.mark.parametrize("scenario", ["outside_read", "network"])
def test_packaged_host_denies_external_access(cross_platform_box, scenario):
    workspace, policy, _ = cross_platform_box
    configured = os.environ.get('GITGO_SANDBOX_TEST_HOST')
    if not configured:
        pytest.skip('Source job; packaged acceptance requires an actual built Host')
    host = Path(configured).resolve(strict=True)
    # Establish the forbidden operation succeeds outside the boundary first.
    listener = None
    try:
        if scenario == 'outside_read':
            outside = workspace.parent / 'packaged-host-only.txt'
            outside.write_text('host-secret')
            assert outside.read_text() == 'host-secret'
            attempt = f"open({str(outside)!r}).read()"
        else:
            import socket
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen(2)
            address = listener.getsockname()
            with socket.create_connection(address, timeout=2):
                pass
            attempt = f"__import__('socket').create_connection({address!r},timeout=2)"
        script = ("try:\n " + attempt + "\nexcept OSError:\n print('blocked')"
                  "\nelse:\n print('escaped')")
        invocation = {"_workspace": str(policy.workspace), "argv": ["python", "-c", script]}
        source = (
            "import json;from backend.core.loop.process_tool_runner import ProcessToolRunner;"
            f"r=ProcessToolRunner(timeout=30).run('exec_command',{invocation!r});"
            "print(json.dumps({'success':r.success,'data':r.data,'error':r.error,'stderr':r.stderr}))"
        )
        result = subprocess.run([str(host), '--gitgo-internal-role', 'python', '-c', source],
            capture_output=True, text=True, encoding='utf-8', timeout=60)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload['success'] and payload['data'].get('success'), payload
        assert payload['data']['stdout'].strip() == 'blocked', payload
    finally:
        if listener is not None:
            listener.close()


@pytest.mark.parametrize("reason,input_size", [("cancel", 0), ("cancel", 4_000_000), ("timeout", 0)])
def test_native_runner_stops_detached_children_with_blocked_io(
        cross_platform_box, monkeypatch, reason, input_size):
    from backend.core.loop.process_tool_runner import ProcessToolRunner
    workspace, policy, executable = cross_platform_box
    child = ("import time;open('child-ready.txt','w').write('ok');"
             "time.sleep(8);open('after-stop.txt','w').write('escaped')")
    detach = "creationflags=0x00000200" if sys.platform == 'win32' else "start_new_session=True"
    source = ("import subprocess,time;from pathlib import Path;"
              f"subprocess.Popen([{executable!r},'-c',{child!r}],{detach});"
              "time.sleep(60)")
    # Substitute only the payload command; OS launch, pipe handling, timeouts
    # and cleanup remain the real production implementation.
    monkeypatch.setattr('backend.core.loop.process_tool_runner.tool_runner_command',
                        lambda: [executable, '-I', '-c', source])
    monkeypatch.setattr('backend.core.loop.process_tool_runner.owned_child_cwd',
                        lambda _: workspace)
    cancellation = threading.Event()
    results = []
    worker = threading.Thread(target=lambda: results.append(ProcessToolRunner(timeout=5).run(
        'exec_command', {'_workspace': str(policy.workspace), 'payload': 'x' * input_size},
        cancellation_event=cancellation)), daemon=True)
    worker.start()
    deadline = time.monotonic() + 4
    try:
        while not (workspace / 'child-ready.txt').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (workspace / 'child-ready.txt').exists(), results
        ready = time.monotonic()
        if reason == 'cancel':
            cancellation.set()
        worker.join(timeout=7)
        assert not worker.is_alive(), 'Stopping a sandbox must also unblock stdin/stdout writers'
        assert len(results) == 1 and not results[0].success, results
        assert results[0].timed_out == (reason == 'timeout'), results
        assert results[0].effect_state == 'ambiguous'
        if reason == 'cancel':
            assert 'cancelled' in results[0].error
        time.sleep(max(0, ready + 8.3 - time.monotonic()))
        assert not (workspace / 'after-stop.txt').exists()
    finally:
        cancellation.set()
        worker.join(timeout=10)


@pytest.mark.parametrize('stream', [1, 2])
def test_native_runner_enforces_output_budget_and_unknown_effects(
        cross_platform_box, monkeypatch, stream):
    from backend.core.loop.process_tool_runner import ProcessToolRunner
    workspace, policy, executable = cross_platform_box
    source = ("import os,time;open('executed.txt','w').write('ok');"
              f"[os.write({stream},b'x'*65536) for _ in range(40)];time.sleep(60)")
    monkeypatch.setattr('backend.core.loop.process_tool_runner.tool_runner_command',
                        lambda: [executable, '-I', '-c', source])
    monkeypatch.setattr('backend.core.loop.process_tool_runner.owned_child_cwd',
                        lambda _: workspace)
    result = ProcessToolRunner(timeout=10).run('exec_command', {'_workspace': str(policy.workspace)})
    assert result.success and result.data['error'] == 'SANDBOX_OUTPUT_LIMIT', result
    assert result.data['effect_state'] == 'ambiguous'
    assert (workspace / 'executed.txt').read_text() == 'ok'


def test_cancel_between_pipeline_admission_and_spawn_never_executes(tmp_path_factory, monkeypatch):
    from backend.core.loop.agent_tool import AgentTool, ToolEffect
    from backend.core.loop.event_bus import EventBus
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry
    monkeypatch.setattr('backend.core.history.HistoryManager.add_operation', lambda *a, **k: None)
    def forbidden_launch(*args, **kwargs):
        pytest.fail('A cancellation observed before launch must never start executable code')
    monkeypatch.setattr('backend.core.sandbox.sandbox_popen', forbidden_launch)
    process = AgentProcessManager().fork(
        parent_id=None, role='worker', tool_registry=ToolRegistry(['command']), max_steps=2,
        ring_level=RingLevel.RING_3, workspace_path=str(tmp_path_factory), task_id='pre-cancel')
    bus = EventBus()
    bus.subscribe('ToolExecuteStarted', lambda _: process.cancellation_event.set())
    tool = AgentTool('command', 'test', {}, lambda _: pytest.fail('Inline execution'),
                     isolated=True, runner_name='exec_command', effect=ToolEffect.PROCESS,
                     read_only=False)
    ctx = ExecutionContext(process=process, session=process.session,
        workspace_path=str(tmp_path_factory), event_bus=bus, cancellation=process.cancellation_event)
    result = ToolPipeline().execute({'name': 'command', 'args': {'argv': ['python', '-c', 'print(1)']}},
                                    tool, ctx, 'pre-cancel', 0)
    assert result.is_error and result.diagnostics['code'] == 'TOOL_CANCELLED', result
    assert result.receipt['effect_state'] == 'not_committed'
    journal = json.loads(Path(result.receipt['invocation_path']).read_text(encoding='utf-8'))
    assert journal['state'] == 'cancelled' and journal['effect_state'] == 'not_committed'


def test_windows_cpu_budget_counts_descendants(native_box):
    workspace, _, spawn = native_box
    child = ("import sys,time;open(sys.argv[1],'w').write('ready');t=time.process_time();"
             "exec('while time.process_time()-t<0.7: pass');time.sleep(60)")
    source = ("import subprocess,sys,time;print('ran',flush=True);"
              f"[subprocess.Popen([sys.executable,'-c',{child!r},'cpu-'+str(i)]) for i in range(2)];"
              "time.sleep(60)")
    from backend.core.sandbox_windows import CpuRateControl, WindowsApi
    proc = spawn(source, override=SandboxPolicy(workspace, cpu_seconds=1))
    try:
        api = WindowsApi()
        rate = CpuRateControl()
        # Read the actual kernel policy; a successful launch alone does not
        # establish that the invocation has a hard aggregate bandwidth cap.
        api.check(api.query_job(proc._gitgo_job_handle, 15, C.byref(rate), C.sizeof(rate), None))
        assert rate.flags & 0x5 == 0x5  # ENABLE | HARD_CAP
        assert 0 < rate.rate <= 10000 // api.active_cpu_count(0xFFFF)
    finally:
        code, out, err = finished(proc, timeout=12)
    assert code != 0, (out, err)
    assert out.strip() == 'ran'
    assert all((workspace / f'cpu-{i}').read_text() == 'ready' for i in range(2))


def test_failed_resource_monitor_kills_started_tool(cross_platform_box, monkeypatch):
    workspace, policy, executable = cross_platform_box
    source = ("import time;open('before-monitor-failure','w').write('ran');"
              "time.sleep(1);open('after-monitor-failure','w').write('escaped')")
    def fail_start(_):
        deadline = time.monotonic() + 3
        while not (workspace / 'before-monitor-failure').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (workspace / 'before-monitor-failure').read_text() == 'ran'
        raise RuntimeError('Host thread resource unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, 'start', fail_start)
        with pytest.raises(SandboxDenied) as error:
            sandbox_popen([executable, '-I', '-c', source], policy, cwd=str(workspace),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=sandbox_environment(os.environ), start_new_session=sys.platform != 'win32')
    assert error.value.result()['effect_state'] == 'ambiguous'
    time.sleep(1.2)
    assert not (workspace / 'after-monitor-failure').exists()


def test_cpu_violation_cannot_be_hidden_by_success_json(cross_platform_box, monkeypatch):
    from backend.core.loop.process_tool_runner import ProcessToolRunner
    workspace, policy, executable = cross_platform_box
    child = ("import sys,time;open(sys.argv[1],'w').write('ready');t=time.process_time();"
             "exec('while time.process_time()-t<0.7: pass');time.sleep(60)")
    source = ("import subprocess,sys,time;print('{\"success\":true,\"data\":{\"ok\":true}}',flush=True);"
              f"[subprocess.Popen([sys.executable,'-c',{child!r},'cpu-'+str(i)]) for i in range(2)];"
              "time.sleep(60)")
    # Keep wall timeout above the CPU budget so only aggregate accounting can
    # stop the sleeping root; run the real OS launch and communication paths.
    monkeypatch.setattr('backend.core.sandbox.SandboxPolicy',
                        lambda workspace, **_: SandboxPolicy(workspace, cpu_seconds=1))
    monkeypatch.setattr('backend.core.loop.process_tool_runner.tool_runner_command',
                        lambda: [executable, '-I', '-c', source])
    monkeypatch.setattr('backend.core.loop.process_tool_runner.owned_child_cwd', lambda _: workspace)
    result = ProcessToolRunner(timeout=10).run('exec_command', {'_workspace': str(policy.workspace)})
    assert result.success and result.data['error'] == 'SANDBOX_EXECUTION_FAILED', result
    assert result.data['effect_state'] == 'ambiguous'
    assert 'CPU' in result.data['message']
    assert all((workspace / f'cpu-{i}').read_text() == 'ready' for i in range(2))


@pytest.mark.parametrize("relation", ["same", "workspace_parent", "runtime_parent"])
def test_workspace_cannot_overlap_host_runtime(tmp_path_factory, monkeypatch, relation):
    runtime = tmp_path_factory / 'runtime'
    runtime.mkdir()
    nested = runtime / 'nested'
    nested.mkdir()
    if relation == 'same':
        workspace = runtime
    elif relation == 'workspace_parent':
        workspace, runtime = runtime, nested
    else:
        workspace = nested
    monkeypatch.setattr('backend.core.sandbox.trusted_runtime_roots', lambda: (runtime,))
    with pytest.raises(SandboxDenied) as denied:
        SandboxPolicy(workspace)
    assert denied.value.code == 'SANDBOX_POLICY_INVALID'
    assert denied.value.result()['effect_state'] == 'not_committed'


def test_runtime_alias_cannot_hide_workspace_overlap(tmp_path_factory):
    from backend.core.sandbox import validate_runtime_separation
    runtime = tmp_path_factory / 'runtime'
    runtime.mkdir()
    workspace = runtime / 'project'
    workspace.mkdir()
    alias = tmp_path_factory / 'alias'
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(runtime)],
                       check=True, capture_output=True)
    else:
        alias.symlink_to(runtime, target_is_directory=True)
    try:
        with pytest.raises(SandboxDenied):
            validate_runtime_separation(workspace, [alias])
    finally:
        if os.name == 'nt':
            alias.rmdir()
        else:
            alias.unlink()


def test_disjoint_runtime_directory_is_allowed(tmp_path_factory):
    from backend.core.sandbox import validate_runtime_separation
    runtime, workspace = tmp_path_factory / 'runtime', tmp_path_factory / 'runtime-project'
    runtime.mkdir()
    workspace.mkdir()
    validate_runtime_separation(workspace, [runtime])


def test_linux_workspace_cannot_select_fake_bwrap(linux_box, monkeypatch):
    workspace, spawn = linux_box
    fake_dir = workspace / 'bin'
    fake_dir.mkdir()
    helper = fake_dir / 'bwrap'
    helper.write_text('#!/bin/sh\necho escaped > "' + str(workspace / 'fake-helper-ran') + '"\n')
    helper.chmod(0o755)
    monkeypatch.setenv('PATH', str(fake_dir) + os.pathsep + os.environ['PATH'])
    assert shutil.which('bwrap') == str(helper)  # Real positive control for PATH spoofing.
    code, out, err = finished(spawn("open('positive-control','w').write('ok');print('isolated')"))
    assert code == 0, err
    assert out.strip() == 'isolated'
    assert (workspace / 'positive-control').read_text() == 'ok'
    assert not (workspace / 'fake-helper-ran').exists()


def test_native_tool_cannot_rewrite_host_invocation_journal(cross_platform_box):
    from backend.core.storage.invocation_journal import invocation_journal_root
    workspace, policy, executable = cross_platform_box
    host_state = workspace.parent / 'host-owned-state'
    host_state.mkdir()
    journal_dir = invocation_journal_root(workspace, paths=SimpleNamespace(project_root=host_state))
    journal_dir.mkdir(parents=True)
    journal = journal_dir / 'invocation.json'
    journal.write_text('{"state":"running"}', encoding='utf-8')
    source = ("open('positive-control','w').write('ok')\ntry:\n "
              f"open({str(journal)!r},'w').write('forged')"
              "\nexcept OSError:\n print('blocked')\nelse:\n print('escaped')")
    proc = sandbox_popen([executable, '-I', '-c', source], policy, cwd=str(workspace),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=sandbox_environment(os.environ), start_new_session=sys.platform != 'win32')
    code, out, err = finished(proc)
    assert code == 0, err
    assert (workspace / 'positive-control').read_text() == 'ok'
    assert out.strip() == 'blocked'
    assert journal.read_text() == '{"state":"running"}'


def test_root_exit_does_not_leave_a_pipe_holding_descendant(cross_platform_box, monkeypatch):
    from backend.core.loop.process_tool_runner import ProcessToolRunner
    workspace, _, executable = cross_platform_box
    child = ("import time;open('descendant-ready','w').write('ran');"
             "time.sleep(2);open('after-root-exit','w').write('escaped');time.sleep(60)")
    source = ("import os,subprocess,time;from pathlib import Path;"
              f"subprocess.Popen([{executable!r},'-I','-c',{child!r}]);"
              "exec('while not Path(\"descendant-ready\").exists(): time.sleep(0.01)');"
              "print('{\"success\":true,\"data\":{\"root\":\"exited\"}}',flush=True);os._exit(0)")
    monkeypatch.setattr('backend.core.loop.process_tool_runner.tool_runner_command',
                        lambda: [executable, '-I', '-c', source])
    monkeypatch.setattr('backend.core.loop.process_tool_runner.owned_child_cwd', lambda _: workspace)
    result = ProcessToolRunner(timeout=8).run('exec_command', {'_workspace': str(workspace)})
    assert result.success and result.data == {'root': 'exited'}, result
    assert result.exit_code == 0 and not result.timed_out, result
    assert result.duration_ms < 7000, result
    assert (workspace / 'descendant-ready').read_text() == 'ran'
    time.sleep(2.3)
    assert not (workspace / 'after-root-exit').exists(), result
