"""Actual Mac programs and frozen public launchers in disposable installations."""
import json
import os
import subprocess
import sys
from contextlib import nullcontext

import pytest

from backend.core.update import handoff, deferred
from backend.core.update.health import frozen_health_check
from backend.core.update.tree import tree_digest
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
    assert handoff.strategy(store) == "immediate"
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


def confirm_with_actual_ui(store, env, expected_status, *, automatic=False, competing_runtime=False):
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
    terminal_env = {**env, "TERM": "xterm-256color", "FORCE_COLOR": "3"}
    terminal_env.pop("NO_COLOR", None)
    proc = subprocess.Popen([str(store.root / "gitgo")], stdin=slave, stdout=slave, stderr=slave,
        env=terminal_env, start_new_session=True)
    os.close(slave)
    output = bytearray()
    screen = TerminalScreen()
    def wait_for(text, *, after=0):
        end = time.monotonic() + 90
        while text not in screen.transcript() or len(output) <= after:
            assert time.monotonic() < end, f"Actual Mac UI did not reach {text!r}: {screen.transcript()[-1200:]}"
            pump()
    def pump():
        if select.select([master], [], [], .1)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                chunk = b""
            assert chunk, f"Actual Mac UI closed: {screen.transcript()[-1200:]}"
            output.extend(chunk); screen.feed(chunk)
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
        if expected_status is None:
            return
        # The initial menu shows only eight commands; /update is below that
        # visible window. Wait for the opened editor, not an offscreen item.
        screen.forget_history()
        start = len(output); send("/"); wait_for("/archive", after=start)
        prefix = "/"
        for letter in ("quit" if automatic else "update"):
            start = len(output); send(letter); prefix += letter
            wait_for(prefix, after=start)
        assert not termios.tcgetattr(master)[0] & termios.ICRNL, "Mac PTY still translates Enter to newline"
        start = len(output); send("\r")
        if automatic:
            wait_for("Quit Gitgo", after=start)
        else:
            wait_for("Gitgo Software Update", after=start)
            wait_for("Downloaded and verified")
            activate_menu_item(screen, "Review restart / update", send, pump)
            wait_for("attempt the selected update?")
        with store.runtime_lease() if competing_runtime else nullcontext():
            start = len(output); send("\r")
            wait_for(f'"update_status": "{expected_status}"', after=start)
            proc.wait(timeout=30)
            assert proc.returncode == 0
        assert deferred.pending_status(store) is None
        assert b"restart_required" not in output
        if expected_status == "blocked":
            assert b"UPDATE_BUSY" in output
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


@pytest.mark.parametrize("broken", [False, True], ids=["upgrade", "startup-failure"])
def test_actual_mac_launcher_updates_after_exit_in_same_boot(actual_lab, broken):
    store, prepare, boot, before, old, new, env = actual_lab
    prepare(broken=broken)
    boot_id = deferred.boot_session()
    confirm_with_actual_ui(store, env, "rolled_back" if broken else "committed")
    transaction = store._ledger()["transaction"]
    assert transaction["state"] == ("ROLLED_BACK" if broken else "COMMITTED")
    assert transaction["failure_code"] == ("UPDATE_HEALTH_FAILED" if broken else None)
    boot(old if broken else new)
    confirm_with_actual_ui(store, env, None)
    assert deferred.boot_session() == boot_id
    assert (tree_digest(store.root / "active") == before) is broken


def test_actual_mac_automatic_update_on_clean_exit(actual_lab):
    store, prepare, boot, _, _, new, env = actual_lab
    prepare()
    boot_id = deferred.boot_session()
    confirm_with_actual_ui(store, env, "committed", automatic=True)
    boot(new)
    assert deferred.boot_session() == boot_id


def test_actual_mac_other_runtime_lease_blocks_replacement(actual_lab):
    store, prepare, boot, before, old, new, env = actual_lab
    prepare()
    confirm_with_actual_ui(store, env, "blocked", competing_runtime=True)
    assert store._ledger()["transaction"] is None
    # Disarm ordinary-quit installation before using the launcher as a probe.
    from backend.core.update.preparation_service import UpdatePreparationService
    UpdatePreparationService(store_loader=lambda: store).configure("preview", True, True, False, 0)
    boot(old)
    assert tree_digest(store.root / "active") == before
