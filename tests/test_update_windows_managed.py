"""Native NTFS managed registration/admission; fake probes are not frozen proof."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.core.installation_lease import runtime_descriptors
from backend.core.update import installation, windows_storage as win
from backend.core.update.installation import ManagedStore, initialize_managed, installed_runtime, open_managed
from backend.core.update.persistence import read_control, atomic_control
from backend.core.update.validation import UpdateError
from backend.core.update.health import isolated_environment
from tests.test_update_transaction import build_fixture, refresh_build

pytestmark = pytest.mark.skipif(not win.supported(), reason="Actual Windows managed ownership required")


@pytest.fixture
def managed(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.resolve() / "private"
    win.create_private_directory(base)
    protected = base / "protected"; protected.mkdir(mode=0o700)
    (protected / "sentinel").write_bytes(b"project-kept")
    seed = base / "seed"; build_fixture(seed, platform="windows", architecture="x86_64", version="1.0.0-alpha.2")
    (seed / "uninstall.exe").unlink()
    (seed / "install-managed.ps1").write_bytes(b"test-placeholder-not-an-installer")
    product = json.loads((seed / "product.json").read_bytes())
    product.update(installation_mode="managed-windows-v1", layout={"dashboard": "gitgo.exe",
        "native_host": "internal/gitgo-host/gitgo-host.exe", "installer": "install-managed.ps1"})
    (seed / "product.json").write_text(json.dumps(product))
    refresh_build(seed, platform="windows", architecture="x86_64", version=product["version"])
    def probe(host, profile):
        assert host.name == "gitgo-host.exe"
        contract = json.loads((seed / "update-data-contract.json").read_bytes())
        return {"runtime": {**{key: product[key] for key in ("product_id", "version", "platform", "architecture")},
                            "data_compatibility": contract["data_compatibility"]}}
    monkeypatch.setattr(installation, "probe_managed_host", probe)
    root = base / "managed"; win.create_private_directory(root)
    store = initialize_managed(root, seed, protected_paths=(protected,))
    yield store, seed, protected
    assert (protected / "sentinel").read_bytes() == b"project-kept"


def test_registration_uses_windows_only_bootstrap_and_schema(managed):
    store, _, _ = managed
    assert store.registration["schema_version"] == 6
    assert store.registration["bootstrap_api"] == 7
    assert store.registration["simulation"] is False
    assert store.registration["updates_enabled"] is False
    assert (store.root / "bootstrap/gitgo-host/gitgo-host.exe").is_file()
    wrapper = (store.root / "gitgo.cmd").read_text()
    assert "%~dp0bootstrap\\gitgo-host\\gitgo-host.exe" in wrapper
    assert "DisableDelayedExpansion" in wrapper and "chcp" not in wrapper
    assert open_managed(store.root).recover().status == "idle"


@pytest.mark.parametrize("role", [None, "daemon", "tool-runner", "python"])
def test_active_roles_hold_actual_sharing_lease_before_application_io(managed, monkeypatch, role):
    store, _, _ = managed
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(store.root / "active/internal/gitgo-host/gitgo-host.exe"))
    with installed_runtime(role):
        assert len(runtime_descriptors()) == 1
        assert store.recover().status == "busy"
    assert runtime_descriptors() == ()
    assert store.recover().status == "idle"


@pytest.mark.parametrize("component", ["launcher", "bootstrap", "schema", "api"])
def test_changed_immutable_control_cannot_admit_windows_runtime(managed, component):
    store, _, _ = managed
    if component == "launcher": (store.root / "gitgo.cmd").write_text("@exit /b 0")
    elif component == "bootstrap": (store.root / "bootstrap/gitgo-host/gitgo-host.exe").write_bytes(b"changed")
    else:
        path = store.root / "control/registration.json"; value = read_control(path)
        value["schema_version" if component == "schema" else "bootstrap_api"] -= 1
        atomic_control(path, value)
    with pytest.raises(UpdateError): open_managed(store.root)


def test_nonempty_windows_installation_is_not_adopted(managed):
    store, seed, protected = managed
    before = read_control(store.root / "control/ledger.json")
    with pytest.raises(UpdateError): initialize_managed(store.root, seed, protected_paths=(protected,))
    assert read_control(store.root / "control/ledger.json") == before


@pytest.mark.parametrize("fault", [None, "existing-profile", "outside-config"])
def test_actual_windows_source_startup_probe_is_isolated(tmp_path_factory, fault):
    base = tmp_path_factory.resolve() / "private"; win.create_private_directory(base)
    profile = base / "profile"; profile.mkdir(mode=0o700)
    outside = base / "outside.json"; outside.write_bytes(b"keep-user-data")
    env = {**isolated_environment(profile), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    if fault == "existing-profile": (profile / "user-data").write_bytes(b"keep")
    if fault == "outside-config": env["GITGO_CONFIG_PATH"] = str(outside)
    diagnostic_entry = """import sys
import backend.core.native_host_entry as entry
from backend.core.update.validation import UpdateError
original = entry._main
def traced(values):
    try: return original(values)
    except UpdateError as error:
        print(error.code + ': ' + str(error), file=sys.stderr)
        raise
    except OSError as error:
        print('OS_ERROR ' + str(getattr(error, 'winerror', error.errno)), file=sys.stderr)
        raise
entry._main = traced
raise SystemExit(entry.main(sys.argv[1:]))
"""
    result = subprocess.run([sys.executable, "-B", "-c", diagnostic_entry,
        "--gitgo-internal-role", "update-startup-probe", "--nonce", "e" * 32], cwd=profile,
        env=env, capture_output=True, text=True, timeout=40)
    assert outside.read_bytes() == b"keep-user-data"
    if fault:
        assert result.returncode != 0 and "native_host_check" not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["native_host_check"] is True
        assert not (profile / "providers.json").exists() and not (profile / "secrets.json").exists()
