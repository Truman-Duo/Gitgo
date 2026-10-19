"""Authenticity and strict wire validation with fresh, memory-only test keys."""

from __future__ import annotations

import base64
import builtins
from copy import deepcopy
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

from backend.core.update import DataCompatibility, ManifestVerifier, UpdateError
from backend.core.update.manifest import MAX_MANIFEST_BYTES, MAX_SIGNATURE_BYTES
from backend.core.update.version import Version


def compatibility_wire():
    return {
        "state_migrations": [{"version": 1, "sha256": "1" * 64}],
        "observability_migrations": [{"version": 1, "sha256": "2" * 64}],
        "config_schema_version": 1, "config_migration_sha256": "3" * 64,
        "provider_schema_version": 1, "provider_migration_sha256": "4" * 64,
    }


def manifest_wire(**changes):
    return {
        "schema_version": 1, "product_id": "gitgo-terminal", "release_id": "test-release-2",
        "channel": "preview", "sequence": 2,
        "published_at": "2026-10-12T00:00:00Z", "expires_at": "2026-10-14T00:00:00Z",
        "version": "1.0.0-alpha.3", "platform": "macos", "architecture": "arm64",
        "archive_format": "tar.gz", "url": "https://releases.example.test/gitgo.tar.gz",
        "bytes": 4096, "sha256": "a" * 64, "source": {"commit": "b" * 40, "tree": "c" * 40},
        "data_compatibility": compatibility_wire(), "minimum_updater_version": "0.1.0",
        **changes,
    }


def sign_raw(raw, private_key, **envelope_changes):
    envelope = {
        "schema_version": 1, "algorithm": "ed25519", "key_id": "ephemeral-test",
        "signature": base64.b64encode(private_key.sign(raw)).decode("ascii"),
        **envelope_changes,
    }
    return json.dumps(envelope).encode("utf-8")


@pytest.fixture
def signing():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, ManifestVerifier({"ephemeral-test": public})


def signed(value, signing):
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return raw, sign_raw(raw, signing[0])


def test_real_signature_and_detached_exact_byte_binding(signing):
    raw, signature = signed(manifest_wire(), signing)
    manifest = signing[1].verify(raw, signature)
    assert manifest.scope == ("gitgo-terminal", "preview", "macos", "arm64")
    assert manifest.source_commit == "b" * 40
    assert manifest.data_compatibility == DataCompatibility.parse(compatibility_wire())
    with pytest.raises(UpdateError, match="UPDATE_SIGNATURE_INVALID"):
        signing[1].verify(raw + b"\n", signature)
    with pytest.raises(UpdateError, match="UPDATE_SIGNATURE_INVALID"):
        signing[1].verify(raw.replace(b"alpha.3", b"alpha.4"), signature)


def test_wrong_private_key_and_unknown_key_cannot_verify(signing):
    raw, _ = signed(manifest_wire(), signing)
    with pytest.raises(UpdateError, match="UPDATE_SIGNATURE_INVALID"):
        signing[1].verify(raw, sign_raw(raw, Ed25519PrivateKey.generate()))
    with pytest.raises(UpdateError, match="UPDATE_KEY_UNKNOWN"):
        signing[1].verify(raw, sign_raw(raw, signing[0], key_id="network-chosen-key"))


def test_signed_linux_arm64_uses_aarch64_not_the_macos_spelling(signing):
    raw, signature = signed(manifest_wire(platform="linux", architecture="aarch64"), signing)
    assert signing[1].verify(raw, signature).scope == ("gitgo-terminal", "preview", "linux", "aarch64")
    raw, signature = signed(manifest_wire(platform="linux", architecture="arm64"), signing)
    with pytest.raises(UpdateError, match="UPDATE_TARGET_INVALID"):
        signing[1].verify(raw, signature)


def test_unverified_manifest_is_not_parsed(signing):
    raw = b"not JSON; do not interpret this as a release"
    with pytest.raises(UpdateError, match="UPDATE_SIGNATURE_INVALID"):
        signing[1].verify(raw, sign_raw(raw, Ed25519PrivateKey.generate()))
    with pytest.raises(UpdateError, match="UPDATE_JSON_INVALID"):
        signing[1].verify(raw, sign_raw(raw, signing[0]))


@pytest.mark.parametrize("changes", [
    {"algorithm": "none"}, {"algorithm": "hmac"}, {"schema_version": True},
    {"schema_version": 2}, {"signature": ""}, {"signature": "=" * 88},
    {"signature": base64.b64encode(b"short").decode("ascii")},
    {"signature": []}, {"public_key": "do not trust network keys"},
])
def test_bad_signature_envelopes_are_rejected(signing, changes):
    raw, _ = signed(manifest_wire(), signing)
    with pytest.raises(UpdateError):
        signing[1].verify(raw, sign_raw(raw, signing[0], **changes))


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"sequence": True}, {"sequence": 0},
    {"sequence": 2**53}, {"sequence": 1.0}, {"bytes": 0}, {"bytes": -1},
    {"bytes": True}, {"bytes": 2 * 1024**3 + 1}, {"sha256": "A" * 64},
    {"sha256": "a" * 63}, {"source": {"commit": "b" * 40, "tree": "not-a-tree"}},
    {"source": {"commit": "b" * 40, "tree": "c" * 40, "dirty": False}},
    {"channel": "nightly"}, {"channel": "stable"}, {"architecture": "amd64"},
    {"platform": "android"}, {"archive_format": "zip"}, {"version": "1.0.0-alpha.03"},
    {"minimum_updater_version": "0.01.0"}, {"data_compatibility": None},
    {"published_at": "2026-02-30T00:00:00Z"}, {"published_at": "2026-10-12T00:00:00+00:00"},
    {"expires_at": "2026-10-12T00:00:00Z"}, {"expires_at": "2027-10-12T00:00:00Z"},
    {"unknown_extension": "must be versioned explicitly"}, {"release_id": "../../outside"},
])
def test_signed_but_invalid_manifest_has_no_candidate(signing, changes):
    with pytest.raises(UpdateError):
        signing[1].verify(*signed(manifest_wire(**changes), signing))


