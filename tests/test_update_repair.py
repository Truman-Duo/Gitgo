"""Native filesystem repair, ephemeral signatures, placeholder program fixtures.

Only the frozen binary probes are replaced with metadata/source adapters. No
package is built and these tests are not a frozen release upgrade acceptance.
"""
import hashlib
import json
import os
import zipfile

import pytest

from backend.core.update import health, repair as module
from backend.core.update.installation import open_managed, installed_runtime
from backend.core.update.manifest import DataCompatibility
from backend.core.update.persistence import atomic_control, read_control
from backend.core.update.data_migrate import migrate_shadows
from backend.core.update.validation import UpdateError
from tests.test_update_manifest import signing, signed, manifest_wire
from tests.test_update_windows_managed import managed
from tests.test_update_transaction import refresh_build, inspect_health, Crash
from tests.test_update_discovery import NOW
from backend.core.update import windows_storage as win

pytestmark = pytest.mark.skipif(not win.supported(), reason="Actual Windows NTFS repair required")


@pytest.fixture
def repair_lab(managed, signing, monkeypatch):
    import shutil
    from backend.core.update import installation
    store, seed, protected = managed
    registration = store.registration
    registration.update(bootstrap_api=8, updater_version="0.1.0")
    atomic_control(store.root / "control/registration.json", registration)
    store = open_managed(store.root)
    config = protected / "config.json"
    original = b'{"projects":[],"retired_option":{"important":"keep"}}'
    config.write_bytes(original)
    monkeypatch.setenv("GITGO_CONFIG_PATH", str(config))
    # Isolated known file binding, exercising the same compiled config adapter.
    monkeypatch.setattr(module, "resource_bindings", lambda *_: {"config": config})
    candidate = store.root.parent / "candidate"
    shutil.copytree(seed, candidate, copy_function=win.durable_copy)
    product = json.loads((candidate / "product.json").read_bytes())
    product["version"] = "1.0.0-alpha.3"
    (candidate / "product.json").write_text(json.dumps(product))
    contract = json.loads((candidate / "update-data-contract.json").read_bytes())
    refresh_build(candidate, platform="windows", architecture="x86_64")
    archive = store.root.parent / "candidate.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for path in candidate.rglob("*"):
            if path.is_file(): package.write(path, path.relative_to(candidate).as_posix())
    raw, signature = signed(manifest_wire(platform="windows", architecture="x86_64", archive_format="zip",
        url="https://releases.example.test/candidate.zip", bytes=archive.stat().st_size,
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), data_compatibility=contract["data_compatibility"]), signing)
    def probe(host, profile):
        bundle = host.parents[2]
        return {"runtime": {**json.loads((bundle / "product.json").read_bytes()),
                            "data_compatibility": contract["data_compatibility"]}}
    def migrate(bundle, profile, plan, nonce):
        value = read_control(plan)
        assert value["nonce"] == nonce
        from pathlib import Path
        migrate_shadows({key: Path(path) for key, path in value["resources"].items()},
                        DataCompatibility.parse(value["source"]))
    monkeypatch.setattr(installation, "probe_managed_host", probe)
    monkeypatch.setattr(health, "frozen_health_check", inspect_health)
    monkeypatch.setattr(health, "migrate_candidate_data", migrate)
    monkeypatch.setattr("backend.core.update.data_migrate.runtime_evidence", lambda: (contract["data_compatibility"], {}))
    return store, (raw, signature, archive), config, original


def execute(lab, **options):
    store, package, _, _ = lab
    return module.repair(store.root, *package, clock=lambda: NOW, **options)


def test_legacy_repair_commits_new_bootstrap_and_preserves_originals(repair_lab):
    store, _, config, original = repair_lab
    result = execute(repair_lab)
    assert result["status"] == "committed"
    new = open_managed(store.root)
    assert new.registration["bootstrap_api"] == 10 and new.registration["updater_version"] == "0.2.0"
    assert new._ledger()["current"]["version"] == "1.0.0-alpha.3"
    assert new._ledger()["watermarks"][0]["sequence"] == 2
    assert json.loads(config.read_bytes())["retired_option"] == {"important": "keep"}
    assert next(config.parent.glob(".config.json.gitgo-upgrade-*.old")).read_bytes() == original
    assert not (store.root / "control/repair-pending.json").exists()
    assert not module._pointer(store.root).exists()


