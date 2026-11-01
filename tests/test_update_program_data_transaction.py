"""One commit decision for program/config using native NTFS and signed fixtures.

The fixture health worker is metadata only; not a frozen product acceptance.
"""
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from backend.core.update import CurrentInstallation, DataCompatibility, UpdatePolicy, health
from backend.core.update import data_transition, data_migrate, windows_storage as win
from backend.core.update.persistence import read_control
from backend.core.update.transaction import initialize_simulation, SimulationStore
from tests.test_update_manifest import signing, signed, manifest_wire
from tests.test_update_transaction import build_fixture, refresh_build, compatibility_wire, inspect_health, Crash
from tests.test_update_discovery import NOW

pytestmark = pytest.mark.skipif(not win.supported(), reason="Native NTFS transaction required")


@pytest.fixture
def lab(tmp_path_factory, signing, monkeypatch):
    base = tmp_path_factory.resolve() / "private"; win.create_private_directory(base)
    protected = base / "profile"; win.create_private_directory(protected)
    config = protected / "config.json"; config.write_bytes(b'{"projects":[],"retired_feature":"retained"}')
    original = config.read_bytes()
    seed = base / "seed"; build_fixture(seed, version="1.0.0-alpha.2", platform="windows", architecture="x86_64")
    root = base / "installation"; win.create_private_directory(root)
    current = CurrentInstallation("gitgo-terminal", "1.0.0-alpha.2", "windows", "x86_64", "0.2.0",
        DataCompatibility.parse(compatibility_wire()), True)
    store = initialize_simulation(root, seed, current, protected_paths=(protected,))
    candidate = base / "candidate"; build_fixture(candidate, platform="windows", architecture="x86_64")
    evidence = candidate / "migrations/config.py"; evidence.write_bytes(b"new writer evidence")
    contract = json.loads((candidate / "update-data-contract.json").read_bytes())
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    contract["data_compatibility"]["config_migration_sha256"] = digest; contract["config"]["sha256"] = digest
    (candidate / "update-data-contract.json").write_text(json.dumps(contract))
    refresh_build(candidate, platform="windows", architecture="x86_64")
    download = base / "download"; win.create_private_directory(download)
    archive = download / "candidate.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for path in candidate.rglob("*"):
            if path.is_file(): package.write(path, path.relative_to(candidate).as_posix())
    raw, signature = signed(manifest_wire(platform="windows", architecture="x86_64", archive_format="zip",
        bytes=archive.stat().st_size, sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        data_compatibility=contract["data_compatibility"]), signing)
    monkeypatch.setattr(data_transition, "resource_bindings", lambda *_: {"config": config})
    monkeypatch.setattr(data_migrate, "runtime_evidence", lambda: (contract["data_compatibility"], {}))
    def migrate(bundle, profile, plan, nonce):
        value = read_control(plan)
        assert value["nonce"] == nonce
        data_migrate.migrate_shadows({key: Path(path) for key, path in value["resources"].items()},
                                    DataCompatibility.parse(value["source"]))
    monkeypatch.setattr(health, "migrate_candidate_data", migrate)
    policy = UpdatePolicy(enabled=True, channel="preview", manifest_url="https://releases.example.test/manifest.json",
        signature_url="https://releases.example.test/manifest.sig", allowed_hosts=("releases.example.test",))
    def apply(**options):
        return store.apply(policy, signing[1], raw, signature, archive, clock=lambda: NOW,
            health_check=inspect_health, **options)
    return store, config, original, apply


@pytest.mark.parametrize("point", ["prepared", "old_renamed", "new_renamed", "health_passed",
    "data_publish_intent", "data_old_saved:config", "data_new_published:config", "data_published", "commit_written"])
def test_single_ledger_restores_or_commits_program_and_data_together(lab, point):
    store, config, original, apply = lab
    def crash(event):
        if event == point: raise Crash(event)
    with pytest.raises(Crash): apply(checkpoint=crash)
    fresh = SimulationStore(store.root, protected_paths=store.protected_paths)
    result = fresh.recover()
    if point == "commit_written":
        assert result.status == "committed" and result.version == "1.0.0-alpha.3"
        assert json.loads(config.read_bytes())["retired_feature"] == "retained"
        assert fresh.data_compatibility() != DataCompatibility.parse(compatibility_wire())
        assert fresh.rollback_blocker() == "UPDATE_REVERSE_MIGRATION_REQUIRED"
        assert fresh.rollback_selection() is None
    else:
        assert result.status == "rolled_back" and result.version == "1.0.0-alpha.2"
        assert config.read_bytes() == original
    assert fresh.recover().status == result.status
    config.write_bytes(b"later user work")
    assert fresh.recover().status == result.status and config.read_bytes() == b"later user work"
