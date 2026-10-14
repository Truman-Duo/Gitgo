"""Real Darwin UUID reads plus explicit device-renumbering fault injection.

This is not a real reboot test and does not amend an existing registration.
"""
import os

import pytest

from backend.core.update import persistence
from backend.core.update.installation import open_managed
from backend.core.update.validation import UpdateError
from tests.test_managed_installation import managed

pytestmark = pytest.mark.skipif(not persistence._darwin(), reason="Requires real Darwin volume attributes")


def test_real_uuid_is_full_nonzero_and_bound_to_open_file(managed):
    store = managed[0]
    path = store.root / "control/runtime.lock"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        identity = persistence.descriptor_identity(fd)
        persistence.validate_device(identity["device"])
        assert identity == persistence.filesystem_identity(path)
        assert identity["device"] == store.registration["device"]
    finally: os.close(fd)


def test_device_renumber_preserves_registration_lock_and_preparation_authority(managed, monkeypatch):
    store = managed[0]
    ledger_before = (store.root / "control/ledger.json").read_bytes()
    # Model the observed reboot change consistently for both pathname and FD
    # stats. Only the mount's st_dev changes; UUID/inode/bytes remain genuine.
    def renumber(call):
        def wrapped(*args, **kwargs):
            info = call(*args, **kwargs)
            values = list(info); values[2] += 100
            return os.stat_result(values)
        return wrapped
    for name in ("stat", "lstat", "fstat"):
        monkeypatch.setattr(os, name, renumber(getattr(os, name)))
    reopened = open_managed(store.root)
    assert reopened.registration == store.registration
    for name in reopened.LOCK_NAMES:
        with persistence.lock_file(store.root / "control" / name,
                expected_identity=reopened.registration["lock_identities"][name]): pass
    assert persistence.filesystem_identity(store.root / "control/preparation") == reopened.registration["preparation_directory_identity"]
    assert (store.root / "control/ledger.json").read_bytes() == ledger_before


def test_other_volume_uuid_is_rejected_even_with_same_inode_and_bytes(managed, monkeypatch):
    store = managed[0]
    ledger_before = (store.root / "control/ledger.json").read_bytes()
    monkeypatch.setattr(persistence, "_volume_uuid", lambda fd: "darwin-volume:11111111-2222-3333-4444-555555555555")
    with pytest.raises(UpdateError, match="UPDATE_CONTROL_INVALID"): open_managed(store.root)
    with pytest.raises(UpdateError, match="UPDATE_LOCK_INVALID"):
        with persistence.lock_file(store.root / "control/runtime.lock",
                expected_identity=store.registration["lock_identities"]["runtime.lock"]): pass
    assert (store.root / "control/ledger.json").read_bytes() == ledger_before


def test_uuid_unavailable_has_no_temporary_device_fallback(managed, monkeypatch):
    store = managed[0]
    def unavailable(fd): persistence.reject("UPDATE_VOLUME_ID_UNAVAILABLE", "Test query failure")
    monkeypatch.setattr(persistence, "_volume_uuid", unavailable)
    with pytest.raises(UpdateError, match="UPDATE_VOLUME_ID_UNAVAILABLE"): open_managed(store.root)


def test_old_registration_is_not_rewritten_or_adopted(managed):
    store = managed[0]
    path = store.root / "control/registration.json"
    value = persistence.read_control(path)
    value.update(schema_version=4, bootstrap_api=5)
    persistence.atomic_control(path, value)
    before = path.read_bytes()
    with pytest.raises(UpdateError, match="UPDATE_CONTROL_INVALID"): open_managed(store.root)
    assert path.read_bytes() == before
