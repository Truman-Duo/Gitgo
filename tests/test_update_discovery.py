"""Policy, replay and compatibility gates exercise the real discovery pipeline."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import subprocess
import sys
import traceback

import pytest

from backend.core.update import (
    CurrentInstallation, DataCompatibility, ManifestVerifier, ReplayState,
    UpdateError, UpdatePolicy, check_for_update,
)
from backend.core.update.manifest import MAX_MANIFEST_BYTES, MAX_SIGNATURE_BYTES
from tests.test_update_manifest import compatibility_wire, manifest_wire, signed, signing


NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
POLICY = UpdatePolicy(True, False, "preview", "https://releases.example.test/preview.json",
                      "https://releases.example.test/preview.sig", ("releases.example.test",))
CURRENT = CurrentInstallation("gitgo-terminal", "1.0.0-alpha.2", "macos", "arm64", "0.1.0",
                              DataCompatibility.parse(compatibility_wire()), True)


def pipeline(signing, value=None, *, policy=POLICY, current=CURRENT, now=NOW, previous=None):
    body, detached = signed(value if value is not None else manifest_wire(), signing)
    calls = []

    def fetch(url, maximum):
        calls.append((url, maximum))
        return body if url == policy.manifest_url else detached

    result = check_for_update(policy, current, signing[1], fetch, now=now, previous=previous)
    return result, calls


def test_available_is_only_metadata_and_never_downloads_asset(signing):
    result, calls = pipeline(signing)
    assert result.status == "available"
    assert result.candidate.url == "https://releases.example.test/gitgo.tar.gz"
    assert result.watermark.manifest_sha256 == result.candidate.manifest_sha256
    assert calls == [(POLICY.manifest_url, MAX_MANIFEST_BYTES), (POLICY.signature_url, MAX_SIGNATURE_BYTES)]


@pytest.mark.parametrize("platform,architecture,archive_format", [
    ("windows", "x86_64", "zip"), ("macos", "arm64", "tar.gz"), ("linux", "x86_64", "tar.gz"),
])
@pytest.mark.parametrize("channel,version", [("stable", "1.0.0"), ("preview", "1.0.0-alpha.3")])
def test_each_declared_target_and_channel_can_discover_matching_metadata(signing, platform, architecture, archive_format, channel, version):
    value = manifest_wire(platform=platform, architecture=architecture, archive_format=archive_format,
                          channel=channel, version=version)
    result, _ = pipeline(signing, value, policy=replace(POLICY, channel=channel),
                         current=replace(CURRENT, platform=platform, architecture=architecture))
    assert result.status == "available"
    assert result.candidate.scope == ("gitgo-terminal", channel, platform, architecture)


def test_disabled_and_unrequested_automatic_checks_do_not_touch_any_dependency():
    def forbidden(*_):
        pytest.fail("disabled discovery touched a dependency")

    assert check_for_update(UpdatePolicy(), None, None, forbidden).status == "disabled"
    assert check_for_update(POLICY, None, None, forbidden, automatic=True).status == "disabled"


def test_automatic_check_requires_both_switches(signing):
    raw, signature = signed(manifest_wire(), signing)
    result = check_for_update(replace(POLICY, automatic_check=True), CURRENT, signing[1],
                              lambda url, _: raw if url.endswith("json") else signature,
                              now=NOW, automatic=True)
    assert result.status == "available"


@pytest.mark.parametrize("current,verifier,code", [
    (replace(CURRENT, managed=False), ManifestVerifier(), "UPDATE_INSTALL_UNMANAGED"),
    (replace(CURRENT, data_compatibility=None), ManifestVerifier(), "UPDATE_COMPATIBILITY_MISSING"),
    (replace(CURRENT, platform="linux", architecture="riscv64"), ManifestVerifier(), "UPDATE_TARGET_INVALID"),
    (CURRENT, ManifestVerifier(), "UPDATE_TRUST_MISSING"),
])
def test_missing_local_authority_fails_before_fetch(current, verifier, code):
    def forbidden(*_):
        pytest.fail("invalid local authority reached transport")

    with pytest.raises(UpdateError, match=code):
        check_for_update(POLICY, current, verifier, forbidden, now=NOW)


@pytest.mark.parametrize("changes,code", [
    ({"product_id": "another-product"}, "UPDATE_SCOPE_MISMATCH"),
    ({"channel": "stable", "version": "1.0.0"}, "UPDATE_SCOPE_MISMATCH"),
    ({"platform": "windows", "architecture": "x86_64", "archive_format": "zip"}, "UPDATE_SCOPE_MISMATCH"),
    ({"platform": "linux", "architecture": "x86_64"}, "UPDATE_SCOPE_MISMATCH"),
    ({"version": "1.0.0-alpha.1"}, "UPDATE_DOWNGRADE_REJECTED"),
    ({"minimum_updater_version": "0.2.0"}, "UPDATE_CLIENT_TOO_OLD"),
])
def test_wrong_product_target_channel_or_version_is_not_an_upgrade(signing, changes, code):
    with pytest.raises(UpdateError, match=code):
        pipeline(signing, manifest_wire(**changes))


@pytest.mark.parametrize("version", ["1.0.0-alpha.2", "1.0.0-alpha.2+new-build"])
def test_same_precedence_is_up_to_date_not_replacement(signing, version):
    result, calls = pipeline(signing, manifest_wire(version=version))
    assert result.status == "up_to_date" and result.candidate is None
    assert result.watermark is not None and len(calls) == 2


def test_expiry_boundaries_future_release_and_naive_clock(signing):
    for now in (NOW - timedelta(days=1), datetime(2026, 10, 14, tzinfo=timezone.utc)):
        with pytest.raises(UpdateError, match="UPDATE_FRESHNESS_INVALID"):
            pipeline(signing, now=now)
    with pytest.raises(UpdateError, match="UPDATE_CLOCK_INVALID"):
        pipeline(signing, now=NOW.replace(tzinfo=None))
    assert pipeline(signing, now=datetime(2026, 10, 12, tzinfo=timezone.utc))[0].status == "available"


def test_exact_retry_allowed_but_sequence_reuse_and_replay_rejected(signing):
    first, _ = pipeline(signing)
    assert pipeline(signing, previous=first.watermark)[0].candidate == first.candidate
    for changes in ({"sequence": 1}, {"sha256": "d" * 64}, {"release_id": "rewritten-release"}):
        with pytest.raises(UpdateError, match="UPDATE_REPLAY_REJECTED"):
            pipeline(signing, manifest_wire(**changes), previous=first.watermark)


def test_new_sequence_does_not_allow_release_or_timestamp_regression(signing):
    first, _ = pipeline(signing, manifest_wire(version="1.0.0-alpha.5"))
    for changes in ({"sequence": 3, "version": "1.0.0-alpha.4"},
                    {"sequence": 3, "published_at": "2026-10-11T00:00:00Z", "version": "1.0.0-alpha.6"}):
        with pytest.raises(UpdateError, match="UPDATE_REPLAY_REJECTED"):
            pipeline(signing, manifest_wire(**changes), previous=first.watermark)
    assert pipeline(signing, manifest_wire(sequence=3, version="1.0.0-alpha.6"),
                    previous=first.watermark)[0].status == "available"


def test_higher_sequence_cannot_rewrite_the_same_package_version(signing):
    first, _ = pipeline(signing)
    with pytest.raises(UpdateError, match="UPDATE_RELEASE_REWRITTEN"):
        pipeline(signing, manifest_wire(sequence=3, sha256="e" * 64), previous=first.watermark)
    # Publishers can refresh validity for the same immutable asset with a new sequence.
    assert pipeline(signing, manifest_wire(sequence=3, expires_at="2026-10-15T00:00:00Z"),
                    previous=first.watermark)[0].status == "available"


def test_watermark_cannot_be_reused_for_another_channel_or_target(signing):
    first, _ = pipeline(signing)
    with pytest.raises(UpdateError, match="UPDATE_REPLAY_STATE_INVALID"):
        pipeline(signing, previous=replace(first.watermark, channel="stable"))
    with pytest.raises(UpdateError, match="UPDATE_CLOCK_INVALID"):
        pipeline(signing, previous=replace(first.watermark, published_at=NOW + timedelta(hours=1)))


@pytest.mark.parametrize("field", ["state_migrations", "observability_migrations", "config_schema_version",
                                    "config_migration_sha256", "provider_schema_version", "provider_migration_sha256"])
def test_each_data_contract_change_blocks_before_running_new_code(signing, field):
    wire = deepcopy(compatibility_wire())
    if field.endswith("migrations"):
        wire[field][0]["sha256"] = "9" * 64
    elif field.endswith("version"):
        wire[field] += 1
    else:
        wire[field] = "9" * 64
    with pytest.raises(UpdateError, match="UPDATE_MIGRATION_REQUIRED"):
        pipeline(signing, manifest_wire(data_compatibility=wire))


@pytest.mark.parametrize("url", [
    "http://releases.example.test/file", "https://other.example.test/file",
    "https://releases.example.test.evil.test/file", "https://releases.example.test@evil.test/file",
    "https://user:password@releases.example.test/file", "https://releases.example.test:8443/file",
    "https://releases.example.test/file?token=secret", "https://releases.example.test/file#fragment",
    "https://releases.example.test/../file", "https://releases.example.test/%2e%2e/file",
    "https://releases.example.test/%5cfile", "https://releases.example.test/%0afile",
    "https://releases.example.test/ bad", "https://releases.example.test/%xx",
    "https://releases.example.test:invalid/file", "https://releases.example.test/file\n",
])
def test_invalid_signed_asset_urls_are_rejected(signing, url):
    with pytest.raises(UpdateError, match="UPDATE_URL_INVALID"):
        pipeline(signing, manifest_wire(url=url))


def test_invalid_manifest_endpoint_fails_before_fetch(signing):
    with pytest.raises(UpdateError, match="UPDATE_URL_INVALID"):
        check_for_update(replace(POLICY, manifest_url="https://evil.test/manifest"), CURRENT,
                         signing[1], lambda *_: pytest.fail("must not fetch"), now=NOW)


def test_offline_failure_is_safe_and_does_not_disclose_proxy_credentials(signing):
    def offline(*_):
        raise OSError("proxy password: test-password")

    with pytest.raises(UpdateError, match="UPDATE_FETCH_FAILED") as failure:
        check_for_update(POLICY, CURRENT, signing[1], offline, now=NOW)
    assert "test-password" not in str(failure.value)
    assert "test-password" not in "".join(traceback.format_exception(failure.value))


def test_signature_fetch_failure_does_not_return_unverified_metadata(signing):
    raw, _ = signed(manifest_wire(), signing)
    calls = []

    def second_fails(url, limit):
        calls.append(url)
        if len(calls) == 2:
            raise TimeoutError("detached signature unavailable")
        return raw

    with pytest.raises(UpdateError, match="UPDATE_FETCH_FAILED"):
        check_for_update(POLICY, CURRENT, signing[1], second_fails, now=NOW)
    assert len(calls) == 2


def test_oversize_first_response_stops_before_signature_fetch(signing):
    calls = []

    def oversized(url, limit):
        calls.append(url)
        return b"x" * (limit + 1)

    with pytest.raises(UpdateError, match="UPDATE_INPUT_SIZE"):
        check_for_update(POLICY, CURRENT, signing[1], oversized, now=NOW)
    assert calls == [POLICY.manifest_url]


def test_discovery_leaves_protected_files_untouched(signing, tmp_path_factory):
    protected = [tmp_path_factory / name for name in ("project.txt", "state.sqlite3", "config.json", "provider_secrets.json")]
    for file in protected:
        file.write_bytes(b"sentinel, not real user data")
    before = {file: file.read_bytes() for file in protected}
    pipeline(signing)
    with pytest.raises(UpdateError):
        pipeline(signing, manifest_wire(version="0.9.0"))
    assert {file: file.read_bytes() for file in protected} == before


def test_discovery_import_does_not_add_crypto_or_open_configuration_and_storage():
    code = (
        "import sys; import backend.core; "
        "from backend.core.config import ConfigManager; "
        "from backend.core.storage.runtime import StorageRuntime; "
        "forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('runtime opened')); "
        "ConfigManager.load = forbidden; StorageRuntime.__init__ = forbidden; "
        "import backend.core.update; "
        "assert 'cryptography' not in sys.modules; "
        "from backend.core.update import check_for_update, UpdatePolicy; "
        "assert check_for_update(UpdatePolicy(), None, None, forbidden).status == 'disabled'"
    )
    subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True, timeout=10)


@pytest.mark.parametrize("changes", [{"enabled": "false"}, {"automatic_check": 1},
                                      {"channel": "nightly"}, {"allowed_hosts": ["example.test"]},
                                      {"allowed_hosts": ("127.0.0.1",)}, {"allowed_hosts": ("*.example.test",)},
                                      {"allowed_hosts": ("example..test",)}, {"allowed_hosts": ("-bad.example",)}])
def test_policy_does_not_coerce_dangerous_configuration(changes):
    with pytest.raises(UpdateError):
        replace(POLICY, **changes)


def test_invalid_local_watermark_and_compatibility_do_not_become_default_success():
    with pytest.raises(UpdateError):
        ReplayState("gitgo-terminal", "preview", "macos", "arm64", True, "a" * 64,
                    "b" * 64, "1.0.0-alpha.3", NOW)
    with pytest.raises(UpdateError):
        replace(CURRENT, data_compatibility={})
    with pytest.raises(UpdateError):
        replace(CURRENT, managed="true")
