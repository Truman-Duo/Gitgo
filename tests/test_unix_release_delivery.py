"""Keep every declared Unix updater target covered by preview release trust."""
import json
from pathlib import Path
import pytest
from scripts.check_unix_release_delivery import TARGETS, policy, validate_published_identity
from types import SimpleNamespace
from backend.core.update.manifest import TARGETS as UPDATE_TARGETS
from backend.core.update.release_policy import parse_release_policy

ROOT = Path(__file__).resolve().parents[1]

def test_all_declared_unix_targets_are_accounted_for():
    assert TARGETS == {target for target in UPDATE_TARGETS if target[0] != "windows"}

@pytest.mark.parametrize("target,arch", sorted(TARGETS))
def test_release_policy_preserves_pinned_key_and_keeps_stable_separate(target, arch):
    base = json.loads((ROOT/"packaging/release-trust/windows-x86_64.json").read_bytes())
    value = policy(ROOT, target, arch)
    assert value["public_keys"] == base["public_keys"]
    assert value["allowed_hosts"] == base["allowed_hosts"]
    assert set(value["channels"]) == {"preview"}
    selected = parse_release_policy(json.dumps(value).encode(), target, arch).channel("preview")
    assert selected.manifest_url.endswith("/updates-preview/"+target+"-"+arch+".manifest.json")
    assert selected.signature_url.endswith("/updates-preview/"+target+"-"+arch+".manifest.sig")

@pytest.mark.parametrize("target,arch", [("macos","x86_64"),("windows","x86_64"),("linux","arm64"),("other","x86_64")])
def test_undeclared_target_is_rejected_before_reading_input(target, arch):
    with pytest.raises(ValueError, match="Unsupported"):
        policy(Path("does-not-exist"), target, arch)


@pytest.mark.parametrize("version", ["1.0.0-alpha.3.1", "1.0.0-alpha.3.2", "1.0.0-alpha.3.3", "2.0.0-beta.1"])
@pytest.mark.parametrize("target,arch", sorted(TARGETS))
def test_published_validation_follows_actual_release_version(version, target, arch):
    manifest = SimpleNamespace(source_commit="a" * 40, release_id="v" + version,
        version=version, scope=("gitgo-terminal", "preview", target, arch))
    validate_published_identity(manifest, "a" * 40, target, arch, version)


@pytest.mark.parametrize("change", ["source_commit", "release_id", "version", "scope"])
def test_new_version_never_relaxes_other_release_identity_checks(change):
    manifest = SimpleNamespace(source_commit="a" * 40, release_id="v1.0.0-alpha.3.1",
        version="1.0.0-alpha.3.1", scope=("gitgo-terminal", "preview", "macos", "arm64"))
    setattr(manifest, change, ("gitgo-terminal", "stable", "macos", "arm64") if change == "scope" else "wrong")
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_published_identity(manifest, "a" * 40, "macos", "arm64", "1.0.0-alpha.3.1")
