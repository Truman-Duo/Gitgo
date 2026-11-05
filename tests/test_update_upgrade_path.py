"""Use real signatures to compare shipping consumers with future candidates."""
from dataclasses import replace
from datetime import timedelta

import pytest

from backend.core.update.upgrade_path import assess_upgrade_path, UpgradePathRequired
from backend.core.update.validation import UpdateError
from scripts.prepare_update_release import validate_upgrade_baseline
from tests.test_update_manifest import manifest_wire, signed, signing


def releases(signing, **changes):
    baseline = manifest_wire(version="1.0.0-alpha.3", sequence=1)
    raw, signature = signed(baseline, signing)
    old = signing[1].verify(raw, signature)
    candidate_raw, candidate_signature = signed(manifest_wire(version="1.0.0-alpha.3.1",
        sequence=2, source={"commit": "e" * 40, "tree": "f" * 40}, **changes), signing)
    candidate = signing[1].verify(candidate_raw, candidate_signature)
    return raw, signature, old, candidate


def test_unrelated_program_replacement_is_not_a_data_migration(signing):
    raw, signature, old, candidate = releases(signing)
    result = validate_upgrade_baseline(signing[1], candidate,
        baseline_manifest=raw, baseline_signature=signature)
    assert result.ready
    assert result.source_version == "1.0.0-alpha.3"
    assert result.target_version == "1.0.0-alpha.3.1"
    assert old.source_tree != candidate.source_tree
    assert result.to_dict()["installed_upgrade_verified"] is False


@pytest.mark.parametrize("resource", ["config", "provider"])
def test_changed_writer_evidence_requires_a_real_transition_not_a_false_release_success(signing, resource):
    raw, signature, old, candidate = releases(signing)
    compatibility = replace(candidate.data_compatibility, **{resource + "_migration_sha256": "9" * 64})
    candidate = replace(candidate, data_compatibility=compatibility)
    with pytest.raises(UpgradePathRequired) as exc:
        validate_upgrade_baseline(signing[1], candidate, baseline_manifest=raw, baseline_signature=signature)
    result = exc.value.assessment
    assert result.data_changes == (resource + ":writer_evidence_changed",)
    assert result.requirements == ("DATA_TRANSITION_PATH_REQUIRED",)
    assert result.to_dict()["migration_executed"] is False


@pytest.mark.parametrize("history,kind", [
    (((1, "1" * 64), (2, "2" * 64)), "extended"),
    (((1, "9" * 64),), "rewritten"),
])
def test_database_history_changes_have_specific_transition_requirements(signing, history, kind):
    _, _, old, candidate = releases(signing)
    candidate = replace(candidate, data_compatibility=replace(candidate.data_compatibility, state_migrations=history))
    result = assess_upgrade_path(old, candidate)
    assert not result.ready
    assert result.data_changes == ("state_migrations:" + kind,)


def test_removed_database_history_is_not_treated_as_no_change(signing):
    _, _, old, candidate = releases(signing)
    old = replace(old, data_compatibility=replace(old.data_compatibility,
        state_migrations=(*old.data_compatibility.state_migrations, (2, "9" * 64))))
    assert assess_upgrade_path(old, candidate).data_changes == ("state_migrations:removed",)


def test_old_updater_cannot_be_retroactively_given_a_new_protocol(signing):
    _, _, old, candidate = releases(signing, minimum_updater_version="0.2.0")
    assert assess_upgrade_path(old, candidate).requirements == ("UPDATER_BRIDGE_REQUIRED",)


def test_modern_engine_supports_changed_contract_and_legacy_repair_is_an_explicit_route(signing):
    raw, signature, old, candidate = releases(signing, minimum_updater_version="0.2.0")
    candidate = replace(candidate, data_compatibility=replace(candidate.data_compatibility, config_migration_sha256="9" * 64))
    assert not assess_upgrade_path(old, candidate).ready
    modern = assess_upgrade_path(replace(old, minimum_updater_version="0.2.0"), candidate)
    assert modern.ready and modern.to_dict()["migration_executed"] is False
    repaired = validate_upgrade_baseline(signing[1], candidate, baseline_manifest=raw,
        baseline_signature=signature, upgrade_route="legacy-repair")
    assert repaired.ready and repaired.route == "legacy-repair"
    assert repaired.to_dict()["installed_upgrade_verified"] is False


@pytest.mark.parametrize("changes,requirement", [
    ({"version": "1.0.0-alpha.3"}, "NEW_RELEASE_VERSION_REQUIRED"),
    ({"sequence": 1}, "MONOTONIC_RELEASE_EVIDENCE_REQUIRED"),
    ({"platform": "linux"}, "RELEASE_TARGET_OR_CHANNEL_CHANGED"),
])
def test_preflight_preserves_release_identity_and_replay_boundaries(signing, changes, requirement):
    _, _, old, candidate = releases(signing)
    assert requirement in assess_upgrade_path(old, replace(candidate, **changes)).requirements


def test_moving_clock_back_does_not_allow_an_older_release(signing):
    _, _, old, candidate = releases(signing)
    candidate = replace(candidate, published_at=old.published_at - timedelta(seconds=1))
    assert "MONOTONIC_RELEASE_EVIDENCE_REQUIRED" in assess_upgrade_path(old, candidate).requirements


def test_historical_signature_is_checked_even_when_its_metadata_has_expired(signing):
    raw, signature, _, candidate = releases(signing)
    assert validate_upgrade_baseline(signing[1], candidate,
        baseline_manifest=raw, baseline_signature=signature).ready
    with pytest.raises(UpdateError, match="UPDATE_SIGNATURE_INVALID"):
        validate_upgrade_baseline(signing[1], candidate,
            baseline_manifest=raw + b" ", baseline_signature=signature)


def test_missing_or_ambiguous_baseline_cannot_silently_become_a_first_release(signing):
    raw, signature, _, candidate = releases(signing)
    for options in ({}, {"baseline_manifest": raw}, {"baseline_signature": signature},
                    {"initial_release": True, "baseline_manifest": raw, "baseline_signature": signature}):
        with pytest.raises(ValueError):
            validate_upgrade_baseline(signing[1], candidate, **options)
    assert validate_upgrade_baseline(signing[1], candidate, initial_release=True) is None


def test_readonly_source_audit_requires_an_independent_publisher_pin(signing, monkeypatch):
    import hashlib
    import json
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from backend.core.update.transaction import _compat_wire
    from scripts.check_update_readiness import inspect
    from scripts.prepare_update_release import github_policy

    raw, signature, old, _ = releases(signing)
    key = signing[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    policy = json.dumps(github_policy(key, "ephemeral-test", old.platform, old.architecture)).encode()
    compatibility = _compat_wire(replace(old.data_compatibility, config_migration_sha256="9" * 64))
    monkeypatch.setattr("backend.core.update.runtime_contract.runtime_evidence", lambda: (compatibility, {}))
    result = inspect(raw, signature, policy, hashlib.sha256(key).hexdigest())
    assert result["baseline"]["signature_verified"]
    assert result["local_source"]["changes"] == ["config:writer_evidence_changed"]
    assert result["release_ready"] is False
    assert result["candidate_release_created"] is False
    with pytest.raises(ValueError, match="public key differs"):
        inspect(raw, signature, policy, "0" * 64)
