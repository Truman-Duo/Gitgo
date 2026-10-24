"""Keep every declared Unix updater target covered by preview release trust."""
import json
from pathlib import Path
import pytest
from scripts.check_unix_release_delivery import TARGETS, policy
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
