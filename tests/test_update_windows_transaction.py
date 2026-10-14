"""Actual NTFS transaction/persistence with signed PLACEHOLDER bundles.

The health driver reads metadata only: this proves the shared state engine on
Windows, NOT a frozen product upgrade or visible Windows terminal acceptance.
"""
import hashlib
import io
import os
import zipfile

import pytest

from backend.core.update import CurrentInstallation, DataCompatibility, UpdatePolicy
from backend.core.update import windows_storage as win
from backend.core.update.persistence import atomic_control, read_control, lock_file, filesystem_identity
from backend.core.update.transaction import initialize_simulation, SimulationStore
from backend.core.update.tree import tree_digest, inventory
from backend.core.update.validation import UpdateError
from tests.test_update_transaction import build_fixture, compatibility_wire, inspect_health, Crash
from tests.test_update_manifest import signing, signed, manifest_wire
from tests.test_update_discovery import NOW

pytestmark = pytest.mark.skipif(not win.supported(), reason="Requires actual Windows NTFS engine")


@pytest.fixture
def lab(tmp_path_factory, signing):
    base = tmp_path_factory.resolve() / "private"
    win.create_private_directory(base)
    protected = base / "projects"; protected.mkdir(mode=0o700)
    (protected / "sentinel").write_bytes(b"project-and-database-unchanged")
    seed = base / "seed"; build_fixture(seed, version="1.0.0-alpha.2", platform="windows", architecture="x86_64")
    root = base / "managed-simulation"; root.mkdir(mode=0o700)
    current = CurrentInstallation("gitgo-terminal", "1.0.0-alpha.2", "windows", "x86_64", "0.1.0",
        DataCompatibility.parse(compatibility_wire()), True)
    store = initialize_simulation(root, seed, current, protected_paths=(protected,))
    original = tree_digest(root / "active")
    source = base / "source"; build_fixture(source, platform="windows", architecture="x86_64")
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if path.is_file(): archive.write(path, path.relative_to(source).as_posix())
    data = body.getvalue()
    raw, signature = signed(manifest_wire(platform="windows", architecture="x86_64", archive_format="zip",
        url="https://releases.example.test/gitgo.zip", bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
        data_compatibility=compatibility_wire()), signing)
    download = base / "download"; download.mkdir(mode=0o700)
    archive_path = download / "candidate.zip"; archive_path.write_bytes(data)
    policy = UpdatePolicy(enabled=True, channel="preview",
        manifest_url="https://releases.example.test/manifest.json", signature_url="https://releases.example.test/manifest.sig",
        allowed_hosts=("releases.example.test",))
    def apply(**kwargs):
        return store.apply(policy, signing[1], raw, signature, archive_path,
            health_check=kwargs.pop("health_check", inspect_health), clock=lambda: NOW, **kwargs)
    yield store, apply, original
    assert (protected / "sentinel").read_bytes() == b"project-and-database-unchanged"


def test_signed_zip_upgrade_and_explicit_retained_rollback(lab):
    store, apply, original = lab
    assert apply().status == "committed"
    water = store._ledger()["watermarks"]
    selection = store.rollback_selection()
    assert store._rollback(selection["rollback_id"], health_check=inspect_health).status == "rolled_back"
    assert tree_digest(store.root / "active") == original
    assert store._ledger()["watermarks"] == water


def test_failed_health_restores_previous_bytes(lab):
    store, apply, original = lab
    def unhealthy(*args): raise ValueError("Explicit test failure")
    assert apply(health_check=unhealthy).status == "rolled_back"
    assert tree_digest(store.root / "active") == original


@pytest.mark.parametrize("point", ["user_rollback_health_passed", "user_rollback_intent", "rollback_intent",
    "failed_renamed", "old_restored", "rollback_written"])
def test_interrupted_explicit_windows_rollback_uses_same_durable_ledger(lab, point):
    store, apply, original = lab
    assert apply().status == "committed"
    selection = store.rollback_selection()
    water = store._ledger()["watermarks"]
    def interrupt(name):
        if name == point: raise Crash()
    with pytest.raises(Crash):
        store._rollback(selection["rollback_id"], health_check=inspect_health, checkpoint=interrupt)
    fresh = SimulationStore(store.root, protected_paths=store.protected_paths)
    result = fresh.recover()
    expected = "committed" if point == "user_rollback_health_passed" else "rolled_back"
    assert result.status == expected
    assert fresh.recover().status == expected
    assert fresh._ledger()["watermarks"] == water
    if expected == "rolled_back": assert tree_digest(store.root / "active") == original


def test_failed_windows_predecessor_health_keeps_the_running_new_version(lab):
    store, apply, _ = lab
    assert apply().status == "committed"
    before = store._ledger()
    selection = store.rollback_selection()
    def unhealthy(*_): raise ValueError("Isolated fault injection")
    result = store._rollback(selection["rollback_id"], health_check=unhealthy)
    assert result.status == "failed" and store._ledger() == before
    assert store.recover().status == "committed"


@pytest.mark.parametrize("point", ["prepared", "old_renamed", "new_renamed", "check_new_intent"])
def test_fresh_reader_recovers_interrupted_transaction(lab, point):
    store, apply, original = lab
    def interrupt(name):
        if name == point: raise Crash()
    with pytest.raises(Crash): apply(checkpoint=interrupt)
    reopened = SimulationStore(store.root, protected_paths=store.protected_paths)
    assert reopened.recover().status == "rolled_back"
    assert tree_digest(store.root / "active") == original
    assert reopened.recover().status == "rolled_back"


def test_live_runtime_lease_prevents_switch(lab):
    store, apply, original = lab
    identity = store.registration["lock_identities"]["runtime.lock"]
    with lock_file(store.root / "control/runtime.lock", shared=True, expected_identity=identity):
        with pytest.raises(UpdateError, match="UPDATE_BUSY"): apply()
        assert store.recover().status == "busy"
    assert tree_digest(store.root / "active") == original


def test_replacement_and_ads_are_detected(lab):
    store, _, _ = lab
    sample = store.root / "active/product.json"
    with open(str(sample) + ":unexpected", "wb") as handle: handle.write(b"hidden")
    with pytest.raises(UpdateError, match="UPDATE_TREE_INVALID"): inventory(store.root / "active")
