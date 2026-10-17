from contextlib import contextmanager
from dataclasses import replace
import ctypes
from pathlib import Path, PureWindowsPath
import threading
import uuid

import pytest

from backend.core.sandbox_read import ObjectIdentity
from backend.core.storage.envelope import StorageEnvelope, FileEnvelope
from backend.core.storage.host_records import HOST_LEAF, HostAccount, HostInput, HostLayout, metadata_classes
from backend.core.storage.models import StorageBlocked
from backend.core.storage.native_windows_host import WindowsHostRegistry
from backend.core.storage.native_windows_journal import WindowsJournalLibrary
from backend.core.storage.native_windows_scope import StorageOwnershipError
from backend.core.storage.runtime_control import RuntimeControl, RuntimeInstallPlan, require_whole_tail
from backend.core.storage.runtime_installation import PinnedRuntime
from backend.core.storage.runtime_manifest import with_runtime_copies
from tests.test_storage_host_records import anchor
from tests.test_storage_runtime_installation import installation
from tests.test_storage_runtime_records import initial
from tests.test_storage_windows_tree import native, private_directory


@contextmanager
def runtime_case(native, temporary):
    scopes, trees, binaries = native
    journals = WindowsJournalLibrary(ctypes.CDLL(str(binaries/"gitgo-storage-journal.dll")))
    scope = scopes.open_scope()
    sid, _ = scope.account()
    profile, input_root = temporary/"profile", temporary/"input"
    private_directory(profile, sid)
    private_directory(input_root, sid)
    parent = scope.root(str(input_root))
    manifest = installation(trees, sid, parent, scope.identity(parent)[0], input_root)
    source = PinnedRuntime(trees, manifest.to_bytes(), manifest.sha256, sid).pin()
    envelope = StorageEnvelope(1, 4096, 2, 8*1024*1024, metadata_classes(2), (FileEnvelope("state", 65536, 2),))
    layout = HostLayout(with_runtime_copies(envelope, manifest.content), manifest.content)
    registry = WindowsHostRegistry(scopes, trees, journals, layout)
    # Only this internal fixture selects a temporary account profile. The public
    # installer obtains the actual native account and has no locator override.
    account = HostAccount(sid, str(profile))
    owner = registry.owner()
    try:
        owner._attempted = True
        parent, identity = owner._parent(account, create=True)
        owner._bootstrap_at(account, parent, identity, HostInput.from_manifest(manifest))
        yield registry, owner, account, source
    finally:
        for retained in list(registry._owners.values()): assert retained.close()
        source.close()
        scope.close()
        assert journals.retained_journals == 0


def reopen(registry, account):
    owner = registry.owner()
    owner._attempted = True
    parent, identity = owner._parent(account, create=False)
    root = owner._scope.open(HOST_LEAF, parent, directory=True)
    owner._verify(account, identity, root)
    return owner


@pytest.mark.parametrize("field", ["host_binding", "source_manifest", "content_contract"])
def test_installation_plan_must_match_exact_host_and_charged_input(field):
    host = anchor()
    plan = RuntimeInstallPlan.for_host(host, 0, str(uuid.uuid4()))
    with pytest.raises(StorageBlocked):
        replace(plan, **{field: "0"*64}).require_host(host)


@pytest.mark.parametrize("field,value", [("slot", True), ("slot", 2), ("generation", "unknown"), ("host_binding", "0")])
def test_installation_plan_refuses_unbounded_or_ambiguous_inputs(field, value):
    plan = RuntimeInstallPlan.for_host(anchor(), 0, str(uuid.uuid4()))
    with pytest.raises(StorageBlocked): replace(plan, **{field: value})


def test_target_description_binds_canonical_location_native_identity_and_source_content():
    host = anchor()
    plan = RuntimeInstallPlan.for_host(host, 0, str(uuid.uuid4()))
    target = plan.target_manifest(host, ObjectIdentity("win32", host.root_identity.volume, 999))
    assert target.root == str(PureWindowsPath(host.account.root)/"runtimes"/plan.generation)
    assert target.content == host.layout.content and target.sha256 != host.source.manifest_digest
    with pytest.raises(StorageBlocked): plan.target_manifest(host, host.root_identity)
    with pytest.raises(StorageBlocked): plan.target_manifest(host, ObjectIdentity("win32", 99, 999))
    entry = plan.initial_entry()
    plan.require_entry(entry)
    with pytest.raises(StorageBlocked): plan.require_entry(dict(entry, source_manifest="0"*64))


def test_complete_journal_tail_is_required_before_new_work():
    view = initial()
    require_whole_tail(replace(view, head=replace(view.head, sequence=2**64-33)))
    with pytest.raises(StorageBlocked):
        require_whole_tail(replace(view, head=replace(view.head, sequence=2**64-32)))


def test_native_runtime_reservation_issues_ticket_after_commit_and_preserves_two_copy_debt(native, tmp_path_factory):
    with runtime_case(native, tmp_path_factory) as (registry, owner, account, source):
        first = RuntimeControl(owner)
        ticket = first.reserve(source)
        first.require_ticket(ticket)
        with pytest.raises(StorageBlocked): first.require_ticket(replace(ticket))
        assert owner._read_slot(63).head.state.value()["entries"][0] == ticket.plan.initial_entry()
        assert not any((Path(account.root)/"runtimes").iterdir())
        with pytest.raises(StorageBlocked): first.reserve(source)
        assert owner.close()
        second_owner = reopen(registry, account)
        second = RuntimeControl(second_owner).reserve(source)
        assert second.plan.slot == 1 and second.plan.generation != ticket.plan.generation
        assert second_owner.close()
        full = reopen(registry, account)
        with pytest.raises(StorageBlocked, match="remain charged"): RuntimeControl(full).reserve(source)
        assert full.close()


