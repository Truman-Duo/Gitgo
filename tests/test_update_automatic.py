"""Automation boundaries using real private NTFS registration and update policy."""
import pytest
from backend.core.update.preparation_service import UpdatePreparationService
from backend.core.update.automatic import configure, preferences, tick, observation, queue_on_clean_exit, installation_supported
from backend.core.update.validation import UpdateError
from tests.test_update_windows_managed import managed
from tests.test_update_manifest import signing
from tests.test_update_discovery import NOW
from backend.core.update.windows_storage import supported
pytestmark = pytest.mark.skipif(not supported(), reason="Native Windows ownership required")

def service(store):
    return UpdatePreparationService(store_loader=lambda: store, clock=lambda: NOW)

def test_preferences_are_installation_owned_persistent_and_compare_revision(managed):
    store, _, _ = managed
    updates = service(store)
    assert preferences(store)["channel"] == "preview"
    result = configure(updates, "stable", False, False, False, 0)
    assert result["schema_version"] == 6
    assert result["automatic_check"] is False
    assert preferences(store)["revision"] == 1
    assert service(store).status()["channel"] == "stable"
    with pytest.raises(UpdateError, match="UPDATE_SELECTION_STALE"):
        configure(updates, "preview", True, True, True, 0)
    with pytest.raises(UpdateError, match="UPDATE_PREFERENCES_INVALID"):
        configure(updates, "preview", "true", True, True, 1)

def test_disabled_checks_do_not_fetch_and_do_not_claim_checked(managed, monkeypatch):
    store, _, _ = managed
    updates = service(store)
    configure(updates, "preview", False, True, True, 0)
    monkeypatch.setattr(updates, "check", lambda *args: pytest.fail("disabled network operation"))
    tick(updates)
    assert observation(store)["state"] == "not_checked"

def test_automatic_check_and_download_are_serial_and_rate_limited(managed, monkeypatch):
    store, _, _ = managed
    updates = service(store); calls = []
    def check(channel, operation):
        calls.append(("check", channel))
        return {"status": "available", "can_download": True, "candidate": {"candidate_id": "a"*64}}
    def download(channel, candidate, operation):
        calls.append(("download", channel, candidate))
        return {"status": "downloaded"}
    monkeypatch.setattr(updates, "check", check); monkeypatch.setattr(updates, "download", download)
    tick(updates); tick(updates)
    assert calls == [("check", "preview"), ("download", "preview", "a"*64)]
    assert observation(store)["state"] == "prepared"

def test_revoked_download_preference_is_observed_before_transfer(managed, monkeypatch):
    store, _, _ = managed
    updates = service(store)
    def check(channel, operation):
        configure(updates, "preview", True, False, False, 0)
        return {"status": "available", "can_download": True, "candidate": {"candidate_id": "a"*64}}
    monkeypatch.setattr(updates, "check", check)
    monkeypatch.setattr(updates, "download", lambda *args: pytest.fail("revoked automatic download"))
    tick(updates)
    assert not preferences(store)["automatic_download"]
    assert observation(store)["state"] != "prepared"

def test_failure_is_visible_sanitized_and_backed_off(managed, monkeypatch):
    store, _, _ = managed
    updates = service(store); calls = []
    def fail(*args):
        calls.append(1)
        raise UpdateError("UPDATE_FETCH_FAILED", "private proxy credential must not be projected")
    monkeypatch.setattr(updates, "check", fail)
    tick(updates); tick(updates)
    record = observation(store)
    assert record["state"] == "error" and record["error_code"] == "UPDATE_FETCH_FAILED"
    assert "private proxy" not in str(record)
    assert len(calls) == 1
    assert record["next_attempt"] > record["last_attempt"]

def test_clean_exit_requires_install_opt_in_and_does_not_guess_a_candidate(managed, monkeypatch):
    store, _, _ = managed
    updates = service(store)
    configure(updates, "preview", True, True, False, 0)
    monkeypatch.setattr(updates, "status", lambda *args: pytest.fail("disabled install"))
    assert queue_on_clean_exit(updates, "b"*32) is None

def test_old_immutable_bootstrap_does_not_gain_automatic_install_capability(managed):
    store, _, _ = managed
    assert installation_supported(store)
    old = type("OldRegistration", (), {"registration": {**store.registration, "bootstrap_api": 7}})()
    assert not installation_supported(old)

def test_probe_and_provisioning_share_the_current_bootstrap_contract():
    from backend.core.update.health import MANAGED_API_VERSION
    from backend.core.update.installation import ManagedStore
    assert MANAGED_API_VERSION == ManagedStore.BOOTSTRAP_API

def test_github_feeds_are_distinct_and_contain_public_trust_only(signing):
    from scripts.prepare_update_release import github_policy
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    key = signing[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    policy = github_policy(key, "publisher-v1", "windows", "x86_64")
    stable, preview = policy["channels"]["stable"], policy["channels"]["preview"]
    assert "/updates-stable/" in stable["manifest_url"]
    assert "/updates-preview/" in preview["manifest_url"]
    assert stable != preview
    assert set(policy["public_keys"][0]) == {"key_id", "ed25519_base64"}


def test_new_preferences_do_not_display_a_previous_channel_attempt(managed, monkeypatch):
    store, _, _ = managed
    updates = service(store)
    monkeypatch.setattr(updates, "check", lambda *args: {"status": "up_to_date"})
    tick(updates)
    assert updates.status()["automatic"]["state"] == "up_to_date"
    configure(updates, "stable", False, False, False, 0)
    assert updates.status()["automatic"]["state"] == "not_checked"
    assert observation(store)["channel"] == "preview"  # retained historical record

@pytest.mark.parametrize("state,candidate,expected",[
    ("ROLLED_BACK","a"*64,"UPDATE_CANDIDATE_ROLLED_BACK"),
    ("COMMITTED","a"*64,None),("ROLLED_BACK","b"*64,None)])
def test_automatic_rejection_uses_exact_terminal_transaction_manifest(state,candidate,expected):
    from types import SimpleNamespace
    from backend.core.update.automatic import candidate_blocker
    store=SimpleNamespace(_ledger=lambda:{"transaction":{"state":state,
        "watermark":{"manifest_sha256":"a"*64}}})
    assert candidate_blocker(store,candidate)==expected
