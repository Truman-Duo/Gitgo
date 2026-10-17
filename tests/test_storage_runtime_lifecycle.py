"""Native runtime lease, atomic activation and exact cleanup acceptance."""
from dataclasses import replace
from pathlib import Path

import pytest

from backend.core.storage.models import StorageBlocked
from backend.core.storage.native_windows_scope import StorageOwnershipError
from backend.core.storage.runtime_control import RuntimeControl
from backend.core.storage.runtime_installation import PinnedRuntime
from backend.core.storage.runtime_launch import RuntimeInstallation, RuntimeCleanup
from backend.core.storage.runtime_lifecycle import activate, select, cancel_unstarted, release_capacity, retire_ready
from tests.test_storage_runtime_control import reopen, runtime_case
from tests.test_storage_runtime_launch import packaged_input, launch_case, fixture_entry, finish
from tests.test_storage_windows_tree import native


def new_work(registry, account, manifest, processes, *, cleanup_slot=None):
    host = reopen(registry, account)
    source = host._new(PinnedRuntime, registry.trees, manifest.to_bytes(), manifest.sha256, account.sid)
    source.pin()
    control = RuntimeControl(host)
    if cleanup_slot is None:
        launch = RuntimeInstallation(control, control.reserve(source))
    else:
        launch = RuntimeCleanup(control, control.reserve_cleanup(source, cleanup_slot))
    return registry, host, account, source, processes, launch


def run(monkeypatch, case, *, fault=0):
    with monkeypatch.context() as patch:
        observed = fixture_entry(patch, case, fault=fault)
        case[-1].start(case[3], case[4])
        result = finish(case[-1])
        assert len(observed) == 1
        return result


def promote(registry, account, slot):
    host = reopen(registry, account)
    try:
        return activate(RuntimeControl(host), slot)
    finally:
        assert host.close()


def refund(registry, account, slot):
    host = reopen(registry, account)
    try:
        release_capacity(RuntimeControl(host), slot)
        assert host._read_slot(63).head.state.value()["entries"][slot] is None
    finally:
        assert host.close()


def test_activation_switch_keeps_old_copy_charged_until_native_cleanup_and_refund(
        native, tmp_path_factory, packaged_input, monkeypatch):
    with launch_case(native, tmp_path_factory, packaged_input) as first:
        registry, _, account, _, processes, _ = first
        old = run(monkeypatch, first)
        selected = promote(registry, account, 0)
        assert selected.generation == old.generation and selected.epoch == 1
        second = new_work(registry, account, packaged_input, processes)
        new = run(monkeypatch, second)
        active = promote(registry, account, 1)
        assert active.generation == new.generation and active.epoch == 2
        host = reopen(registry, account)
        state = host._read_slot(63).head.state.value()
        assert [entry["phase"] for entry in state["entries"]] == ["RETIRING", "ACTIVE"]
        before = host._read_slot(63).raw
        with pytest.raises(StorageBlocked, match="committed cleanup"):
            release_capacity(RuntimeControl(host), 0)
        assert (Path(account.root)/"slot-0063.json").read_bytes() == before
        assert host.close()
        cleanup = new_work(registry, account, packaged_input, processes, cleanup_slot=0)
        assert run(monkeypatch, cleanup).phase == "ABSENT"
        assert not Path(old.manifest.root).exists() and Path(new.manifest.root).is_dir()
        host = reopen(registry, account)
        assert host._read_slot(63).head.state.value()["entries"][0] is not None
        assert host.close()
        refund(registry, account, 0)
        reused = new_work(registry, account, packaged_input, processes)
        assert reused[-1]._ticket.plan.slot == 0 and reused[-1]._ticket.plan.generation != old.generation
        assert run(monkeypatch, reused).phase == "READY"


def test_selected_runtime_lease_blocks_new_installation_and_survives_gate_handoff(
        native, tmp_path_factory, packaged_input, monkeypatch):
    with launch_case(native, tmp_path_factory, packaged_input) as case:
        registry, _, account, _, _, _ = case
        installed = run(monkeypatch, case)
        reference = promote(registry, account, 0)
        reader = reopen(registry, account)
        chosen = select(RuntimeControl(reader))
        assert chosen.require_live(reference) == installed.manifest
        with pytest.raises(StorageBlocked): chosen.require_live(replace(reference, epoch=2))
        reader.handoff_gate()
        assert chosen.require_live() == installed.manifest
        writer = reopen(registry, account)
        source = writer._new(PinnedRuntime, registry.trees, packaged_input.to_bytes(), packaged_input.sha256, account.sid)
        source.pin()
        before = writer._read_slot(63).raw
        with pytest.raises(StorageOwnershipError) as failure: RuntimeControl(writer).reserve(source)
        assert failure.value.native_error == 33
        assert (Path(account.root)/"slot-0063.json").read_bytes() == before
        assert writer.close() and chosen.require_live() == installed.manifest
        assert reader.close()
        with pytest.raises(StorageBlocked): chosen.require_live()


