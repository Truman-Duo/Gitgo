"""Explicit, recoverable transition from an immutable legacy bootstrap.

Run from trusted source or a separately verified complete release, after the
old application's instances have exited. Never rewrite the old bootstrap in
place or pretend that a new payload retroactively upgrades the old launcher.
All old installation bytes and original data generations remain recoverable.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import uuid

from .archive import ExtractionLimits, _extract_archive
from .checker import CurrentInstallation, check_for_update
from .contracts import verify_bundle_contract
from .data_transition import ENGINE_VERSION, prepare_transition, resource_bindings
from .data_upgrade import DataUpgrade, storage_maintenance
from .installation import open_managed, initialize_managed
from .persistence import atomic_control, read_control, filesystem_identity, private_namespace, open_readonly
from .staging import _verify_file
from .transaction import _watermark, _watermark_wire
from .validation import fields, reject, text


def _private(path):
    if os.name == "nt":
        from .windows_storage import create_private_directory
        create_private_directory(path)
    else: path.mkdir(mode=0o700)


def _move(source, target, expected, *, callback=None):
    if target.exists() or target.is_symlink(): reject("UPDATE_REPAIR_REQUIRED", "Repair destination is occupied")
    if os.name == "nt":
        from .windows_storage import rename_owned_directory
        return rename_owned_directory(source, target, expected, callback=callback)
    else:
        if filesystem_identity(source) != expected:
            reject("UPDATE_REPAIR_REQUIRED", "Repair directory identity changed")
        os.rename(source, target)
        if callback is not None:
            with private_namespace(target): return callback()


def _pointer(root):
    return root.parent / ("." + root.name + ".gitgo-repair.json")


def _read(root):
    pointer = fields(read_control(_pointer(root)), {"schema_version", "id", "root", "work"})
    if type(pointer["schema_version"]) is not int or pointer["schema_version"] != 1 or pointer["root"] != str(root):
        reject("UPDATE_REPAIR_REQUIRED", "Repair pointer identity is invalid")
    text(pointer["id"], r"[0-9a-f]{32}", 32)
    work = root.parent / ("." + root.name + ".gitgo-repair-" + pointer["id"])
    if pointer["work"] != str(work) or work != work.resolve(strict=True):
        reject("UPDATE_REPAIR_REQUIRED", "Repair work directory is redirected")
    value = fields(read_control(work / "repair.json"), {"schema_version", "id", "root", "state", "old_identity",
        "new_identity", "registration", "old_version", "new_version", "resources"})
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["id"] != pointer["id"] or value["root"] != str(root)
            or value["state"] not in {"PREPARED", "QUIESCED", "OLD_MOVED", "NEW_PROVISIONED", "DATA_PUBLISHED", "COMMITTED", "RESTORED"}):
        reject("UPDATE_REPAIR_REQUIRED", "Repair journal identity is invalid")
    return work, value


def _save(work, value, state):
    value["state"] = state
    atomic_control(work / "repair.json", value)


def recover(root, *, locks_held=False, checkpoint=lambda _: None):
    """No new downloads, inferred directories, replayed approvals or deletes."""
    root = Path(root)
    work, value = _read(root)
    with private_namespace(work):
        if root.exists() and filesystem_identity(root) not in (value["old_identity"], value["new_identity"]):
            reject("UPDATE_REPAIR_REQUIRED", "Repair refuses an unrelated installation directory")
        if value["state"] == "RESTORED":
            store = open_managed(root)
            _pointer(root).unlink()
            return {"status": "rolled_back", "version": store._ledger()["current"]["version"]}
        if value["state"] == "COMMITTED":
            if filesystem_identity(root) != value["new_identity"]:
                reject("UPDATE_REPAIR_REQUIRED", "Committed installation identity changed")
            store = open_managed(root)
            marker = root / "control/repair-pending.json"
            if marker.exists(): marker.unlink()
            _pointer(root).unlink()
            return {"status": "committed", "version": store._ledger()["current"]["version"]}
        resources = {name: Path(path) for name, path in value["resources"].items()}
        upgrade = DataUpgrade(work / value["id"], value["id"], resources)
        # An interrupted preparation never changed originals or blocked the
        # old app. It may have legitimately resumed and written new data.
        if value["state"] != "PREPARED":
            upgrade.recover(committed=False, locks_held=locks_held)
        checkpoint("repair_data_restored")
        old = work / "old-installation"
        if root.exists() and filesystem_identity(root) == value["new_identity"]:
            _move(root, work / "failed-installation", value["new_identity"])
        def restore():
            if filesystem_identity(root) != value["old_identity"]:
                reject("UPDATE_REPAIR_REQUIRED", "Repair refuses an unrelated installation directory")
            atomic_control(root / "control/registration.json", value["registration"])
            store = open_managed(root)
            if store.recover().status not in {"idle", "committed", "rolled_back"}:
                reject("UPDATE_REPAIR_REQUIRED", "Original installation could not be revalidated")
            _save(work, value, "RESTORED")
            _pointer(root).unlink()
            return {"status": "rolled_back", "version": value["old_version"]}
        if not root.exists():
            if not old.exists(): reject("UPDATE_REPAIR_REQUIRED", "Original installation identity is missing")
            return _move(old, root, value["old_identity"], callback=restore)
        with private_namespace(root): return restore()


def repair(root, raw, signature, archive, *, checkpoint=lambda _: None, clock=lambda: datetime.now(timezone.utc)):
    root, archive = Path(root), Path(archive)
    if not root.is_absolute() or root != root.resolve(strict=True):
        reject("UPDATE_REPAIR_REQUIRED", "Repair requires an explicit canonical installed root")
    if _pointer(root).exists():
        reject("UPDATE_REPAIR_REQUIRED", "Recover the previous updater replacement before trying another")
    store = open_managed(root)
    from .release_policy import installed_release_policy
    policy = installed_release_policy(store)
    if policy is None: reject("UPDATE_TRUST_MISSING", "Legacy installation has no pinned publisher trust")
    manifest = policy.verifier.verify(raw, signature)
    registration = store.registration
    identity = store._ledger()["current"]
    old_watermarks = store._ledger()["watermarks"]
    current = CurrentInstallation(registration["product_id"], identity["version"], registration["platform"],
        registration["architecture"], ENGINE_VERSION, store.data_compatibility(), True)
    previous = next((_watermark(item) for item in store._ledger()["watermarks"] if item["channel"] == manifest.channel), None)
    checked = check_for_update(policy.channel(manifest.channel), current, policy.verifier,
        lambda url, _: raw if url == policy.channel(manifest.channel).manifest_url else signature,
        now=clock(), previous=previous)
    if checked.candidate is None: reject("UPDATE_NO_CANDIDATE", "Updater repair requires a newer signed release")
    repair_id = uuid.uuid4().hex
    work = root.parent / ("." + root.name + ".gitgo-repair-" + repair_id)
    _private(work)
    with private_namespace(work), ExitStack() as data_scope:
        # Copy and reverify before executing any candidate. Never adopt a
        # caller-supplied extracted directory, health flag or new trust root.
        copied = work / "archive.verified"
        descriptor = open_readonly(archive)
        with os.fdopen(descriptor, "rb") as incoming, copied.open("x+b") as outgoing:
            _verify_file(incoming, manifest, lambda: False)
            shutil.copyfileobj(incoming, outgoing); outgoing.flush(); os.fsync(outgoing.fileno())
            _verify_file(outgoing, manifest, lambda: False)
            seed = _extract_archive(outgoing, manifest.archive_format, work / "payload", work, ExtractionLimits(), lambda: False)
        verify_bundle_contract(seed, manifest)
        from .release_policy import POLICY_FILE, read_policy_file
        if read_policy_file(seed / POLICY_FILE) != read_policy_file(root / "bootstrap" / POLICY_FILE):
            reject("UPDATE_TRUST_CHANGED", "Updater repair cannot replace the installation's pinned release trust")
        from .health import frozen_health_check
        from .transaction import HealthReport
        from dataclasses import asdict
        profile = work / "health"; _private(profile)
        report = frozen_health_check(seed, profile)
        expected = {key: getattr(manifest, key) for key in ("product_id", "version", "platform", "architecture", "source_commit", "source_tree")}
        if type(report) is not HealthReport or asdict(report) != {**expected, "passed": True}:
            reject("UPDATE_HEALTH_FAILED", "Repair candidate startup did not match its release")
        verify_bundle_contract(seed, manifest)
        blank = work / "new-installation"; _private(blank)
        data_directory = work / repair_id; _private(data_directory)
        resources = resource_bindings(current.data_compatibility, manifest.data_compatibility)
        data_scope.enter_context(storage_maintenance(resources))
        with store._operation():
            if store._ledger()["current"] != identity:
                reject("UPDATE_SELECTION_STALE", "Installation changed before updater replacement")
            upgrade = prepare_transition(store, data_directory, current.data_compatibility, manifest.data_compatibility,
                seed, resources=resources)
            value = {"schema_version": 1, "id": repair_id, "root": str(root), "state": "PREPARED",
                "old_identity": filesystem_identity(root), "new_identity": filesystem_identity(blank),
                "registration": registration, "old_version": current.version, "new_version": manifest.version,
                "resources": {name: str(path) for name, path in resources.items()}}
            _save(work, value, "PREPARED")
            atomic_control(_pointer(root), {"schema_version": 1, "id": repair_id, "root": str(root), "work": str(work)})
            checkpoint("repair_prepared")
            # Old immutable code rejects this explicit non-runnable repair
            # record. Its complete original registration remains in the journal.
            atomic_control(root / "control/registration.json", {"schema_version": 0,
                "state": "updater_replacement", "repair_id": repair_id, "work": str(work)})
            _save(work, value, "QUIESCED")
            checkpoint("repair_quiesced")
        try:
            # No running image, namespace pin or application lease remains in
            # the old root. Its registration is non-runnable until recovery.
            _move(root, work / "old-installation", value["old_identity"])
            _save(work, value, "OLD_MOVED"); checkpoint("repair_old_moved")
            def provision():
                replacement = initialize_managed(root, seed, protected_paths=store.protected_paths,
                    _repair={"schema_version": 1, "id": repair_id, "work": str(work)})
                _save(work, value, "NEW_PROVISIONED"); checkpoint("repair_new_provisioned")
                upgrade.publish(checkpoint=checkpoint)
                _save(work, value, "DATA_PUBLISHED"); checkpoint("repair_data_published")
                ledger = replacement._ledger()
                mark = _watermark_wire(checked.watermark)
                ledger["watermarks"] = sorted([item for item in old_watermarks if item["channel"] != mark["channel"]] + [mark], key=lambda item: item["channel"])
                replacement._save(ledger)
                _save(work, value, "COMMITTED"); checkpoint("repair_committed")
                (root / "control/repair-pending.json").unlink()
                _pointer(root).unlink()
                return {"status": "committed", "version": manifest.version, "backup": str(work)}
            return _move(blank, root, value["new_identity"], callback=provision)
        except Exception:
            return recover(root, locks_held=True)
