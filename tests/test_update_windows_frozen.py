"""Two real Windows frozen bundles; injected signed stream, no public feed/UI.

The source coordinator invokes the real shared transaction; startup/recovery
use the physically installed immutable frozen bootstrap. Not visible-terminal
acceptance or a publisher Release. No paid provider and no daily installation.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest

from backend.core.update import handoff
from backend.core.update.tree import tree_digest
from backend.core.update.installation import open_managed
from backend.core.update.persistence import lock_file
from backend.core.update.validation import UpdateError
from tests.test_update_two_frozen_versions import actual_lab, finish_disposable_handoff
from tests.test_update_manifest import signing
from tests.test_update_transaction import Crash

pytestmark = pytest.mark.skipif(os.name != "nt" or not os.environ.get("GITGO_UPDATE_PREVIOUS_STAGE")
    or not os.environ.get("GITGO_UPDATE_CANDIDATE_STAGE"), reason="Requires two actual Windows frozen bundles")


def test_real_windows_upgrade_rollback_keep_program_and_user_data(actual_lab, monkeypatch):
    store, prepare, boot, before, old, new, _ = actual_lab
    service, selected = prepare()
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_apply("preview", selected, "c" * 32)
        result = handoff._consume_request(store, nonce, 75, finish_disposable_handoff())
        assert result.status == "committed", result
    boot(new)
    water = store._ledger()["watermarks"]
    selection = store.rollback_selection()
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_rollback(selection["rollback_id"], "d" * 32)
        result = handoff._consume_request(store, nonce, 75, finish_disposable_handoff())
        assert result.status == "rolled_back", result
    boot(old)
    assert tree_digest(store.root / "active") == before
    assert store._ledger()["watermarks"] == water


def test_broken_real_windows_dashboard_restores_bootable_predecessor(actual_lab, monkeypatch):
    store, prepare, boot, before, old, _, _ = actual_lab
    service, selected = prepare(broken=True)
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_apply("preview", selected, "c" * 32)
        result = handoff._consume_request(store, nonce, 75, finish_disposable_handoff())
        assert result.status == "rolled_back" and result.error_code == "UPDATE_HEALTH_FAILED", result
    boot(old)
    assert tree_digest(store.root / "active") == before


def test_frozen_windows_bootstrap_recovers_after_switch_interruption(actual_lab, monkeypatch):
    store, prepare, boot, before, old, _, env = actual_lab
    service, selected = prepare()
    saved = store._save
    def crash(ledger, state=None, **kwargs):
        result = saved(ledger, state, **kwargs)
        if state == "CHECK_NEW": raise Crash()
        return result
    monkeypatch.setattr(store, "_save", crash)
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_apply("preview", selected, "c" * 32)
        with pytest.raises(Crash): handoff._consume_request(store, nonce, 75, finish_disposable_handoff())
    host = store.root / "bootstrap/gitgo-host/gitgo-host.exe"
    result = subprocess.run([str(host), "--gitgo-internal-role", "managed-recover"], env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "rolled_back"
    boot(old)
    assert tree_digest(store.root / "active") == before


def test_real_windows_managed_launcher_and_live_lease(actual_lab):
    store, _, _, before, old, _, env = actual_lab
    # CreateProcess takes the cmd command line directly; list2cmdline's C-argv
    # escaping of nested quotes is not cmd.exe's /s /c quoting grammar.
    command = 'cmd.exe /d /s /c ""' + str(store.root / 'gitgo.cmd') + '" --version"'
    result = subprocess.run(command,
        env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Gitgo " + old
    with store.runtime_lease():
        assert open_managed(store.root).recover().status == "busy"
    assert tree_digest(store.root / "active") == before
