"""The local cross-version feed keeps real signature/hash verification intact."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.update import UpdateError
from backend.core.update.automatic import preferences
from tests.test_update_manifest import signing, sign_raw
from tests.test_update_preparation_service import prep


spec = importlib.util.spec_from_file_location("cross_version_probe",
    Path(__file__).parents[1] / "scripts/macos_cross_version_update_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def fixture_args(prep, tmp_path_factory):
    _, store, network, signing, *_ = prep
    files = tmp_path_factory / "feed"
    files.mkdir()
    value = json.loads(network["raw"])
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = lambda value: value.strftime("%Y-%m-%dT%H:%M:%SZ")
    value.update(published_at=stamp(now - timedelta(minutes=1)), expires_at=stamp(now + timedelta(days=1)))
    raw = json.dumps(value).encode()
    (files / "manifest.json").write_bytes(raw)
    (files / "manifest.sig").write_bytes(sign_raw(raw, signing[0]))
    (files / "package.tar.gz").write_bytes(network["body"])
    return SimpleNamespace(root=store.root, channel="preview", manifest=files / "manifest.json",
        signature=files / "manifest.sig", archive=files / "package.tar.gz")


@pytest.mark.skipif(os.name != "posix", reason="The macOS signed-package fixture requires POSIX installation ownership")
def test_local_fixture_prepares_through_pinned_service_without_switch(prep, tmp_path_factory):
    args = fixture_args(prep, tmp_path_factory)
    report = probe.prepare(args)
    assert report["passed"] and report["download_status"] == "downloaded"
    assert report["before"]["current"] == report["after"]["current"]
    assert report["before"]["active_tree_sha256"] == report["after"]["active_tree_sha256"]
    assert report["calls"] == ["https://releases.example.test/preview.json",
        "https://releases.example.test/preview.sig", "https://releases.example.test/gitgo.tar.gz"]
    assert preferences(prep[1])["automatic_check"] is False
    assert preferences(prep[1])["automatic_install"] is False


@pytest.mark.skipif(os.name != "posix", reason="The macOS signed-package fixture requires POSIX installation ownership")
def test_untrusted_metadata_cannot_select_local_archive(prep, tmp_path_factory):
    args = fixture_args(prep, tmp_path_factory)
    args.manifest.write_bytes(args.manifest.read_bytes() + b"\n")
    with pytest.raises(UpdateError, match="UPDATE_SIGNATURE_INVALID"):
        probe.prepare(args)
    assert prep[1]._ledger()["watermarks"] == []
    assert not list((prep[1].root / "control/preparation").iterdir())


@pytest.mark.skipif(os.name != "posix", reason="The macOS signed-package fixture requires POSIX installation ownership")
def test_signed_metadata_does_not_approve_different_local_bytes(prep, tmp_path_factory):
    args = fixture_args(prep, tmp_path_factory)
    data = args.archive.read_bytes()
    args.archive.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    with pytest.raises(UpdateError, match="UPDATE_HASH_MISMATCH"):
        probe.prepare(args)
    assert not (prep[1].root / "control/preparation/prepared-preview.json").exists()


def test_final_launcher_result_wins_over_prior_terminal_text():
    output = (b'\x1b[2J{"update_status": "blocked", "error_code": "UPDATE_BUSY"}\r\n'
              b'{"update_status": "blocked", "error_code": "UPDATE_HANDOFF_FAILED"}\r\n')
    value = probe.final_update_result(output)
    state = dict(current={"version": "a"}, transaction=None, active_tree_sha256="a" * 64)
    with pytest.raises(RuntimeError, match="not another blocker"):
        probe.check_update_observation(state, state, value, "blocked")


@pytest.mark.parametrize("changed", ["current", "transaction", "active_tree_sha256"])
def test_busy_result_does_not_hide_changed_program_or_transaction(changed):
    before = dict(current={"version": "a"}, transaction=None, active_tree_sha256="a" * 64)
    result = probe.final_update_result(b'{"update_status": "blocked", "error_code": "UPDATE_BUSY"}\r\n')
    probe.check_update_observation(before, dict(before), result, "blocked")
    after = {**before, changed: "changed"}
    with pytest.raises(RuntimeError, match="changed the program or durable transaction"):
        probe.check_update_observation(before, after, result, "blocked")
