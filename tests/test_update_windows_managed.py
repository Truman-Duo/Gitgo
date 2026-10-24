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
from tests.test_update_manifest import signing

pytestmark = pytest.mark.skipif(not win.supported(), reason="Actual Windows managed ownership required")


@pytest.fixture
def managed(tmp_path_factory, monkeypatch, signing):
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
    import base64
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    public = signing[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    policy = {"schema_version": 1, "product_id": "gitgo-terminal", "platform": "windows", "architecture": "x86_64",
        "allowed_hosts": ["releases.example.test"], "public_keys": [{"key_id": "ephemeral-test", "ed25519_base64": base64.b64encode(public).decode()}],
        "channels": {"preview": {"manifest_url": "https://releases.example.test/manifest.json", "signature_url": "https://releases.example.test/manifest.sig"}}}
    (seed / "release-trust.json").write_text(json.dumps(policy))
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
    assert store.registration["bootstrap_api"] == 8
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
        value["schema_version" if component == "schema" else "bootstrap_api"] = (0 if component == "api" else value["schema_version"] - 1)
        atomic_control(path, value)
    with pytest.raises(UpdateError): open_managed(store.root)


def test_nonempty_windows_installation_is_not_adopted(managed):
    store, seed, protected = managed
    before = read_control(store.root / "control/ledger.json")
    with pytest.raises(UpdateError): initialize_managed(store.root, seed, protected_paths=(protected,))
    assert read_control(store.root / "control/ledger.json") == before


def test_probe_profile_cannot_nest_inside_its_candidate_or_copy_itself(managed):
    from backend.core.update.health import frozen_health_check
    _, seed, _ = managed
    with pytest.raises(UpdateError, match="UPDATE_HEALTH_FAILED"):
        frozen_health_check(seed, seed)
    assert not (seed / "program").exists()


@pytest.mark.parametrize("automatic", [False, True])
def test_native_windows_download_cache_can_be_flushed_and_revalidated(managed, signing, monkeypatch, automatic):
    import hashlib
    import httpx
    import zipfile
    from backend.core.update.preparation_service import UpdatePreparationService
    from tests.test_update_manifest import manifest_wire, signed
    from tests.test_update_discovery import NOW
    from tests.test_update_transaction import compatibility_wire
    from tests.test_update_preparation_service import Stream, httpx_transport
    store, _, _ = managed
    candidate = store.root.parent / "candidate"
    build_fixture(candidate, platform="windows", architecture="x86_64")
    (candidate / "uninstall.exe").unlink()
    (candidate / "install-managed.ps1").write_bytes(b"test-placeholder-only")
    product = json.loads((candidate / "product.json").read_bytes())
    product.update(installation_mode="managed-windows-v1", layout={"dashboard": "gitgo.exe",
        "native_host": "internal/gitgo-host/gitgo-host.exe", "installer": "install-managed.ps1"})
    (candidate / "product.json").write_text(json.dumps(product))
    refresh_build(candidate, platform="windows", architecture="x86_64")
    archive = store.root.parent / "candidate.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for path in candidate.rglob("*"):
            if path.is_file(): package.write(path, path.relative_to(candidate).as_posix())
    data = archive.read_bytes()
    raw, signature = signed(manifest_wire(platform="windows", architecture="x86_64", archive_format="zip",
        url="https://releases.example.test/candidate.zip", bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
        data_compatibility=compatibility_wire()), signing)
    def serve(request):
        value = raw if str(request.url).endswith(".json") else signature if str(request.url).endswith(".sig") else data
        return httpx.Response(200, stream=Stream(value))
    service = UpdatePreparationService(store_loader=lambda: open_managed(store.root), clock=lambda: NOW,
        transport_factory=lambda hosts, **options: httpx_transport(hosts, serve, **options))
    if automatic:
        from backend.core.update.automatic import tick, queue_on_clean_exit, configure
        from backend.core.update import handoff
        tick(service)
        selected = service.status()["candidate"]["candidate_id"]
        assert service.status()["status"] == "downloaded"
        with handoff.launch_session(store) as nonce:
            result = queue_on_clean_exit(service, nonce)
            assert result["candidate_id"] == selected and result["exit_code"] == 75
            ticket = read_control(handoff._path(store, nonce, "request"))
            assert ticket["schema_version"] == 2 and ticket["preference_revision"] == 0
            configure(service, "preview", True, True, False, 0)
            with pytest.raises(UpdateError, match="UPDATE_SELECTION_STALE"):
                handoff._perform_request(store, ticket, verify_only=True)
    else:
        selected = service.check("preview", "a" * 32)["candidate"]["candidate_id"]
        assert service.download("preview", selected, "b" * 32)["status"] == "downloaded"
    assert service.status("preview")["status"] == "downloaded"
    cached = next((store.root / "control/preparation").glob("gitgo-update-*/archive.verified"))
    descriptor = win.open_file(cached, write=True)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)
    assert store._ledger()["current"]["version"] == "1.0.0-alpha.2"


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

def test_managed_payload_allows_sandbox_rx_without_exposing_update_controls(managed):
    from backend.core.sandbox_access import WorkspaceAccess
    from backend.core.update.tree import tree_digest
    store,_,_=managed
    workspace=store.root.parent/"sandbox-workspace";workspace.mkdir()
    (workspace/"input.txt").write_text("test",encoding="utf-8")
    before=tree_digest(store.root/"active")
    access=WorkspaceAccess(workspace,[store.root/"active"],store.root.parent/"sandbox-access.json")
    try:
        access.prepare()
        assert tree_digest(store.root/"active")==before
        with store.runtime_lease():pass
        # Program RX must never relax the control ledger's private boundary.
        with win.opened(store.root/"control/ledger.json"):pass
    finally:access.close()

def test_program_read_access_does_not_relax_private_control_acl(managed):
    from backend.core.sandbox_access import WorkspaceAccess
    store,_,_=managed
    workspace=store.root.parent/"control-read-workspace";workspace.mkdir()
    access=WorkspaceAccess(workspace,[store.root/"control"],store.root.parent/"control-read-access.json")
    try:
        access.prepare()
        with pytest.raises(UpdateError,match="UPDATE_ACL_INVALID"):
            with win.opened(store.root/"control/ledger.json"):pass
    finally:access.close()


def test_program_payload_still_rejects_sandbox_modify_permission(managed):
    from backend.core.sandbox_access import WorkspaceAccess
    from backend.core.update.tree import tree_digest
    store,_,_=managed
    access=WorkspaceAccess(store.root/"active",[],store.root.parent/"program-modify-access.json")
    try:
        access.prepare()
        with pytest.raises(UpdateError,match="UPDATE_ACL_INVALID"):
            tree_digest(store.root/"active")
    finally:access.close()
