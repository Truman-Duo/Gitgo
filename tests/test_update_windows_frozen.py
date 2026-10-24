"""Two real Windows frozen bundles; injected signed stream, no public feed/UI.

The source coordinator invokes the real shared transaction; startup/recovery
use the physically installed immutable frozen bootstrap. Not visible-terminal
acceptance or a publisher Release. No paid provider and no daily installation.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest

from backend.core.update import handoff
from backend.core.update.tree import tree_digest
from backend.core.update.installation import open_managed
from backend.core.update.persistence import lock_file
from backend.core.update.validation import UpdateError
from tests.test_update_two_frozen_versions import actual_lab, finish_disposable_handoff
from tests.test_update_manifest import signing
from tests.test_update_transaction import Crash

pytestmark = pytest.mark.skipif(os.name != "nt" or not os.environ.get("GITGO_UPDATE_PREVIOUS_STAGE")
    or not os.environ.get("GITGO_UPDATE_CANDIDATE_STAGE"), reason="Requires two actual Windows frozen bundles")


def test_real_windows_upgrade_rollback_keep_program_and_user_data(actual_lab, monkeypatch):
    store, prepare, boot, before, old, new, _ = actual_lab
    service, selected = prepare()
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_apply("preview", selected, "c" * 32)
        result = handoff._consume_request(store, nonce, 75, finish_disposable_handoff())
        assert result.status == "committed", result
    boot(new)
    water = store._ledger()["watermarks"]
    selection = store.rollback_selection()
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_rollback(selection["rollback_id"], "d" * 32)
        result = handoff._consume_request(store, nonce, 75, finish_disposable_handoff())
        assert result.status == "rolled_back", result
    boot(old)
    assert tree_digest(store.root / "active") == before
    assert store._ledger()["watermarks"] == water


def test_broken_real_windows_dashboard_restores_bootable_predecessor(actual_lab, monkeypatch):
    store, prepare, boot, before, old, _, _ = actual_lab
    service, selected = prepare(broken=True)
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_apply("preview", selected, "c" * 32)
        result = handoff._consume_request(store, nonce, 75, finish_disposable_handoff())
        assert result.status == "rolled_back" and result.error_code == "UPDATE_HEALTH_FAILED", result
    boot(old)
    assert tree_digest(store.root / "active") == before


def test_frozen_windows_bootstrap_recovers_after_switch_interruption(actual_lab, monkeypatch):
    store, prepare, boot, before, old, _, env = actual_lab
    service, selected = prepare()
    saved = store._save
    def crash(ledger, state=None, **kwargs):
        result = saved(ledger, state, **kwargs)
        if state == "CHECK_NEW": raise Crash()
        return result
    monkeypatch.setattr(store, "_save", crash)
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_apply("preview", selected, "c" * 32)
        with pytest.raises(Crash): handoff._consume_request(store, nonce, 75, finish_disposable_handoff())
    host = store.root / "bootstrap/gitgo-host/gitgo-host.exe"
    result = subprocess.run([str(host), "--gitgo-internal-role", "managed-recover"], env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "rolled_back"
    boot(old)
    assert tree_digest(store.root / "active") == before


def test_real_windows_managed_launcher_and_live_lease(actual_lab):
    store, _, _, before, old, _, env = actual_lab
    # CreateProcess takes the cmd command line directly; list2cmdline's C-argv
    # escaping of nested quotes is not cmd.exe's /s /c quoting grammar.
    command = 'cmd.exe /d /s /c ""' + str(store.root / 'gitgo.cmd') + '" --version"'
    result = subprocess.run(command,
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Gitgo " + old
    with store.runtime_lease():
        assert open_managed(store.root).recover().status == "busy"
    assert tree_digest(store.root / "active") == before


def test_actual_active_windows_host_holds_admission_until_clean_exit(actual_lab):
    from scripts.smoke_packaged_runtime import ProtocolClient
    store, _, _, before, old, _, env = actual_lab
    client = ProtocolClient(store.root / "active/internal/gitgo-host/gitgo-host.exe", env)
    try:
        assert client.host_version == old
        assert open_managed(store.root).recover().status == "busy"
        assert client.call("product.version")["result"]["version"] == old
    finally: client.close()
    assert open_managed(store.root).recover().status == "idle"
    assert tree_digest(store.root / "active") == before


@pytest.mark.parametrize("shell", ["pwsh.exe", "powershell.exe"], ids=["ps7", "ps51"])
def test_actual_windows_installer_and_unicode_powershell_command(actual_lab, shell):
    from backend.core.process_control import spawn_managed, terminate_tree
    from backend.core.process_output import collect_output
    store, _, _, _, old, _, env = actual_lab
    base = store.root.parent
    root, bin_dir = base / "独立 安装", base / "独立 命令"
    seed = base / "seed"
    installer = spawn_managed([shell, "-NoProfile", "-File", str(seed / "install-managed.ps1"),
        "-Root", str(root), "-BinDir", str(bin_dir), "-ProtectedPath", str(base / "protected")],
        env=env, cwd=base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    output, errors, _ = collect_output(installer, timeout=180, limit=65536,
        terminate=lambda: terminate_tree(installer))
    assert installer._gitgo_cleanup["complete"], installer._gitgo_cleanup
    if installer.returncode != 0:
        # Inspect only bounded error codes/types from a separate fresh attempt;
        # never publish an exception message, user configuration or traceback.
        diagnostic = """import sys,json
