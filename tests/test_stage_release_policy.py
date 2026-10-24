"""Public build policy inputs do not require private installed-file ACLs."""
import json
import os
import pytest
from scripts.prepare_update_release import github_policy
from scripts.stage_release_policy import stage_policy
from backend.core.update.release_policy import MAX_POLICY_BYTES
from backend.core.update.validation import UpdateError


def inputs(root):
    stage = root / "stage"
    stage.mkdir()
    (stage / "product.json").write_text(json.dumps({
        "platform": "windows", "architecture": "x86_64"}), encoding="utf-8")
    (stage / "update-data-contract.json").write_text("{}", encoding="utf-8")
    policy = root / "public-trust.json"
    raw = json.dumps(github_policy(b"x" * 32, "build-publisher", "windows", "x86_64")).encode()
    policy.write_bytes(raw)
    if os.name == "posix":
        policy.chmod(0o644)  # Public source; production bootstrap stays private.
    return stage, policy, raw


def test_relative_public_build_input_is_staged_without_mutation(tmp_path_factory, monkeypatch):
    stage, policy, raw = inputs(tmp_path_factory)
    monkeypatch.chdir(tmp_path_factory)
    stage_policy(stage.relative_to(tmp_path_factory), policy.relative_to(tmp_path_factory))
    assert (stage / "release-trust.json").read_bytes() == raw
    assert policy.read_bytes() == raw


@pytest.mark.parametrize("damage", ["oversize", "malformed", "missing_evidence"])
def test_invalid_public_build_policy_does_not_create_package_trust(tmp_path_factory, damage):
    stage, policy, _ = inputs(tmp_path_factory)
    if damage == "oversize":
        policy.write_bytes(b" " * (MAX_POLICY_BYTES + 1))
    elif damage == "malformed":
        policy.write_bytes(b"{}")
    else:
        (stage / "update-data-contract.json").unlink()
    with pytest.raises(UpdateError):
        stage_policy(stage, policy)
    assert not (stage / "release-trust.json").exists()
