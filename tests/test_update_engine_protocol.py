"""The actual coordinator emitter must remain readable by installed launchers."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from backend.core.update import coordinator, data_transition, handoff
from backend.core.update.transaction import TransactionResult
from backend.core.update.validation import fields


@pytest.mark.parametrize("status", ["idle", "committed", "rolled_back", "blocked", "busy", "recovery_required"])
@pytest.mark.parametrize("diagnostic", [None, {"stage": "CHECK_NEW", "reason": "collect_timeout"}])
@pytest.mark.parametrize("recover", [False, True])
def test_frozen_engine_emits_legacy_wire_even_with_internal_diagnostics(tmp_path_factory, monkeypatch, capsys, status, diagnostic, recover):
    root = tmp_path_factory / "installation"
    cache = root / "control/engines" / ("a" * 64)
    program = cache / "internal/gitgo-host" / ("gitgo-host.exe" if sys.platform == "win32" else "gitgo-host")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(program))
    result = TransactionResult(status, "1.0.1", None, diagnostic)
    store = SimpleNamespace(root=root, recover=lambda: result)
    ticket = {"deferred": False, "nonce": "b" * 32, "exit_code": 75, "cleanup_complete": True}
    monkeypatch.setattr(coordinator, "_read", lambda _: (ticket, cache))
    monkeypatch.setattr(handoff, "_consume_request", lambda *_: result)
    args = SimpleNamespace(propose=False, recover=recover)
    assert coordinator._execute_main(store, args) == 0
    message = json.loads(capsys.readouterr().out)
    # This is the exact strict field set of immutable API 10/11 launchers.
    fields(message, {"status", "version", "error_code"})
    assert message == {"status": status, "version": "1.0.1", "error_code": None}
    assert asdict(result)["health_failure"] == diagnostic


@pytest.mark.parametrize("queued", [False, True])
def test_automatic_proposal_also_uses_the_legacy_wire(tmp_path_factory, monkeypatch, capsys, queued):
    from backend.core.update import automatic
    root = tmp_path_factory / "installation"
    program = root / "active/internal/gitgo-host" / ("gitgo-host.exe" if sys.platform == "win32" else "gitgo-host")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(program))
    monkeypatch.setattr(automatic, "queue_on_clean_exit", lambda *_: {"status": "exit_required"} if queued else None)
    args = SimpleNamespace(propose=True, recover=False, nonce="a" * 32)
    assert coordinator._execute_main(SimpleNamespace(root=root), args) == 0
    message = json.loads(capsys.readouterr().out)
    fields(message, {"status", "version", "error_code"})
    assert message["status"] == ("exit_required" if queued else "idle")


def test_fixed_legacy_mac_engine_never_claims_active_engine_capabilities(monkeypatch):
    from backend.core.update.bootstrap_contract import MACOS_QUIT_API, MACOS_PROBE_BROKER_API, ENGINE_DELEGATION_APIS
    monkeypatch.setattr(data_transition, "ENGINE_VERSION", "9.0.0")
    registration = {"bootstrap_api": MACOS_QUIT_API, "updater_version": "0.2.0"}
    assert data_transition.executing_engine_version(registration) == "0.2.0"
    assert MACOS_PROBE_BROKER_API in ENGINE_DELEGATION_APIS
    assert data_transition.executing_engine_version({**registration, "bootstrap_api": MACOS_PROBE_BROKER_API}) == "9.0.0"