from backend.core.update.installation import install_from_frozen_package
try:
 result=install_from_frozen_package(*json.loads(sys.argv[1]))
 print(json.dumps(result),flush=True)
except Exception as error:
 items=[]
 for _ in range(8):
  if error is None: break
  items.append({'type':type(error).__name__,'code':getattr(error,'code',None),'winerror':getattr(error,'winerror',None)})
  error=error.__cause__ or error.__context__
 print(json.dumps(items));sys.exit(2)
"""
        arguments = json.dumps([str(base / "诊断 安装"), str(base / "诊断 命令"), []])
        proc = spawn_managed([str(seed / "internal/gitgo-host/gitgo-host.exe"),
            "--gitgo-internal-role", "python", "-c", diagnostic, arguments], env=env, cwd=base,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        detail, diagnostic_errors, _ = collect_output(proc, timeout=180, limit=65536, terminate=lambda: terminate_tree(proc))
        role_proc = spawn_managed([str(seed / "internal/gitgo-host/gitgo-host.exe"),
            "--gitgo-internal-role", "managed-install", "--root", str(base / "role 安装"),
            "--bin-dir", str(base / "role 命令"), "--protected-path", str(base / "protected")],
            env=env, cwd=base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        role_output, _, _ = collect_output(role_proc, timeout=180, limit=65536,
            terminate=lambda: terminate_tree(role_proc))
        # Error text may contain a path. Report only bounded standard failure
        # categories and numerical exits, not the raw stderr or traceback.
        categories = [name for name in ("PermissionError", "OSError", "UpdateError", "ImportError",
            "ModuleNotFoundError", "Fatal Python error", "Failed to load", "failed to load",
            "access violation", "Access is denied") if name in diagnostic_errors]
        import re
        safe_errors = re.sub(r"[A-Za-z]:[\\/][^\r\n]*", "<isolated-test-path>", errors)
        pytest.fail(json.dumps({"installer_exit": installer.returncode,
            "installer_output": output.strip(), "diagnostic_exit": proc.returncode,
            "diagnostic_output": detail.strip(), "stderr_categories": categories,
            "installer_error": safe_errors[-4096:], "stderr_bytes": len(diagnostic_errors),
            "role_exit": role_proc.returncode, "role_output": role_output.strip(),
            "cleanup": proc._gitgo_cleanup}))
    assert open_managed(root).registration["bootstrap_api"] == 8
    command = bin_dir / "gitgo.ps1"
    assert command.read_bytes().startswith(b"\xef\xbb\xbf")
    launch = subprocess.run([shell, "-NoProfile", "-File", str(command), "--version"], env=env,
        cwd=base, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert launch.returncode == 0, launch.stderr
    assert launch.stdout.strip() == "Gitgo " + old
    before = (root / "control/ledger.json").read_bytes()
    duplicate = subprocess.run([shell, "-NoProfile", "-File", str(seed / "install-managed.ps1"),
        "-Root", str(root), "-BinDir", str(bin_dir)], env=env,
        cwd=base, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert duplicate.returncode != 0
    assert (root / "control/ledger.json").read_bytes() == before

def test_real_windows_bootstrap_automatically_applies_prepared_update_on_clean_exit(actual_lab):
    store, prepare, boot, _, old, new, env = actual_lab
    prepare()
    host=store.root/"bootstrap/gitgo-host/gitgo-host.exe"
    result=subprocess.run([str(host),"--gitgo-internal-role","managed-launch","--version"],
        env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=180)
    assert result.returncode==0,result.stderr
    assert "Gitgo "+old in result.stdout,result.stdout
    assert any(json.loads(line).get("update_status") == "committed"
               for line in result.stdout.splitlines() if line.startswith("{")),result.stdout
    assert store._ledger()["current"]["version"]==new
    boot(new)


def test_real_windows_bootstrap_respects_disabled_automatic_install(actual_lab):
    store, prepare, boot, before, old, _, _ = actual_lab
    service,_=prepare()
    service.configure("preview",True,True,False,0)
    boot(old)
    assert tree_digest(store.root/"active")==before

def test_real_managed_host_retains_lease_with_sandbox_program_rx(actual_lab):
    from backend.core.sandbox_access import WorkspaceAccess
    from scripts.smoke_packaged_runtime import ProtocolClient
    store, _, boot, before, old, _, env = actual_lab
    workspace=store.root.parent/"sandbox-rx-project";workspace.mkdir()
    access=WorkspaceAccess(workspace,[store.root/"active"],store.root.parent/"sandbox-rx-access.json")
    try:
        access.prepare()
        boot(old)
        client=ProtocolClient(store.root/"active/internal/gitgo-host/gitgo-host.exe",env)
        try:
            assert client.call("product.version")["result"]["version"]==old
            assert open_managed(store.root).recover().status=="busy"
        finally:client.close()
        assert tree_digest(store.root/"active")==before
    finally:access.close()


def test_managed_appcontainer_uses_only_inherited_lease_not_private_control(actual_lab):
    """Actual frozen child; a source parent without admission cannot forge it."""
    from backend.core.sandbox_access import WorkspaceAccess
    from backend.core.sandbox import SandboxPolicy, sandbox_environment, sandbox_popen
    from backend.core.process_control import terminate_tree
    from backend.core.process_output import collect_output
    store, _, _, before, _, _, env = actual_lab
    # Health probes omit user folders; creating an AppContainer needs the
    # operator profile location, as in the normal sandbox environment.
    env={**env,"LOCALAPPDATA":os.environ["LOCALAPPDATA"]}
    workspace=store.root.parent/"restricted-managed-tools";workspace.mkdir()
    host=store.root/"active/internal/gitgo-host/gitgo-host.exe"
    access=WorkspaceAccess(workspace,[store.root/"active"],store.root.parent/"restricted-access.json")
    child = """
