"""Actual Mac programs in quiescent disposable installations, NOT reboot QA.

The source coordinator calls the private engine in an unused test installation.
No ordinary Mac session is misreported clean or allowed immediate replacement.
Boot-session gating is separately tested; a real manual reboot remains required.
"""
import json
import os
import subprocess
import sys

import pytest

from backend.core.update import handoff, deferred
from backend.core.update.health import frozen_health_check
from backend.core.update.tree import tree_digest
from backend.core.update.persistence import lock_file
from tests.test_update_manifest import signing
from tests.test_update_transaction import Crash
from tests.test_update_two_frozen_versions import actual_lab

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not os.environ.get("GITGO_UPDATE_PREVIOUS_STAGE")
    or not os.environ.get("GITGO_UPDATE_CANDIDATE_STAGE"), reason="Requires two fresh actual macOS bundles")


def request(store, selected):
    return {"schema_version": 1, "installation_id": store.registration["installation_id"],
        "nonce": "a" * 32, "current": store._ledger()["current"], "channel": "preview", "candidate_id": selected}


def test_actual_quiescent_mac_upgrade_and_retained_rollback(actual_lab):
    store, prepare, boot, before, old, new, _ = actual_lab
    _, selected = prepare()
    assert handoff.strategy(store) == "after_reboot"
    # No running old app was launched beyond bounded --version. This is an
    # explicit engine laboratory, not the production handoff/reboot wrapper.
    assert handoff._perform_request(store, request(store, selected)).status == "committed"
    boot(new)
    water = store._ledger()["watermarks"]
    previous = store.rollback_selection()
    assert store._rollback(previous["rollback_id"], health_check=frozen_health_check).status == "rolled_back"
    boot(old)
    assert tree_digest(store.root / "active") == before and store._ledger()["watermarks"] == water


def test_signed_but_broken_mac_frontend_restores_real_old_program(actual_lab):
    store, prepare, boot, before, old, _, _ = actual_lab
    _, selected = prepare(broken=True)
    result = handoff._perform_request(store, request(store, selected))
    assert result.status == "rolled_back" and result.error_code == "UPDATE_HEALTH_FAILED"
    boot(old)
    assert tree_digest(store.root / "active") == before


def test_actual_mac_bootstrap_recovers_an_interrupted_engine(actual_lab, monkeypatch):
    store, prepare, boot, before, old, _, env = actual_lab
    _, selected = prepare()
    save = store._save
    def crash(ledger, state=None, **kwargs):
        result = save(ledger, state, **kwargs)
        if state == "CHECK_NEW": raise Crash()
        return result
    monkeypatch.setattr(store, "_save", crash)
    with pytest.raises(Crash): handoff._perform_request(store, request(store, selected))
    result = subprocess.run([str(store.root / "bootstrap/gitgo-host/gitgo-host"),
        "--gitgo-internal-role", "managed-recover"], env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "rolled_back"
    boot(old)
    assert tree_digest(store.root / "active") == before


def test_actual_bootstrap_does_not_apply_pending_request_in_current_boot(actual_lab):
    store, prepare, boot, before, old, new, _ = actual_lab
    _, selected = prepare()
    # Publish through the source driver in this explicit unused test store;
    # the following launch exercises the actual physical frozen bootstrap.
    with lock_file(store.root / "control/preparation.lock", expected_identity=store.registration["lock_identities"]["preparation.lock"]):
        assert deferred.publish(store, request(store, selected)).status == "restart_required"
    assert deferred.boot_session() == deferred._read(store)["boot_session"]
    boot(old)
    assert store._ledger()["current"]["version"] == old
    assert deferred.pending_status(store)["version"] == new
    assert tree_digest(store.root / "active") == before
