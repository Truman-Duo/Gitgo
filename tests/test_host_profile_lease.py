from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.core.frontend_origin import frontend_origin
from backend.core.host_profile_lease import HostProfileLease


def test_origin_is_bounded_diagnostic_data_not_configuration(monkeypatch):
    value = {"version": 1, "terminal": "git_bash", "platform": "win32", "source": "terminal_handoff",
             "state_home": "never honoured", "permission": "never honoured"}
    monkeypatch.setenv("GITGO_FRONTEND_ORIGIN", json.dumps(value))
    assert frontend_origin() == {key: value[key] for key in ("version", "terminal", "platform", "source")}
    monkeypatch.setenv("GITGO_FRONTEND_ORIGIN", json.dumps({**value, "terminal": "bad\nterminal"}))
    with pytest.raises(ValueError, match="FRONTEND_ORIGIN_INVALID"):
        frontend_origin()


def test_real_process_lock_blocks_duplicate_and_recovers_after_hard_close(tmp_path_factory):
    path = tmp_path_factory / "shared"
    code = "from pathlib import Path;from backend.core.host_profile_lease import HostProfileLease;import sys,time;lease=HostProfileLease(Path(sys.argv[1]));print('ready',flush=True);time.sleep(60)"
    child = subprocess.Popen([sys.executable, "-u", "-c", code, str(path)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="HOST_PROFILE_IN_USE"):
            HostProfileLease(path)
        with HostProfileLease(tmp_path_factory / "independent"):
            pass
        child.kill()
        child.wait(timeout=5)
        # The stable lock file remains but the OS lock is released on death.
        with HostProfileLease(path):
            assert (path / "dashboard-owner.lock").exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