def test_lost_committed_reservation_issues_no_ticket_and_is_not_replayed(native, tmp_path_factory, monkeypatch):
    with runtime_case(native, tmp_path_factory) as (registry, owner, account, source):
        coordinator = RuntimeControl(owner)
        original = registry.journals._calls["update"]
        def lost(*args):
            assert original(*args) == 0
            raise RuntimeError("lost runtime reservation")
        with monkeypatch.context() as context:
            context.setitem(registry.journals._calls, "update", lost)
            with pytest.raises(RuntimeError, match="lost runtime reservation"): coordinator.reserve(source)
        assert coordinator._ticket is None and not owner._ready and registry.journals.retained_journals == 1
        assert owner.close()
        resumed = reopen(registry, account)
        assert resumed._read_slot(63).head.state.value()["entries"][0]["phase"] == "RESERVED"
        assert RuntimeControl(resumed).reserve(source).plan.slot == 1
        assert resumed.close()


def test_input_transfer_invalidates_original_wrapper_without_another_native_pin(native, tmp_path_factory):
    with runtime_case(native, tmp_path_factory) as (registry, owner, _, source):
        count = registry.trees.retained_trees
        moved = owner.take_input(source)
        assert moved is not source and moved.ready and not source.ready
        assert registry.trees.retained_trees == count
        source.close()
        assert moved.read("lib/module.py", 0, 6) == b"module"
        owner.require_input(moved)
        with pytest.raises(StorageBlocked): owner.require_input(source)
        assert owner.close() and not moved.ready


def test_unknown_input_transfer_stays_owned_and_disables_host_admission(native, tmp_path_factory, monkeypatch):
    with runtime_case(native, tmp_path_factory) as (registry, owner, account, source):
        original = PinnedRuntime._move_into
        def lost(self, destination):
            original(self, destination)
            raise RuntimeError("lost transferred pins")
        with monkeypatch.context() as context:
            context.setattr(PinnedRuntime, "_move_into", lost)
            with pytest.raises(RuntimeError, match="lost transferred pins"): owner.take_input(source)
        assert not source.ready and not owner._ready and registry.trees.retained_trees == 2
        with pytest.raises(StorageOwnershipError) as caught: reopen(registry, account)
        assert caught.value.native_error == 33
        for other in list(registry._owners.values()):
            if other is not owner: assert other.close()
        assert owner.close() and registry.trees.retained_trees == 0


def test_foreign_thread_cannot_read_move_or_release_input_pins(native, tmp_path_factory):
    with runtime_case(native, tmp_path_factory) as (_, owner, _, source):
        errors = []
        def foreign():
            for action in (lambda: source.read("python.exe", 0, 1), source.close, lambda: owner.take_input(source)):
                try: action()
                except StorageBlocked as error: errors.append(str(error))
        thread = threading.Thread(target=foreign)
        thread.start();thread.join(timeout=10)
        assert not thread.is_alive() and len(errors) == 3
        assert source.ready and source.read("python.exe", 0, 1) == b"e"


def test_native_leaf_retirement_invalidates_wrapper_readiness(native, tmp_path_factory):
    with runtime_case(native, tmp_path_factory) as (_, owner, _, source):
        source._owner.close()
        assert not source.ready
        with pytest.raises(StorageBlocked): owner.take_input(source)


@pytest.mark.parametrize("kind", ["file", "directory", "reserved-root"])
def test_unaccounted_or_premature_runtime_members_block_new_admission(native, tmp_path_factory, kind):
    with runtime_case(native, tmp_path_factory) as (registry, owner, account, source):
        if kind == "reserved-root":
            ticket = RuntimeControl(owner).reserve(source)
            name = ticket.plan.generation
            assert owner.close()
            owner = reopen(registry, account)
        else: name = str(uuid.uuid4())
        target = Path(account.root)/"runtimes"/name
        if kind == "file": target.write_bytes(b"unaccounted")
        else: private_directory(target, account.sid)
        before = owner._read_slot(63).raw
        with pytest.raises(StorageBlocked): RuntimeControl(owner).reserve(source)
        assert (Path(account.root)/"slot-0063.json").read_bytes() == before
        assert target.exists() and owner.close()


@pytest.mark.parametrize("operation", ["tree_inventory", "install_absent"])
def test_unknown_namespace_observation_never_issues_runtime_capacity(native, tmp_path_factory, monkeypatch, operation):
    with runtime_case(native, tmp_path_factory) as (registry, owner, account, source):
        before = owner._read_slot(63).raw
        coordinator = RuntimeControl(owner)
        original = registry.trees._calls[operation]
        def lost(*args):
            assert original(*args) == 0
            raise RuntimeError("lost namespace observation")
        with monkeypatch.context() as context:
            context.setitem(registry.trees._calls, operation, lost)
            with pytest.raises(RuntimeError, match="lost namespace observation"): coordinator.reserve(source)
        assert coordinator._ticket is None and not owner._ready
        assert (Path(account.root)/"slot-0063.json").read_bytes() == before
        assert owner.close()
