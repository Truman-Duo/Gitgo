"""Reboot-bound state-machine tests with disposable products, not Mac reboot QA."""
import os
import sys
from types import SimpleNamespace

import pytest

from backend.core.installation_lease import retain_descriptor
from backend.core.update import deferred, handoff, UpdateError
from backend.core.update.persistence import read_control, atomic_control, lock_file
from backend.core.update.tree import tree_digest
from tests.test_update_handoff import lab, queue, consume
from tests.test_update_manifest import signing
from tests.test_update_manual_rollback import upgraded


BOOT_A = "11111111-1111-4111-8111-111111111111"
BOOT_B = "22222222-2222-4222-8222-222222222222"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Managed POSIX ownership")


@pytest.fixture
def mac_lab(tmp_path_factory, monkeypatch, signing):
    # Explicit Mac product fixtures, not rewritten live registration authority.
    yield from lab.__wrapped__(tmp_path_factory, monkeypatch, signing, SimpleNamespace(param="macos"))


@pytest.fixture
def reboot_lab(mac_lab, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")  # State wiring only.
    monkeypatch.setattr(handoff, "strategy", lambda _: "after_reboot")
    monkeypatch.setattr(deferred, "boot_session", lambda: BOOT_A)
    return mac_lab


def confirm(lab, monkeypatch):
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        assert queue(lab)["apply_strategy"] == "after_reboot"
        return consume(lab, nonce, cleanup={"complete": False})


def test_confirm_and_repeated_launch_in_same_boot_never_replace(reboot_lab, monkeypatch):
    lab = reboot_lab
    assert confirm(lab, monkeypatch).status == "restart_required"
    before = lab.store._ledger(); network = list(lab.calls)
    for _ in range(2):
        assert deferred._consume_after_reboot(lab.store).status == "restart_required"
    assert lab.store._ledger() == before and lab.calls == network
    assert tree_digest(lab.store.root / "active") == lab.before
    status = lab.service.status("preview")
    assert status["apply_strategy"] == "after_reboot" and status["pending_restart"]["version"] == "1.0.0-alpha.3"
    assert status["can_request_apply"] is False and status["can_request_rollback"] is False
    assert "boot_session" not in str(status) and BOOT_A not in str(status)


def test_different_boot_reverifies_and_consumes_exact_update_once(reboot_lab, monkeypatch):
    lab = reboot_lab
    confirm(lab, monkeypatch)
    calls = list(lab.calls)
    monkeypatch.setattr(deferred, "boot_session", lambda: BOOT_B)
    assert deferred._consume_after_reboot(lab.store).status == "committed"
    assert lab.store._ledger()["current"]["version"] == "1.0.0-alpha.3"
    assert lab.calls == calls  # No startup network fetch.
    assert deferred._consume_after_reboot(lab.store) is None
    assert deferred.pending_status(lab.store) is None


def test_wrong_or_cancelled_pending_selection_cannot_remove_request(reboot_lab, monkeypatch):
    lab = reboot_lab
    confirm(lab, monkeypatch)
    pending = deferred.pending_status(lab.store)
    with pytest.raises(UpdateError, match="UPDATE_SELECTION_STALE"):
        lab.service.cancel_pending("f" * 64, "1" * 32)
    lab.service.cancel("2" * 32)
    with pytest.raises(UpdateError, match="UPDATE_CANCELLED"):
        lab.service.cancel_pending(pending["request_id"], "2" * 32)
    assert deferred.pending_status(lab.store) == pending
    assert lab.service.cancel_pending(pending["request_id"], "3" * 32)["status"] == "cancelled"
    assert deferred.pending_status(lab.store) is None
    assert tree_digest(lab.store.root / "active") == lab.before


def test_pending_request_blocks_changed_checks_downloads_and_confirmation(reboot_lab, monkeypatch):
    lab = reboot_lab
    confirm(lab, monkeypatch)
    calls = list(lab.calls)
    for method, args in ((lab.service.check, ("preview", "1" * 32)),
                         (lab.service.download, ("preview", lab.selected, "2" * 32)),
                         (lab.service.request_apply, ("preview", lab.selected, "3" * 32))):
        with pytest.raises(UpdateError, match="UPDATE_DEFERRED_EXISTS"): method(*args)
    assert lab.calls == calls and tree_digest(lab.store.root / "active") == lab.before


def test_changed_signed_evidence_after_reboot_fails_without_touching_program(reboot_lab, monkeypatch):
    lab = reboot_lab
    confirm(lab, monkeypatch)
    receipt = lab.service._receipt_path(lab.store, "preview", "check")
    value = read_control(receipt); value["signature"] = "AAAA"; atomic_control(receipt, value)
    monkeypatch.setattr(deferred, "boot_session", lambda: BOOT_B)
    result = deferred._consume_after_reboot(lab.store)
    assert result.status == "blocked"
    assert tree_digest(lab.store.root / "active") == lab.before
    assert deferred._consume_after_reboot(lab.store) is None  # No automatic retry.


def test_busy_runtime_blocks_once_without_terminating_running_instance(reboot_lab, monkeypatch):
    lab = reboot_lab
    confirm(lab, monkeypatch)
    monkeypatch.setattr(deferred, "boot_session", lambda: BOOT_B)
    with lab.store.runtime_lease():
        result = deferred._consume_after_reboot(lab.store)
        assert result.status == "blocked" and result.error_code == "UPDATE_BUSY"
    assert tree_digest(lab.store.root / "active") == lab.before
    assert deferred._consume_after_reboot(lab.store) is None


def test_boot_reader_unavailable_does_not_claim_or_mutate(reboot_lab, monkeypatch):
    lab = reboot_lab
    confirm(lab, monkeypatch)
    before = deferred.pending_status(lab.store)
    def unavailable(): raise UpdateError("UPDATE_REBOOT_ID_UNAVAILABLE", "Unavailable")
    monkeypatch.setattr(deferred, "boot_session", unavailable)
    assert deferred._consume_after_reboot(lab.store).error_code == "UPDATE_REBOOT_ID_UNAVAILABLE"
    assert deferred.pending_status(lab.store) == before
    assert tree_digest(lab.store.root / "active") == lab.before


def test_crash_after_durable_claim_is_not_replayed(reboot_lab, monkeypatch):
    lab = reboot_lab
    confirm(lab, monkeypatch)
    value = deferred._read(lab.store)
    atomic_control(deferred._claimed_path(lab.store), {"schema_version": 1,
        "installation_id": lab.store.registration["installation_id"], "request_id": deferred._request_id(value)})
    monkeypatch.setattr(deferred, "boot_session", lambda: BOOT_B)
    assert deferred._consume_after_reboot(lab.store).error_code == "UPDATE_DEFERRED_CONSUMED"
    assert deferred.pending_status(lab.store) is None
    assert tree_digest(lab.store.root / "active") == lab.before


def test_source_role_or_shared_descriptor_cannot_execute_deferred_request(reboot_lab, monkeypatch):
    lab = reboot_lab
    confirm(lab, monkeypatch)
    with pytest.raises(UpdateError, match="UPDATE_BOOTSTRAP_ROLE"):
        deferred.consume_after_reboot(lab.store)
    with lab.store.runtime_lease() as (_, fd), retain_descriptor(fd):
        assert deferred._consume_after_reboot(lab.store).error_code == "UPDATE_SCOPE_UNAVAILABLE"


def test_offline_rollback_also_waits_for_different_boot(mac_lab, monkeypatch):
    lab = mac_lab
    selected = upgraded(lab, monkeypatch)
    water = lab.store._ledger()["watermarks"]
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(handoff, "strategy", lambda _: "after_reboot")
    monkeypatch.setattr(deferred, "boot_session", lambda: BOOT_A)
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        lab.service.request_rollback(selected["rollback_id"], "3" * 32)
        assert consume(lab, nonce, cleanup={"complete": False}).status == "restart_required"
    assert deferred._consume_after_reboot(lab.store).status == "restart_required"
    monkeypatch.setattr(deferred, "boot_session", lambda: BOOT_B)
    assert deferred._consume_after_reboot(lab.store).status == "rolled_back"
    assert tree_digest(lab.store.root / "active") == lab.before
    assert lab.store._ledger()["watermarks"] == water


def test_real_kernel_boot_reader_is_stable_and_ignores_environment(monkeypatch):
    if sys.platform != "darwin": pytest.skip("Actual Darwin sysctl required")
    before = deferred.boot_session()
    monkeypatch.setenv("GITGO_BOOT_SESSION", BOOT_A)
    assert deferred.boot_session() == before


def test_forged_display_version_is_not_the_verified_selection(reboot_lab, monkeypatch):
    lab = reboot_lab
    confirm(lab, monkeypatch)
    value = deferred._read(lab.store); value["version"] = "99.0.0"
    atomic_control(deferred._pending_path(lab.store), value)
    monkeypatch.setattr(deferred, "boot_session", lambda: BOOT_B)
    assert deferred._consume_after_reboot(lab.store).error_code == "UPDATE_SELECTION_STALE"
    assert tree_digest(lab.store.root / "active") == lab.before


def test_new_profile_overlap_after_confirmation_is_rejected(reboot_lab, monkeypatch):
    lab = reboot_lab
    confirm(lab, monkeypatch)
    monkeypatch.setenv("GITGO_CONFIG_PATH", str(lab.store.root / "active/config.json"))
    monkeypatch.setattr(deferred, "boot_session", lambda: BOOT_B)
    assert deferred._consume_after_reboot(lab.store).error_code == "UPDATE_PROTECTED_PATHS"
    assert tree_digest(lab.store.root / "active") == lab.before


@pytest.mark.parametrize("durable", [False, True])
def test_claim_write_failure_cannot_switch_or_replay_a_durable_claim(reboot_lab, monkeypatch, durable):
    lab = reboot_lab
    confirm(lab, monkeypatch)
    monkeypatch.setattr(deferred, "boot_session", lambda: BOOT_B)
    save = deferred.atomic_control
    def fail(path, value):
        if durable: save(path, value)
        raise OSError("private diagnostic not exposed")
    with monkeypatch.context() as context:
        context.setattr(deferred, "atomic_control", fail)
        result = deferred._consume_after_reboot(lab.store)
        assert result.status == "blocked" and result.error_code == "UPDATE_DEFERRED_FAILED"
    assert tree_digest(lab.store.root / "active") == lab.before
    next_result = deferred._consume_after_reboot(lab.store)
    assert next_result.status == ("blocked" if durable else "committed")
    if durable: assert next_result.error_code == "UPDATE_DEFERRED_CONSUMED"