@pytest.mark.parametrize("point", ["repair_prepared", "repair_quiesced", "repair_old_moved",
    "repair_new_provisioned", "data_old_saved:config", "data_new_published:config",
    "repair_data_published", "repair_committed"])
def test_every_repair_interruption_recovers_both_installation_and_data(repair_lab, point):
    store, _, config, original = repair_lab
    registration = store.registration
    def crash(event):
        if event == point: raise Crash(event)
    with pytest.raises(Crash): execute(repair_lab, checkpoint=crash)
    if point == "repair_new_provisioned":
        # The installed candidate cannot run before the shared commit decision.
        import sys
        from unittest.mock import patch
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(store.root / "active/internal/gitgo-host/gitgo-host.exe")):
            with pytest.raises(UpdateError, match="UPDATE_BOOTSTRAP_REPAIR_REQUIRED"):
                with installed_runtime(None): pass
    result = module.recover(store.root)
    if point == "repair_committed":
        assert result["status"] == "committed"
        assert json.loads(config.read_bytes())["retired_option"]["important"] == "keep"
    else:
        assert result["status"] == "rolled_back" and config.read_bytes() == original
        assert open_managed(store.root).registration == registration
    assert not module._pointer(store.root).exists()


def test_bad_signature_and_live_runtime_leave_old_installation_untouched(repair_lab):
    store, package, config, original = repair_lab
    with pytest.raises(UpdateError, match="UPDATE_SIGNATURE_INVALID"):
        module.repair(store.root, package[0] + b" ", *package[1:], clock=lambda: NOW)
    with store.runtime_lease():
        with pytest.raises(UpdateError, match="UPDATE_BUSY"): execute(repair_lab)
    assert config.read_bytes() == original and not module._pointer(store.root).exists()


def test_failed_startup_never_quiesces_legacy_installation(repair_lab, monkeypatch):
    store, _, config, original = repair_lab
    monkeypatch.setattr(health, "frozen_health_check", lambda *_: False)
    with pytest.raises(UpdateError, match="UPDATE_HEALTH_FAILED"): execute(repair_lab)
    assert open_managed(store.root).registration["bootstrap_api"] == 8
    assert config.read_bytes() == original and not module._pointer(store.root).exists()


def test_even_signed_candidate_cannot_replace_the_installed_trust(repair_lab, signing):
    store, package, config, original = repair_lab
    candidate = store.root.parent / "candidate"
    policy_path = candidate / "release-trust.json"
    policy = json.loads(policy_path.read_bytes()); policy["allowed_hosts"].append("another.example.test")
    policy_path.write_text(json.dumps(policy))
    refresh_build(candidate, platform="windows", architecture="x86_64")
    with zipfile.ZipFile(package[2], "w", zipfile.ZIP_DEFLATED) as archive:
        for path in candidate.rglob("*"):
            if path.is_file(): archive.write(path, path.relative_to(candidate).as_posix())
    wire = json.loads(package[0]); wire.update(bytes=package[2].stat().st_size,
        sha256=hashlib.sha256(package[2].read_bytes()).hexdigest())
    raw, signature = signed(wire, signing)
    with pytest.raises(UpdateError, match="UPDATE_TRUST_CHANGED"):
        module.repair(store.root, raw, signature, package[2], clock=lambda: NOW)
    assert config.read_bytes() == original and not module._pointer(store.root).exists()


def test_recovery_rejects_unrelated_installation_before_restoring_any_data(repair_lab):
    store, _, config, _ = repair_lab
    def crash(event):
        if event == "repair_data_published": raise Crash()
    with pytest.raises(Crash): execute(repair_lab, checkpoint=crash)
    converted = config.read_bytes()
    os.rename(store.root, store.root.with_name("detached-candidate"))
    win.create_private_directory(store.root)
    (store.root / "unrelated").write_bytes(b"do not touch")
    with pytest.raises(UpdateError, match="UPDATE_REPAIR_REQUIRED"): module.recover(store.root)
    assert config.read_bytes() == converted and (store.root / "unrelated").read_bytes() == b"do not touch"