@pytest.mark.parametrize("raw", [
    b'{"schema_version":1,"schema_version":1}', b'{"source":{"tree":1,"tree":2}}',
    b'{"bytes":NaN}', b'{"bytes":Infinity}', b'{"bytes":1.5}',
    b'{"sequence":123456789012345678901}', b'[]', b'null', b'\xff',
    pytest.param(b'{"data":' + b'[' * 2000 + b']' * 2000 + b'}', id="excessive-depth"),
])
def test_ambiguous_or_unbounded_json_is_rejected_after_authentication(signing, raw):
    with pytest.raises(UpdateError, match="UPDATE_JSON_INVALID"):
        signing[1].verify(raw, sign_raw(raw, signing[0]))


def test_input_bounds_and_duplicate_envelope_fields(signing):
    raw, signature = signed(manifest_wire(), signing)
    for body, detached in [(b"", signature), (raw, b""), ("not bytes", signature),
                           (b"x" * (MAX_MANIFEST_BYTES + 1), signature),
                           (raw, b"x" * (MAX_SIGNATURE_BYTES + 1))]:
        with pytest.raises(UpdateError, match="UPDATE_INPUT_SIZE"):
            signing[1].verify(body, detached)
    duplicate = signature[:-1] + b',"algorithm":"ed25519"}'
    with pytest.raises(UpdateError, match="UPDATE_JSON_INVALID"):
        signing[1].verify(raw, duplicate)


def test_json_depth_guard_ignores_containers_inside_strings(signing):
    value = manifest_wire(url="https://releases.example.test/" + "[{}]" * 25)
    assert signing[1].verify(*signed(value, signing)).url == value["url"]


def test_missing_required_fields_and_injected_trust_roots_are_rejected(signing):
    value = manifest_wire()
    value.pop("data_compatibility")
    with pytest.raises(UpdateError, match="UPDATE_SCHEMA_INVALID"):
        signing[1].verify(*signed(value, signing))
    raw, signature = signed(manifest_wire(), signing)
    with pytest.raises(UpdateError, match="UPDATE_KEY_UNKNOWN"):
        ManifestVerifier().verify(raw, signature)
    with pytest.raises(UpdateError, match="UPDATE_TRUST_INVALID"):
        ManifestVerifier({"ephemeral-test": b"short"})


def test_unsupported_ed25519_backend_does_not_fall_back(signing, monkeypatch):
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    def unsupported(*_):
        raise UnsupportedAlgorithm("test backend")

    monkeypatch.setattr(Ed25519PublicKey, "from_public_bytes", unsupported)
    with pytest.raises(UpdateError, match="UPDATE_CRYPTO_UNAVAILABLE"):
        signing[1].verify(*signed(manifest_wire(), signing))


def test_trust_roots_are_copied_and_backend_failure_is_closed(signing, monkeypatch):
    key = signing[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    trust = {"ephemeral-test": key}
    verifier = ManifestVerifier(trust)
    trust.clear()
    raw, detached = signed(manifest_wire(), signing)
    assert verifier.verify(raw, detached).key_id == "ephemeral-test"
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("missing test backend")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(UpdateError, match="UPDATE_CRYPTO_UNAVAILABLE"):
        verifier.verify(raw, detached)


@pytest.mark.parametrize("changes", [
    {"state_migrations": []},
    {"state_migrations": [{"version": 2, "sha256": "a" * 64}]},
    {"state_migrations": [{"version": 1, "sha256": "a" * 64}, {"version": 1, "sha256": "b" * 64}]},
    {"observability_migrations": [{"version": True, "sha256": "a" * 64}]},
    {"config_schema_version": True}, {"provider_migration_sha256": "missing"},
])
def test_incomplete_compatibility_history_is_rejected(changes):
    with pytest.raises(UpdateError):
        DataCompatibility.parse({**compatibility_wire(), **changes})


def test_compatibility_snapshot_is_immutable_and_checks_every_migration():
    wire = compatibility_wire()
    original = DataCompatibility.parse(wire)
    changed = deepcopy(wire)
    changed["state_migrations"][0]["sha256"] = "9" * 64
    assert original != DataCompatibility.parse(changed)
    wire["state_migrations"].clear()
    assert original.state_migrations == ((1, "1" * 64),)


def test_semver_official_precedence_and_numeric_order():
    values = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
              "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
    assert sorted(map(Version.parse, reversed(values))) == list(map(Version.parse, values))
    assert Version.parse("1.9.0") < Version.parse("1.10.0")
    assert Version.parse("1.0.0+old") == Version.parse("1.0.0+new")
    assert Version.parse("1.0.0-alpha.9") < Version.parse("1.0.0-alpha.10")


@pytest.mark.parametrize("value", ["", "v1.0.0", "1.0", "01.0.0", "1.00.0", "1.0.0-01",
                                       "1.0.0-", "1.0.0+", "1.0.0\n", "１.0.0", True, "1" * 129])
def test_invalid_versions_are_not_coerced(value):
    with pytest.raises(UpdateError, match="UPDATE_VERSION_INVALID"):
        Version.parse(value)
