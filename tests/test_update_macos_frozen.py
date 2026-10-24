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


def confirm_with_actual_ui(store, env, *, rollback=False):
    """Hidden PTY process/input evidence, not formal visible terminal QA."""
    import fcntl
    import pty
    import select
    import struct
    import termios
    import time
    from pathlib import Path
    from backend.core.update.installation import open_managed
    from tests.terminal_screen import TerminalScreen, activate_menu_item, settle_screen
    Path(env["GITGO_CONFIG_PATH"]).write_text(json.dumps({"projects": [], "language": "en",
        "launcher": {"terminal": "current", "configured": True}}))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    proc = subprocess.Popen([str(store.root / "gitgo")], stdin=slave, stdout=slave, stderr=slave,
        env={**env, "TERM": "xterm-256color"}, start_new_session=True)
    os.close(slave)
    output = bytearray()
    screen = TerminalScreen()
    def wait_for(text, *, after=0):
        end = time.monotonic() + 90
        while text not in screen.transcript() or len(output) <= after:
            assert time.monotonic() < end, f"Actual Mac UI did not reach {text!r}"
            pump()
    def pump():
        if select.select([master], [], [], .1)[0]:
            try:
                chunk = os.read(master, 65536)
                output.extend(chunk); screen.feed(chunk)
            except OSError: raise AssertionError("Actual Mac UI closed before confirmation")
            assert len(output) < 4 * 1024**2
        else:
            assert proc.poll() is None, "Actual Mac UI exited before confirmation"
    def send(value):
        settle_screen(screen, pump)
        os.write(master, value.encode())
    try:
        wait_for("Providers")
        screen.forget_history()
        start = len(output); send("\x1b"); wait_for("No projects yet.", after=start)
        # The initial menu shows only eight commands; /update is below that
        # visible window. Wait for the opened editor, not an offscreen item.
        screen.forget_history()
        start = len(output); send("/"); wait_for("/archive", after=start)
        prefix = "/"
        for letter in "update":
            start = len(output); send(letter); prefix += letter
            wait_for(prefix, after=start)
        assert not termios.tcgetattr(master)[0] & termios.ICRNL, "Mac PTY still translates Enter to newline"
        start = len(output); send("\r"); wait_for("Gitgo Software Update", after=start)
        wait_for("After a manual macOS restart")
        if not rollback:
            wait_for("Downloaded and verified")
            activate_menu_item(screen, "Review restart / update", send, pump)
        else:
            wait_for("Retained version")
            activate_menu_item(screen, "Review rollback", send, pump)
        wait_for("manually restart macOS")
        start = len(output); send("\r")
        wait_for('"update_status": "restart_required"', after=start)
        proc.wait(timeout=30)
        assert proc.returncode == 0
        pending = deferred.pending_status(store)
        assert pending is not None and pending["action"] == ("rollback" if rollback else "apply")
        return pending
    finally:
        if os.environ.get("GITGO_UPDATE_LAB_LOG"):
            try:
                fd = os.open(os.environ["GITGO_UPDATE_LAB_LOG"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle: handle.write(output)
            except OSError: pass
        # Only a directly owned disposable launcher. No PID/PGID snapshots.
        if proc.poll() is None:
            try: os.write(master, b"\x03"); proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                proc.terminate()
                try: proc.wait(timeout=10)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
        os.close(master)
        end = time.monotonic() + 10
        while open_managed(store.root).recover().status == "busy" and time.monotonic() < end:
            time.sleep(.1)
        assert open_managed(store.root).recover().status != "busy"


def test_actual_mac_ui_confirmation_saves_intent_but_does_not_replace(actual_lab):
    store, prepare, boot, before, old, new, env = actual_lab
    prepare()
    assert confirm_with_actual_ui(store, env)["version"] == new
    boot(old)
    assert tree_digest(store.root / "active") == before
