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
                            capture_output=True, text=True, timeout=120)
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
        env=env, capture_output=True, text=True, timeout=120)
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


def test_actual_windows_installer_and_unicode_powershell_command(actual_lab):
    from backend.core.process_control import spawn_managed, terminate_tree
    from backend.core.process_output import collect_output
    store, _, _, _, old, _, env = actual_lab
    base = store.root.parent
    root, bin_dir = base / "独立 安装", base / "独立 命令"
    seed = base / "seed"
    installer = spawn_managed(["pwsh.exe", "-NoProfile", "-File", str(seed / "install-managed.ps1"),
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
 install_from_frozen_package(*json.loads(sys.argv[1]))
except Exception as error:
 items=[]
 for _ in range(8):
  if error is None: break
  items.append({'type':type(error).__name__,'code':getattr(error,'code',None),'winerror':getattr(error,'winerror',None)})
  error=error.__cause__ or error.__context__
 print(json.dumps(items));sys.exit(2)
"""
        arguments = json.dumps([str(base / "diagnostic-root"), str(base / "diagnostic-bin"), []])
        proc = spawn_managed([str(seed / "internal/gitgo-host/gitgo-host.exe"),
            "--gitgo-internal-role", "python", "-c", diagnostic, arguments], env=env, cwd=base,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        detail, diagnostic_errors, _ = collect_output(proc, timeout=180, limit=65536, terminate=lambda: terminate_tree(proc))
        # Error text may contain a path. Report only bounded standard failure
        # categories and numerical exits, not the raw stderr or traceback.
        categories = [name for name in ("PermissionError", "OSError", "UpdateError", "ImportError",
            "ModuleNotFoundError", "Fatal Python error", "Failed to load", "failed to load",
            "access violation", "Access is denied") if name in diagnostic_errors]
        pytest.fail(json.dumps({"installer_exit": installer.returncode,
            "installer_output": output.strip(), "diagnostic_exit": proc.returncode,
            "diagnostic_output": detail.strip(), "stderr_categories": categories,
            "stderr_bytes": len(diagnostic_errors), "cleanup": proc._gitgo_cleanup}))
    assert open_managed(root).registration["bootstrap_api"] == 7
    command = bin_dir / "gitgo.ps1"
    assert command.read_bytes().startswith(b"\xef\xbb\xbf")
    launch = subprocess.run(["pwsh.exe", "-NoProfile", "-File", str(command), "--version"], env=env,
        cwd=base, capture_output=True, text=True, timeout=120)
    assert launch.returncode == 0, launch.stderr
    assert launch.stdout.strip() == "Gitgo " + old
    before = (root / "control/ledger.json").read_bytes()
    duplicate = subprocess.run(["pwsh.exe", "-NoProfile", "-File", str(seed / "install-managed.ps1"),
        "-Root", str(root), "-BinDir", str(bin_dir)], env=env,
        cwd=base, capture_output=True, text=True, timeout=60)
    assert duplicate.returncode != 0
    assert (root / "control/ledger.json").read_bytes() == before