import json,os
from pathlib import Path
from backend.core.update.sandbox_admission import in_appcontainer
from backend.core.installation_lease import runtime_descriptors
assert in_appcontainer()
assert runtime_descriptors()
denied=[]
for descriptor in runtime_descriptors():
    for operation in (lambda:os.read(descriptor,1),lambda:os.write(descriptor,b"x")):
        try:operation()
        except OSError:denied.append("contents")
        else:raise AssertionError("Lease content authority leaked")
control=Path(CONTROL_PATH)
try:control.read_bytes()
except PermissionError:denied.append("private-control")
else:raise AssertionError("Private update control was readable")
print(json.dumps({"restricted":True,"denied":denied}),flush=True)
""".replace("CONTROL_PATH",repr(str(store.root/"control/ledger.json")))
    driver = """
import json,subprocess
from pathlib import Path
from backend.core.child_process import python_command
from backend.core.sandbox import SandboxPolicy,sandbox_environment,sandbox_popen
from backend.core.process_control import terminate_tree
from backend.core.process_output import collect_output
import os
process=sandbox_popen(python_command(["-c",CHILD]),SandboxPolicy(Path(WORKSPACE)),
 cwd=WORKSPACE,env=sandbox_environment(os.environ),
 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8")
out,err,_=collect_output(process,timeout=45,limit=65536,terminate=lambda:terminate_tree(process))
assert process.returncode==0,(process.returncode,out,err)
cleanup=process.close_sandbox()
assert cleanup["complete"],cleanup
print(out,flush=True)
""".replace("CHILD",repr(child)).replace("WORKSPACE",repr(str(workspace)))
    try:
        access.prepare()
        run=subprocess.run([str(host),"--gitgo-internal-role","python","-c",driver],env=env,
            capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=90)
        assert run.returncode==0,run.stdout+"\n"+run.stderr
        result=json.loads(run.stdout)
        assert result["restricted"] and result["denied"].count("contents")>=2
        assert "private-control" in result["denied"]
        # No managed parent descriptor is registered in this source test process.
        # A selector in an environment cannot substitute for that capability.
        forged=sandbox_environment(env)
        forged["GITGO_SANDBOX_INSTALL_LEASE_HANDLES"]="12345"
        process=sandbox_popen([str(host),"--gitgo-internal-role","python","-c",
            "print('UNSAFE_BODY_STARTED')"],SandboxPolicy(workspace),cwd=str(workspace),env=forged,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8")
        out,err,_=collect_output(process,timeout=45,limit=65536,terminate=lambda:terminate_tree(process))
        assert process.returncode!=0,(out,err)
        assert "UNSAFE_BODY_STARTED" not in out
        assert process.close_sandbox()["complete"]
        assert tree_digest(store.root/"active")==before
    finally:access.close()