@pytest.mark.parametrize("fault", [1, 5])
def test_interrupted_cleanup_remains_charged_and_later_cleanup_verifies_remaining_members(
        native, tmp_path_factory, packaged_input, monkeypatch, fault):
    with launch_case(native, tmp_path_factory, packaged_input) as case:
        registry, _, account, _, processes, _ = case
        if fault == 5:
            assert run(monkeypatch, case).phase == "READY"
            host = reopen(registry, account)
            retire_ready(RuntimeControl(host), 0)
            assert host.close()
        else:
            assert run(monkeypatch, case, fault=2).phase == "RETIRING"
        failed = new_work(registry, account, packaged_input, processes, cleanup_slot=0)
        assert run(monkeypatch, failed, fault=fault).phase == "RETIRING"
        host = reopen(registry, account)
        old = host._read_slot(63).head.state.value()["entries"][0]
        assert old["worker"]["phase"] == "RETIRED" and old["root"] is not None
        assert host.close()
        retry = new_work(registry, account, packaged_input, processes, cleanup_slot=0)
        assert run(monkeypatch, retry).phase == "ABSENT"
        assert retry[-1]._event["launch"] != old["worker"]["launch"]
        refund(registry, account, 0)


def test_missing_root_creation_identity_stays_quarantined_without_adoption_or_deletion(
        native, tmp_path_factory, packaged_input, monkeypatch):
    with launch_case(native, tmp_path_factory, packaged_input) as case:
        registry, _, account, _, processes, launch = case
        assert run(monkeypatch, case, fault=4).phase == "RETIRING"
        path = Path(account.root)/"runtimes"/launch._ticket.plan.generation
        assert path.is_dir() and list(path.iterdir()) == []
        before = (Path(account.root)/"slot-0063.json").read_bytes()
        with pytest.raises(StorageBlocked):
            new_work(registry, account, packaged_input, processes, cleanup_slot=0)
        assert (Path(account.root)/"slot-0063.json").read_bytes() == before
        for retained in list(registry._owners.values()): assert retained.close()
        assert path.is_dir() and list(path.iterdir()) == []
        host = reopen(registry, account)
        value = host._read_slot(63).head.state.value()["entries"][0]
        assert value["root"] is None and value["worker"]["phase"] == "RETIRED"
        with pytest.raises(StorageBlocked): release_capacity(RuntimeControl(host), 0)
        assert host.close()


def test_never_started_reservation_needs_cleanup_receipt_before_capacity_reuse(
        native, tmp_path_factory, packaged_input, monkeypatch):
    with launch_case(native, tmp_path_factory, packaged_input) as case:
        registry, host, account, source, processes, _ = case
        source.close()
        assert host.close()
        host = reopen(registry, account)
        cancel_unstarted(RuntimeControl(host), 0)
        assert host._read_slot(63).head.state.value()["entries"][0]["phase"] == "RETIRING"
        assert host.close()
        cleanup = new_work(registry, account, packaged_input, processes, cleanup_slot=0)
        assert run(monkeypatch, cleanup).phase == "ABSENT"
        refund(registry, account, 0)


def test_unactivated_input_never_supplies_a_selected_storage_runtime(native, tmp_path_factory):
    with runtime_case(native, tmp_path_factory) as (registry, host, account, _):
        before = host._read_slot(63).raw
        with pytest.raises(StorageBlocked, match="active installed"): select(RuntimeControl(host))
        assert (Path(account.root)/"slot-0063.json").read_bytes() == before
        assert host.close()


def test_lost_activation_commit_returns_no_reference_and_fresh_selection_revalidates(
        native, tmp_path_factory, packaged_input, monkeypatch):
    with launch_case(native, tmp_path_factory, packaged_input) as case:
        registry, _, account, _, _, _ = case
        installed = run(monkeypatch, case)
        host = reopen(registry, account)
        original = registry.journals._calls["update"]
        def lost(*args):
            assert original(*args) == 0
            raise RuntimeError("lost activation")
        with monkeypatch.context() as patch:
            patch.setitem(registry.journals._calls, "update", lost)
            with pytest.raises(RuntimeError, match="lost activation"): activate(RuntimeControl(host), 0)
        assert not host._ready and registry.journals.retained_journals == 1
        assert host.close()
        host = reopen(registry, account)
        chosen = select(RuntimeControl(host))
        assert chosen.reference.epoch == 1 and chosen.require_live() == installed.manifest
        assert host.close()
