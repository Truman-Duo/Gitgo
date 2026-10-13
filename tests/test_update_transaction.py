"""Simulation-only installation authority, state transitions and fresh recovery."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tarfile

import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from backend.core.update import CurrentInstallation, DataCompatibility, UpdateError, UpdatePolicy
from backend.core.update.persistence import atomic_control, lock_file, read_control
from backend.core.update.transaction import HealthReport, SimulationStore, initialize_simulation
from backend.core.update.tree import inventory, tree_digest
from tests.test_update_discovery import NOW, POLICY
from tests.test_update_manifest import manifest_wire, signed, signing


pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Simulation transaction locks/ownership are POSIX-only; no Windows apply adapter",
)


DATA = {"state": b"state-migration-source", "observability": b"observability-migration-source",
        "config": b"config-migration-source", "provider": b"provider-migration-source"}


def compatibility_wire():
    sha = {key: hashlib.sha256(value).hexdigest() for key, value in DATA.items()}
    return {"state_migrations": [{"version": 1, "sha256": sha["state"]}],
            "observability_migrations": [{"version": 1, "sha256": sha["observability"]}],
            "config_schema_version": 1, "config_migration_sha256": sha["config"],
            "provider_schema_version": 1, "provider_migration_sha256": sha["provider"]}


def build_fixture(root, *, version="1.0.0-alpha.3", platform="macos", architecture="arm64"):
    root.mkdir(mode=0o700)
    (root / "internal/gitgo-host").mkdir(parents=True, mode=0o700)
    (root / "migrations").mkdir(mode=0o700)
    for key, data in DATA.items():
        (root / "migrations" / (key + ".py")).write_bytes(data)
    layout = {"dashboard": "gitgo", "native_host": "internal/gitgo-host/gitgo-host",
              "installer": "install.sh", "uninstaller": "uninstall.sh"}
    if platform == "windows":
        layout = {"dashboard": "gitgo.exe", "native_host": "internal/gitgo-host/gitgo-host.exe",
                  "uninstaller": "uninstall.exe"}
    for name in layout.values():
        path = root / name
        path.write_bytes(b"placeholder-only; NOT a runnable product")
        path.chmod(0o700)
    product = {"schema_version": 2, "product_id": "gitgo-terminal", "primary_command": "gitgo",
               "version": version, "platform": platform, "architecture": architecture, "layout": layout}
    (root / "product.json").write_text(json.dumps(product))
    compat = compatibility_wire()
    contract = {"schema_version": 1, "data_compatibility": compat}
    for name, key in (("state_migrations", "state"), ("observability_migrations", "observability")):
        contract[name] = [{**compat[name][0], "path": "migrations/" + key + ".py"}]
    for name in ("config", "provider"):
        contract[name] = {"schema_version": 1, "path": "migrations/" + name + ".py",
                          "sha256": compat[name + "_migration_sha256"]}
    (root / "update-data-contract.json").write_text(json.dumps(contract))
    refresh_build(root, version=version, platform=platform, architecture=architecture)


def refresh_build(root, *, version="1.0.0-alpha.3", platform="macos", architecture="arm64"):
    files = [{key: item[key] for key in ("path", "bytes", "sha256")} for item in inventory(root)
             if item["kind"] == "file" and item["path"] != "build-manifest.json"]
    build = {"schema_version": 1, "version": version,
             "platform": "win-x64" if platform == "windows" else platform, "architecture": architecture,
             "source": {"commit": "b" * 40, "tree": "c" * 40, "dirty": False}, "files": files}
    (root / "build-manifest.json").write_text(json.dumps(build))


def archive_fixture(source, directory, signing, **changes):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        archive.add(source, arcname="macos-arm64")
    body = stream.getvalue()
    wire = manifest_wire(bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                         data_compatibility=compatibility_wire(), **changes)
    raw, detached = signed(wire, signing)
    path = directory / "candidate.tar.gz"
    path.write_bytes(body)
    return raw, detached, path


def inspect_health(active, profile):
    # Read metadata only. NEVER execute fixture binaries or scripts.
    assert profile.is_dir() and not list(profile.iterdir())
    product = json.loads((active / "product.json").read_text())
    build = json.loads((active / "build-manifest.json").read_text())
    return HealthReport(product["product_id"], product["version"], product["platform"], product["architecture"],
                        build["source"]["commit"], build["source"]["tree"], True)


@pytest.fixture
def lab(tmp_path_factory, signing):
    base = tmp_path_factory.resolve()
    protected = []
    for name in ("project", "config", "runtime"):
        path = base / name
        path.mkdir()
        for file in ("sentinel", "sentinel-wal", "sentinel-shm"):
            (path / file).write_bytes((name + file).encode())
        protected.append(path)
    seed = base / "seed"
    seed.mkdir(mode=0o700)
    (seed / "program").write_bytes(b"old runnable simulation placeholder")
    root = base / "simulation"
    root.mkdir(mode=0o700)
    current = CurrentInstallation("gitgo-terminal", "1.0.0-alpha.2", "macos", "arm64", "0.1.0",
                                  DataCompatibility.parse(compatibility_wire()), True)
    store = initialize_simulation(root, seed, current, protected_paths=tuple(protected))
    source = base / "new-source"
    build_fixture(source)
    download = base / "download"
    download.mkdir(mode=0o700)
    package = archive_fixture(source, download, signing)
    snapshots = {p: p.read_bytes() for r in protected for p in r.rglob("*") if p.is_file()}
    yield store, source, package, signing, current
    assert all(p.read_bytes() == data for p, data in snapshots.items())


def apply(lab, **options):
    store, _, (raw, detached, archive), signing, _ = lab
    return store.apply(options.pop("policy", POLICY), signing[1], raw, detached, archive,
                       health_check=options.pop("health_check", inspect_health),
                       clock=options.pop("clock", lambda: NOW), **options)


def reopen(store):
    return SimulationStore(store.root, protected_paths=store.protected_paths)


@pytest.mark.parametrize("after_write", [False, True])
def test_failed_second_update_intent_does_not_report_previous_commit_as_new_success(lab, monkeypatch, after_write):
    assert apply(lab).status == "committed"
    store, source, _, signing, _ = lab
    before = tree_digest(store.root / "active")
    product = json.loads((source / "product.json").read_bytes()); product["version"] = "1.0.0-alpha.4"
    (source / "product.json").write_text(json.dumps(product))
    refresh_build(source, version=product["version"])
    raw, signature, archive = archive_fixture(source, source.parent / "download", signing,
        version=product["version"], sequence=3)
    save = store._save
    once = False
    def fail(ledger, state=None, **kwargs):
        nonlocal once
        if not once and state is None and ledger["transaction"]["new"]["version"] == product["version"]:
            once = True
            if after_write: save(ledger, state, **kwargs)
            raise OSError("test durable intent failure")
        return save(ledger, state, **kwargs)
    monkeypatch.setattr(store, "_save", fail)
    result = store.apply(POLICY, signing[1], raw, signature, archive, health_check=inspect_health, clock=lambda: NOW)
    assert result.status == ("rolled_back" if after_write else "failed")
    assert result.version == "1.0.0-alpha.3"
    assert tree_digest(store.root / "active") == before


class Crash(BaseException):
    pass


def crash_at(point):
    def callback(event):
        if event == point:
            raise Crash(event)
    return callback


def test_success_commits_identity_and_retains_previous_payload(lab):
    store = lab[0]
    old = tree_digest(store.root / "active")
    result = apply(lab)
    assert result.status == "committed"
    ledger = read_control(store.root / "control/ledger.json")
    txn = ledger["transaction"]
    assert txn["state"] == "COMMITTED" and ledger["current"]["version"] == "1.0.0-alpha.3"
    assert tree_digest(store.root / "transactions" / txn["id"] / "old") == old
    assert reopen(store).recover().status == "committed"
    with reopen(store).runtime_session() as active:
        assert (active / "product.json").is_file()


def test_disabled_does_not_touch_store_or_dependency():
    store = object.__new__(SimulationStore)
    assert store.apply(UpdatePolicy(), None, None, None, None, health_check=None,
                       clock=lambda: pytest.fail("disabled clock used")).status == "disabled"


@pytest.mark.parametrize("report", [False, True, None, {"passed": True}])
def test_boolean_or_unstructured_approval_cannot_commit(lab, report):
    result = apply(lab, health_check=lambda *_: report)
    assert result.status == "rolled_back" and result.error_code == "UPDATE_HEALTH_FAILED"
    assert (lab[0].root / "active/program").read_bytes().startswith(b"old")


@pytest.mark.parametrize("change", [{"passed": False}, {"version": "99.0.0"}, {"platform": "linux"},
                                    {"source_tree": "d" * 40}, {"product_id": "other"}, {"passed": 1}])
def test_probe_failure_or_wrong_identity_restores_old(lab, change):
    def probe(active, profile):
        return replace(inspect_health(active, profile), **change)
    assert apply(lab, health_check=probe).status == "rolled_back"
    ledger = read_control(lab[0].root / "control/ledger.json")
    assert ledger["transaction"]["state"] == "ROLLED_BACK"
    assert ledger["current"]["version"] == "1.0.0-alpha.2"
    assert ledger["watermarks"][0]["sequence"] == 2  # NEVER downgrade high water
    assert reopen(lab[0]).recover().status == "rolled_back"
    assert apply(lab).status == "committed"  # exact signed release may retry


@pytest.mark.parametrize("point", ["candidate_validated", "candidate_synced", "prepared", "save_old_intent",
                                    "old_renamed", "publish_new_intent", "new_renamed", "check_new_intent",
                                    "health_passed", "commit_written"])
def test_fresh_store_recovers_every_forward_interruption(lab, point):
    with pytest.raises(Crash):
        apply(lab, checkpoint=crash_at(point))
    store = reopen(lab[0])
    if point not in ("candidate_validated", "candidate_synced", "commit_written"):
        with pytest.raises(UpdateError, match="UPDATE_RECOVERY_REQUIRED"):
            with store.runtime_session():
                pytest.fail("uncommitted payload admitted")
    result = store.recover()
    expected = "idle" if point.startswith("candidate_") else "committed" if point == "commit_written" else "rolled_back"
    assert result.status == expected
    assert store.recover().status == expected  # idempotent, no duplicate moves
    with store.runtime_session() as active:
        assert (active / ("product.json" if expected == "committed" else "program")).is_file()


@pytest.mark.parametrize("point", ["rollback_intent", "failed_renamed", "old_restored", "rollback_written"])
def test_fresh_store_recovers_every_rollback_interruption(lab, point):
    with pytest.raises(Crash):
        apply(lab, health_check=lambda *_: False, checkpoint=crash_at(point))
    assert reopen(lab[0]).recover().status == "rolled_back"
    assert (lab[0].root / "active/program").is_file()


def test_live_runtime_prevents_update_without_killing_it(lab):
    store = lab[0]
    before = (store.root / "control/ledger.json").read_bytes()
    with store.runtime_session() as path, store.runtime_session() as second:
        assert path == second
        with pytest.raises(UpdateError, match="UPDATE_BUSY"):
            apply(lab)
        assert store.recover().status == "busy"
    assert (store.root / "control/ledger.json").read_bytes() == before
    assert not list((store.root / "transactions").iterdir())
    assert apply(lab).status == "committed"


def test_concurrent_operation_lock_has_one_authority_inode(lab):
    store = lab[0]
    path = store.root / "control/operation.lock"
    inode = path.stat().st_ino
    with lock_file(path):
        with pytest.raises(UpdateError, match="UPDATE_BUSY"):
            apply(lab)
    assert path.stat().st_ino == inode
    assert apply(lab).status == "committed"
    assert path.stat().st_ino == inode


@pytest.mark.parametrize("slot", ["active", "old", "new"])
def test_unknown_or_mutated_recorded_payload_is_not_overwritten(lab, slot):
    point = "prepared" if slot == "new" else "new_renamed"
    with pytest.raises(Crash):
        apply(lab, checkpoint=crash_at(point))
    store = lab[0]
    transaction = read_control(store.root / "control/ledger.json")["transaction"]
    path = store.root / "active" if slot == "active" else store.root / "transactions" / transaction["id"] / slot
    (path / "unexpected").write_bytes(b"retain this user change")
    before = tree_digest(path)
    assert reopen(store).recover().status == "recovery_required"
    assert tree_digest(path) == before
    assert (path / "unexpected").is_file()


def test_redirected_slot_is_retained_and_never_traversed(lab):
    with pytest.raises(Crash):
        apply(lab, checkpoint=crash_at("old_renamed"))
    active = lab[0].root / "active"
    active.symlink_to(lab[0].protected_paths[0], target_is_directory=True)
    assert reopen(lab[0]).recover().status == "recovery_required"
    assert active.is_symlink()


@pytest.mark.parametrize("corruption", [b"bad JSON", b"{}", b'{"schema_version":1,"schema_version":1}'])
def test_corrupt_ledger_never_becomes_a_new_baseline(lab, corruption):
    path = lab[0].root / "control/ledger.json"
    path.write_bytes(corruption)
    assert reopen(lab[0]).recover().status == "recovery_required"
    assert path.read_bytes() == corruption
    assert (lab[0].root / "active/program").is_file()


def test_ledger_cannot_redirect_slot_paths(lab):
    with pytest.raises(Crash):
        apply(lab, checkpoint=crash_at("prepared"))
    path = lab[0].root / "control/ledger.json"
    value = read_control(path)
    value["transaction"]["id"] = "../../project"
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    assert reopen(lab[0]).recover().status == "recovery_required"
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", ["registration.json", "ledger.json", "operation.lock", "runtime.lock"])
def test_control_symlinks_cannot_redirect_reads_or_locks(lab, name):
    path = lab[0].root / "control" / name
    path.rename(path.with_suffix(".retained"))
    path.symlink_to(lab[0].protected_paths[0] / "sentinel")
    if name in ("registration.json", "operation.lock", "runtime.lock"):
        with pytest.raises(UpdateError):
            reopen(lab[0])
        assert lab[0].recover().status == "recovery_required"
    else:
        assert reopen(lab[0]).recover().status == "recovery_required"
    assert path.is_symlink()


def test_nonempty_or_protected_root_cannot_be_adopted(lab):
    store, source, _, _, current = lab
    with pytest.raises(UpdateError, match="UPDATE_SIMULATION_INVALID"):
        initialize_simulation(store.root, source, current, protected_paths=store.protected_paths)
    protected = store.protected_paths[0]
    protected.chmod(0o700)
    with pytest.raises(UpdateError, match="UPDATE_PROTECTED_PATHS"):
        initialize_simulation(protected, source, current, protected_paths=store.protected_paths)


def test_health_driver_cannot_modify_payload_and_still_commit(lab):
    def probe(active, profile):
        report = inspect_health(active, profile)
        (active / "gitgo").write_bytes(b"modified")
        return report
    result = apply(lab, health_check=probe)
    assert result.status == "recovery_required"  # unknown new payload retained
    assert read_control(lab[0].root / "control/ledger.json")["transaction"]["state"] == "CHECK_NEW"


def test_candidate_bad_signature_or_digest_cannot_move_current(lab):
    store, _, (raw, detached, archive), keys, _ = lab
    before = tree_digest(store.root / "active")
    for altered_raw, altered_archive in ((raw + b"\n", False), (raw, True)):
        if altered_archive:
            archive.write_bytes(b"broken archive")
        with pytest.raises(UpdateError):
            store.apply(POLICY, keys[1], altered_raw, detached, archive,
                        health_check=lambda *_: pytest.fail("unverified package probed"), clock=lambda: NOW)
        assert tree_digest(store.root / "active") == before
        assert read_control(store.root / "control/ledger.json")["transaction"] is None


@pytest.mark.parametrize("file,change", [
    ("product.json", {"version": "99.0.0"}), ("product.json", {"platform": "linux"}),
    ("product.json", {"schema_version": True}), ("build-manifest.json", {"files": []}),
    ("build-manifest.json", {"source": {"commit": "d" * 40, "tree": "c" * 40, "dirty": False}}),
    ("update-data-contract.json", {"schema_version": 2}),
    ("update-data-contract.json", {"state_migrations": []}),
    ("update-data-contract.json", {"config": {"schema_version": 1, "path": "../../config", "sha256": "1" * 64}}),
])
def test_correctly_signed_but_inconsistent_package_cannot_switch(lab, file, change):
    store, source, _, keys, _ = lab
    path = source / file
    value = json.loads(path.read_text())
    value.update(change)
    path.write_text(json.dumps(value))
    if file != "build-manifest.json":
        refresh_build(source)
    package = archive_fixture(source, lab[2][2].parent, keys)
    modified = (store, source, package, keys, lab[4])
    before = tree_digest(store.root / "active")
    with pytest.raises(UpdateError, match="UPDATE_PACKAGE_CONTRACT"):
        apply(modified)
    assert tree_digest(store.root / "active") == before
    assert read_control(store.root / "control/ledger.json")["transaction"] is None


def test_actual_migration_hash_is_checked_not_just_matching_metadata(lab):
    store, source, _, keys, _ = lab
    (source / "migrations/state.py").write_bytes(b"changed migration source")
    refresh_build(source)
    package = archive_fixture(source, lab[2][2].parent, keys)
    with pytest.raises(UpdateError, match="UPDATE_PACKAGE_CONTRACT"):
        apply((store, source, package, keys, lab[4]))
    assert (store.root / "active/program").is_file()


def test_rollback_does_not_allow_older_release_metadata_on_restart(lab):
    assert apply(lab, health_check=lambda *_: False).status == "rolled_back"
    store, source, _, keys, current = lab
    older = archive_fixture(source, lab[2][2].parent, keys, sequence=1)
    with pytest.raises(UpdateError, match="UPDATE_REPLAY_REJECTED"):
        apply((reopen(store), source, older, keys, current))


def test_expiry_during_health_probe_restores_old(lab):
    times = iter([NOW, NOW, NOW + timedelta(days=3)])
    assert apply(lab, clock=lambda: next(times)).status == "rolled_back"


def test_fsync_failure_before_switch_does_not_lose_old(lab, monkeypatch):
    from backend.core.update import transaction
    original = transaction.atomic_control
    failed = [False]
    def fail_once(path, value):
        if value.get("transaction") and not failed[0]:
            failed[0] = True
            raise OSError("disk failure")
        original(path, value)
    monkeypatch.setattr(transaction, "atomic_control", fail_once)
    assert apply(lab).status == "failed"
    assert reopen(lab[0]).recover().status == "idle"
    assert (lab[0].root / "active/program").is_file()


def test_post_commit_error_uses_durable_record_not_stale_memory(lab):
    def error_after_commit(event):
        if event == "commit_written":
            raise OSError("response lost")
    assert apply(lab, checkpoint=error_after_commit).status == "committed"


@pytest.mark.parametrize("point", ["old_renamed", "new_renamed", "commit_written"])
def test_real_child_process_exit_releases_locks_and_recovers(lab, point):
    store, _, (raw, detached, archive), keys, _ = lab
    (archive.parent / "manifest.json").write_bytes(raw)
    (archive.parent / "manifest.sig").write_bytes(detached)
    public = keys[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    script = '''
import os, sys
from pathlib import Path
from backend.core.update import ManifestVerifier
from backend.core.update.transaction import SimulationStore
from tests.test_update_transaction import inspect_health
from tests.test_update_discovery import NOW, POLICY
root, archive, public, point = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
store = SimulationStore(root, protected_paths=tuple(Path(p) for p in sys.argv[5:]))
def checkpoint(event):
    if event == point:
        os._exit(73)
store.apply(POLICY, ManifestVerifier({"ephemeral-test": bytes.fromhex(public)}),
    (archive.parent / "manifest.json").read_bytes(), (archive.parent / "manifest.sig").read_bytes(),
    archive, health_check=inspect_health, clock=lambda: NOW, checkpoint=checkpoint)
'''
    result = subprocess.run([sys.executable, "-B", "-c", script, str(store.root), str(archive), public, point,
                             *(str(p) for p in store.protected_paths)], capture_output=True, timeout=15)
    assert result.returncode == 73, result.stderr.decode()
    assert reopen(store).recover().status == ("committed" if point == "commit_written" else "rolled_back")


def test_up_to_date_metadata_also_persists_channel_high_water(lab):
    assert apply(lab).status == "committed"
    store, source, (raw, detached, archive), keys, current = lab
    wire = json.loads(raw)
    wire["sequence"] = 3
    newer_raw, newer_sig = signed(wire, keys)
    assert apply((reopen(store), source, (newer_raw, newer_sig, archive), keys, current)).status == "up_to_date"
    ledger = read_control(store.root / "control/ledger.json")
    assert ledger["watermarks"][0]["sequence"] == 3
    assert ledger["transaction"]["watermark"]["sequence"] == 2  # retained transaction evidence
    assert reopen(store).recover().status == "committed"
    with pytest.raises(UpdateError, match="UPDATE_REPLAY_REJECTED"):
        apply((reopen(store), source, (raw, detached, archive), keys, current))


@pytest.mark.parametrize("name", ["id", "state", "old", "new", "release", "watermark"])
def test_ledger_structural_damage_does_not_move_any_directory(lab, name):
    with pytest.raises(Crash):
        apply(lab, checkpoint=crash_at("new_renamed"))
    store = lab[0]
    path = store.root / "control/ledger.json"
    value = read_control(path)
    value["transaction"][name] = None
    path.write_text(json.dumps(value))
    before = tree_digest(store.root / "active")
    assert reopen(store).recover().status == "recovery_required"
    assert tree_digest(store.root / "active") == before


def test_record_write_error_after_replace_reloads_durable_intent(lab, monkeypatch):
    from backend.core.update import transaction
    original = transaction.atomic_control
    fired = [False]
    def write_then_fail(path, value):
        original(path, value)
        if value.get("transaction") and not fired[0]:
            fired[0] = True
            raise OSError("directory flush failed after replace")
    monkeypatch.setattr(transaction, "atomic_control", write_then_fail)
    assert apply(lab).status == "rolled_back"
    assert reopen(lab[0]).recover().status == "rolled_back"


def test_rollback_record_failure_requires_recovery_not_false_success(lab, monkeypatch):
    from backend.core.update import transaction
    original = transaction.atomic_control
    def fail_final_record(path, value):
        if value.get("transaction", {}).get("state") == "ROLLED_BACK":
            raise OSError("disk full")
        original(path, value)
    monkeypatch.setattr(transaction, "atomic_control", fail_final_record)
    assert apply(lab, health_check=lambda *_: False).status == "recovery_required"
    assert (lab[0].root / "active/program").is_file()  # physically restored, not yet durably admitted
    with pytest.raises(UpdateError, match="UPDATE_RECOVERY_REQUIRED"):
        with lab[0].runtime_session():
            pytest.fail("pending rollback admitted")
    monkeypatch.setattr(transaction, "atomic_control", original)
    assert reopen(lab[0]).recover().status == "rolled_back"


def test_source_changed_between_intent_and_move_is_retained(lab):
    def changed(event):
        if event == "save_old_intent":
            (lab[0].root / "active/program").write_bytes(b"uncoordinated user change")
    assert apply(lab, checkpoint=changed).status == "recovery_required"
    assert (lab[0].root / "active/program").read_bytes() == b"uncoordinated user change"


def test_retained_old_payload_must_still_match_before_commit(lab):
    def probe(active, profile):
        report = inspect_health(active, profile)
        (profile.parent / "old/program").write_bytes(b"changed old payload")
        return report
    assert apply(lab, health_check=probe).status == "recovery_required"
    assert read_control(lab[0].root / "control/ledger.json")["transaction"]["state"] == "CHECK_NEW"


def test_unknown_durable_temp_file_is_never_adopted_or_deleted(lab):
    temporary = lab[0].root / "control/.record-unknown"
    temporary.write_bytes(b"do not adopt this as a ledger")
    assert reopen(lab[0]).recover().status == "idle"
    assert apply(lab).status == "committed"
    assert temporary.read_bytes() == b"do not adopt this as a ledger"


def test_build_inventory_order_is_not_a_spurious_platform_requirement(lab):
    store, source, _, keys, current = lab
    path = source / "build-manifest.json"
    value = json.loads(path.read_text())
    value["files"].reverse()
    path.write_text(json.dumps(value))
    package = archive_fixture(source, lab[2][2].parent, keys)
    assert apply((store, source, package, keys, current)).status == "committed"


def test_archive_cannot_supply_installer_ownership(lab):
    store, source, _, keys, current = lab
    (source / ".gitgo-install-owner").write_bytes(b"do not trust package ownership")
    refresh_build(source)
    package = archive_fixture(source, lab[2][2].parent, keys)
    with pytest.raises(UpdateError, match="UPDATE_PACKAGE_CONTRACT"):
        apply((store, source, package, keys, current))
    assert (store.root / "active/program").is_file()


def test_multiple_sequential_updates_retain_both_prior_payloads(lab):
    assert apply(lab).status == "committed"
    store, source, _, keys, current = lab
    first = read_control(store.root / "control/ledger.json")["transaction"]["id"]
    product_path = source / "product.json"
    product = json.loads(product_path.read_text())
    product["version"] = "1.0.0-alpha.4"
    product_path.write_text(json.dumps(product))
    refresh_build(source, version="1.0.0-alpha.4")
    package = archive_fixture(source, lab[2][2].parent, keys, version="1.0.0-alpha.4", sequence=3)
    assert apply((reopen(store), source, package, keys, current)).status == "committed"
    ledger = read_control(store.root / "control/ledger.json")
    assert ledger["current"]["version"] == "1.0.0-alpha.4"
    assert ledger["transaction"]["old"]["version"] == "1.0.0-alpha.3"
    assert (store.root / "transactions" / first / "old/program").is_file()
    assert len(list((store.root / "transactions").iterdir())) == 2


def test_transaction_parent_redirection_cannot_move_old_program_into_project(lab):
    store = lab[0]
    def redirect(event):
        if event == "save_old_intent":
            txn = read_control(store.root / "control/ledger.json")["transaction"]
            directory = store.root / "transactions" / txn["id"]
            directory.rename(directory.with_name("retained-" + directory.name))
            directory.symlink_to(store.protected_paths[0], target_is_directory=True)
    assert apply(lab, checkpoint=redirect).status == "recovery_required"
    assert (store.root / "active/program").is_file()
    assert not (store.protected_paths[0] / "old").exists()


def test_probe_profile_redirection_is_rejected_before_driver_call(lab):
    store = lab[0]
    def redirect(event):
        if event == "check_new_intent":
            txn = read_control(store.root / "control/ledger.json")["transaction"]
            profile = store.root / "transactions" / txn["id"] / "probe"
            profile.rename(profile.with_name("retained-probe"))
            profile.symlink_to(store.protected_paths[0], target_is_directory=True)
    result = apply(lab, checkpoint=redirect, health_check=lambda *_: pytest.fail("redirected profile reached driver"))
    assert result.status == "rolled_back"
    assert (store.root / "active/program").is_file()


def test_directory_sync_failure_after_old_rename_restores_from_durable_intent(lab, monkeypatch):
    from backend.core.update import transaction
    original = transaction.sync_directory
    fired = [False]
    def fail_once(path):
        if path == lab[0].root and not fired[0] and not (path / "active").exists():
            fired[0] = True
            raise OSError("directory flush failed after rename")
        original(path)
    monkeypatch.setattr(transaction, "sync_directory", fail_once)
    assert apply(lab).status == "rolled_back"
    assert fired[0]
    assert (lab[0].root / "active/program").is_file()


def test_publish_rename_failure_restores_old_without_deleting_new(lab, monkeypatch):
    from backend.core.update import transaction
    original = transaction.os.rename
    def failure(source, target):
        if source.name == "new" and target == lab[0].root / "active":
            raise OSError("publish failed")
        original(source, target)
    monkeypatch.setattr(transaction.os, "rename", failure)
    assert apply(lab).status == "rolled_back"
    ledger = read_control(lab[0].root / "control/ledger.json")
    directory = lab[0].root / "transactions" / ledger["transaction"]["id"]
    assert (directory / "new/product.json").is_file()
    assert (lab[0].root / "active/program").is_file()


def test_failed_restore_retains_old_and_recovers_after_filesystem_fixed(lab, monkeypatch):
    from backend.core.update import transaction
    original = transaction.os.rename
    def failure(source, target):
        if source.name == "old":
            raise OSError("restore failed")
        original(source, target)
    monkeypatch.setattr(transaction.os, "rename", failure)
    assert apply(lab, health_check=lambda *_: False).status == "recovery_required"
    ledger = read_control(lab[0].root / "control/ledger.json")
    directory = lab[0].root / "transactions" / ledger["transaction"]["id"]
    assert (directory / "old/program").is_file()
    assert (directory / "failed/product.json").is_file()
    assert not (lab[0].root / "active").exists()
    monkeypatch.setattr(transaction.os, "rename", original)
    assert reopen(lab[0]).recover().status == "rolled_back"


def test_channel_watermarks_do_not_reset_each_other(lab):
    assert apply(lab).status == "committed"
    store, source, _, keys, current = lab
    path = source / "product.json"
    value = json.loads(path.read_text())
    value["version"] = "1.0.0"
    path.write_text(json.dumps(value))
    refresh_build(source, version="1.0.0")
    package = archive_fixture(source, lab[2][2].parent, keys, channel="stable", version="1.0.0", sequence=1)
    assert apply((store, source, package, keys, current), policy=replace(POLICY, channel="stable")).status == "committed"
    marks = read_control(store.root / "control/ledger.json")["watermarks"]
    assert [(item["channel"], item["sequence"]) for item in marks] == [("preview", 2), ("stable", 1)]
    assert reopen(store).recover().status == "committed"


def test_runtime_held_by_another_process_blocks_switch_until_released(lab):
    store = lab[0]
    script = '''
import sys
from pathlib import Path
from backend.core.update.transaction import SimulationStore
store = SimulationStore(Path(sys.argv[1]), protected_paths=tuple(Path(p) for p in sys.argv[2:]))
with store.runtime_session():
    print("admitted", flush=True)
    sys.stdin.readline()
'''
    child = subprocess.Popen([sys.executable, "-B", "-c", script, str(store.root),
                              *(str(p) for p in store.protected_paths)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), "child runtime admission timed out"
            assert child.stdout.readline().strip() == "admitted"
        with pytest.raises(UpdateError, match="UPDATE_BUSY"):
            apply(lab)
    finally:
        try:
            child.communicate("release\n", timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()  # only this test-owned child, never an existing runtime
            child.communicate(timeout=5)
    assert child.returncode == 0
    assert apply(lab).status == "committed"


@pytest.mark.parametrize("name", ["operation.lock", "runtime.lock"])
def test_replaced_lock_cannot_split_authority_even_while_old_inode_is_held(lab, name):
    store = lab[0]
    path = store.root / "control" / name
    before = (store.root / "control/ledger.json").read_bytes()
    with lock_file(path, shared=True):
        path.rename(path.with_name("retained-" + name))
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with pytest.raises(UpdateError, match="UPDATE_CONTROL_INVALID"):
            apply(lab)
        with pytest.raises(UpdateError, match="UPDATE_CONTROL_INVALID"):
            reopen(store)
    assert (store.root / "control/ledger.json").read_bytes() == before
    assert not list((store.root / "transactions").iterdir())
    assert (store.root / "active/program").is_file()


def test_lock_acquisition_checks_opened_identity_not_only_prior_path_check(lab):
    path = lab[0].root / "control/runtime.lock"
    identity = lab[0].registration["lock_identities"]["runtime.lock"]
    path.rename(path.with_name("retained-runtime.lock"))
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with pytest.raises(UpdateError, match="UPDATE_LOCK_INVALID"):
        with lock_file(path, expected_identity=identity):
            pytest.fail("substituted lock was admitted")


@pytest.mark.parametrize("name", ["control", "transactions"])
def test_copied_control_or_transaction_parent_is_not_adopted(lab, name):
    store = lab[0]
    path = store.root / name
    retained = path.with_name("retained-" + name)
    path.rename(retained)
    shutil.copytree(retained, path, symlinks=True)
    with pytest.raises(UpdateError, match="UPDATE_CONTROL_INVALID"):
        reopen(store)
    assert store.recover().status == "recovery_required"
    assert (store.root / "active/program").is_file()


def test_inspecting_registration_cannot_change_captured_authority(lab):
    store = lab[0]
    view = store.registration
    view["lock_identities"]["runtime.lock"]["inode"] = 1
    view["data_compatibility"]["config_schema_version"] = 999
    assert store.registration["lock_identities"]["runtime.lock"]["inode"] != 1
    assert store.registration["data_compatibility"]["config_schema_version"] == 1
    assert apply(lab).status == "committed"


@pytest.mark.parametrize("driver", [None, True, "run a shell command", {}])
def test_missing_explicit_health_driver_is_rejected_before_switch(lab, driver):
    store = lab[0]
    before = (store.root / "control/ledger.json").read_bytes()
    with pytest.raises(UpdateError, match="UPDATE_HEALTH_DRIVER"):
        apply(lab, health_check=driver)
    assert (store.root / "control/ledger.json").read_bytes() == before
    assert not list((store.root / "transactions").iterdir())


def test_retention_limit_does_not_delete_or_adopt_orphans(lab):
    store = lab[0]
    for index in range(32):
        (store.root / "transactions" / ("orphan-" + str(index))).mkdir(mode=0o700)
    before = (store.root / "control/ledger.json").read_bytes()
    with pytest.raises(UpdateError, match="UPDATE_RETAIN_LIMIT"):
        apply(lab)
    assert len(list((store.root / "transactions").iterdir())) == 32
    assert (store.root / "control/ledger.json").read_bytes() == before
    assert (store.root / "active/program").is_file()
