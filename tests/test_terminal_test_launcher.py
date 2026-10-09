"""Owned lifecycle checks use real processes, without substituting for UI QA."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from scripts import terminal_test_launcher as launcher

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows terminal test lifecycle")


def eventually(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Lifecycle condition timed out")


def test_fresh_profiles_do_not_reuse_settings_or_touch_user_files(tmp_path_factory, monkeypatch):
    original = tmp_path_factory / "original.json"
    original.write_text(json.dumps({"projects": [], "launcher": {"terminal": "git_bash", "configured": True,
                        "command": "previous terminal", "args": ["old"]}}), encoding="utf-8")
    provider = tmp_path_factory / "providers.json"
    provider.write_text('{"providers": [], "active_provider": ""}', encoding="utf-8")
    secrets = tmp_path_factory / "secrets.json"
    secrets.write_text('{"version":1,"secrets":{}}', encoding="utf-8")
    originals = {path: path.read_bytes() for path in (original, provider, secrets)}
    monkeypatch.setenv("GITGO_CONFIG_PATH", str(original))
    monkeypatch.setenv("GITGO_LLM_CONFIG_PATH", str(provider))
    monkeypatch.setenv("GITGO_LLM_SECRET_PATH", str(secrets))
    base = tmp_path_factory / "sessions"
    first, session1 = launcher.prepare(base=base, bun=Path(sys.executable))
    # A saved first run must not bypass the selector in the second run.
    saved = json.loads((first / "config.json").read_text())
    saved["launcher"] = {"terminal": "git_bash", "configured": True}
    launcher.write_json(first / "config.json", saved)
    second, session2 = launcher.prepare(base=base, bun=Path(sys.executable))
    assert first != second and session1["token"] != session2["token"]
    assert json.loads((second / "config.json").read_text())["launcher"] == {
        "terminal": "auto", "configured": False, "command": "", "args": []}
    assert (second / "llm_config.json").read_bytes() == originals[provider]
    assert (second / "provider_secrets.json").read_bytes() == originals[secrets]
    (second / "provider_secrets.json").write_text("temporary changed settings")
    assert all(path.read_bytes() == data for path, data in originals.items())
    env = launcher.test_environment(second, session2)
    assert env["GITGO_LAUNCH_SESSION"] == str(second / "session.json")
    launcher.cleanup(first, session1["token"], base=base)
    assert second.exists()
    launcher.cleanup(second, session2["token"], base=base)


def test_cleanup_rejects_foreign_roots_and_mismatched_ownership(tmp_path_factory):
    base = tmp_path_factory / "sessions"
    root, session = launcher.prepare(base=base, bun=Path(sys.executable))
    with pytest.raises(RuntimeError, match="ownership marker"):
        launcher.cleanup(root, str(uuid.uuid4()), base=base)
    with pytest.raises(RuntimeError, match="outside"):
        launcher.cleanup(tmp_path_factory, session["token"], base=base)
    assert root.exists()
    launcher.cleanup(root, session["token"], base=base)


def test_cleanup_retries_after_partial_deletion(tmp_path_factory, monkeypatch):
    base = tmp_path_factory / "sessions"
    root, session = launcher.prepare(base=base, bun=Path(sys.executable))
    real_rmtree = launcher.shutil.rmtree
    calls = []
    def partially_locked(path):
        calls.append(path)
        if len(calls) == 1:
            (path / "session.json").unlink()
            raise PermissionError("sqlite handle still closing")
        real_rmtree(path)
    monkeypatch.setattr(launcher.shutil, "rmtree", partially_locked)
    launcher.cleanup(root, session["token"], base=base, retry_seconds=2)
    assert len(calls) == 2 and not root.exists()


def test_real_bun_handoff_and_abrupt_close_keep_then_remove_profile(tmp_path_factory):
    bun = Path(os.environ.get("GITGO_BUN") or Path.home() / ".bun/bun.exe")
    if not bun.is_file():
        pytest.skip("Bun runtime unavailable")
    base = tmp_path_factory / "sessions"
    root, session = launcher.prepare(base=base, bun=bun)
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
    identity = launcher.WindowsProcess(owner.pid)
    session["owner"] = identity.identity()
    identity.close()
    launcher.write_json(root / "session.json", session)
    keeper_code = "import sys; from pathlib import Path; from scripts.terminal_test_launcher import keep; sys.exit(keep(Path(sys.argv[1]), sys.argv[2], base=Path(sys.argv[3])))"
    keeper = subprocess.Popen([sys.executable, "-c", keeper_code, str(root), session["token"], str(base)], cwd=launcher.ROOT,
                              creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dashboards = []
    try:
        eventually(lambda: (root / "keeper-ready.json").exists())
        module = (launcher.ROOT / "cli/dashboard/src/backend/launchSession.ts").as_posix()
        code = f'import {{registerLaunchSession}} from {json.dumps(module)}; registerLaunchSession(); setInterval(() => {{}}, 1000);'
        env = launcher.test_environment(root, session)
        for count in (1, 2):
            child = subprocess.Popen([str(bun), "-e", code], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            dashboards.append(child)
            eventually(lambda: len(list((root / "participants").glob("*.json"))) == count)
        # Closing the original coordinator and original dashboard must not
        # erase the selected child terminal's profile (handoff baton).
        dashboards[0].kill()
        dashboards[0].wait(timeout=5)
        owner.kill()
        owner.wait(timeout=5)
        time.sleep(1.5)
        assert root.exists() and keeper.poll() is None and dashboards[1].poll() is None
        # Abrupt kill deliberately prevents exit callbacks from running.
        dashboards[1].kill()
        dashboards[1].wait(timeout=5)
        assert keeper.wait(timeout=8) == 0
        assert not root.exists()
        report = json.loads((base / "last-result.json").read_text())
        assert report["cleaned"] and report["participants"] == 2
        assert report["visible_ui_verified"] is False
    finally:
        for proc in [*dashboards, owner, keeper]:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        if root.exists():
            launcher.cleanup(root, session["token"], base=base)


def test_wrong_live_process_is_reported_instead_of_waiting_or_deleting(tmp_path_factory):
    base = tmp_path_factory / "sessions"
    root, session = launcher.prepare(base=base, bun=Path(sys.executable))
    identity = launcher.WindowsProcess(os.getpid())
    wrong = {**identity.identity(), "token": session["token"], "startedAt": identity.started_at - 60000}
    identity.close()
    launcher.write_json(root / "participants/foreign.json", wrong)
    assert launcher.keep(root, session["token"], base=base) == 1
    assert root.exists()
    error = json.loads((base / f"{root.name}.cleanup-error.json").read_text())
    assert "identity mismatch" in error["errors"][0]
    launcher.cleanup(root, session["token"], base=base)
