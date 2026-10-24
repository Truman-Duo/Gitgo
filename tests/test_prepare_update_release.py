"""The released archive and publisher feed must use the installed trust."""
import json
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from backend.core.update.validation import UpdateError
from scripts.prepare_update_release import github_policy, staged_policy


def fixture_policy(tmp_path_factory):
    stage = tmp_path_factory / "release-stage"
    stage.mkdir()
    public = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    value = github_policy(public, "publisher-v1", "windows", "x86_64")
    del value["channels"]["stable"]
    (stage / "release-trust.json").write_text(json.dumps(value), encoding="utf-8")
    return stage, public, value


def select(stage, public, channel="preview"):
    return staged_policy(stage, public, "publisher-v1", "windows", "x86_64",
                         "Truman-Duo/Gitgo", channel)


def test_preview_only_release_preserves_the_exact_installed_policy(tmp_path_factory):
    stage, public, value = fixture_policy(tmp_path_factory)
    raw, policy = select(stage, public)
    assert raw == (stage / "release-trust.json").read_bytes()
    assert json.loads(raw) == value
    assert set(policy.channels) == {"preview"}
    with pytest.raises(UpdateError, match="UPDATE_CHANNEL_UNCONFIGURED"):
        select(stage, public, "stable")


@pytest.mark.parametrize("change", ["key", "feed", "hosts", "target", "missing"])
def test_signing_rejects_a_package_that_would_not_trust_its_release(tmp_path_factory, change):
    stage, public, value = fixture_policy(tmp_path_factory)
    if change == "key":
        value["public_keys"][0]["key_id"] = "another-publisher"
    elif change == "feed":
        value["channels"]["preview"]["manifest_url"] = "https://github.com/other/repo/manifest.json"
    elif change == "hosts":
        value["allowed_hosts"].append("example.com")
    elif change == "target":
        value["architecture"] = "aarch64"
    path = stage / "release-trust.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    if change == "missing":
        path.unlink()
    with pytest.raises((ValueError, UpdateError, FileNotFoundError)):
        select(stage, public)
