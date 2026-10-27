"""Startup skips discovery only when no verified terminal will be launched."""
import sys

import pytest

from backend.core import terminal_launcher as terminals
from backend.core.application import ApplicationServices, OperationError
from backend.core.config import ConfigManager


@pytest.mark.parametrize("launcher,attached,configured", [
    ({"terminal": "current"}, False, True),
    ({"terminal": "git_bash", "configured": True}, True, True),
    ({"terminal": "auto"}, False, False),
    ({"terminal": "git_bash", "configured": False}, False, False),
])
def test_staying_or_first_run_does_not_probe_other_terminals(monkeypatch, launcher, attached, configured):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(terminals, "terminal_inventory", lambda *_: pytest.fail("unexpected executable discovery"))
    result = terminals.startup_terminal_inventory(launcher, attached=attached)
    assert result["discovery"] == "deferred"
    assert result["selected"] == launcher["terminal"]
    assert result["configured"] is configured
    assert result["effective"]["id"] == "current"
    assert all(not option.get("identity") and not option["command"] for option in result["options"])


@pytest.mark.parametrize("terminal", ["auto", "git_bash", "windows_terminal", "custom", "missing"])
def test_new_configured_launch_keeps_full_verification(monkeypatch, terminal):
    monkeypatch.setattr(sys, "platform", "win32")
    launcher = {"terminal": terminal, "configured": True}
    calls = []
    expected = {"effective": {"id": "current"}, "warnings": ["unverified fixture rejected"]}
    monkeypatch.setattr(terminals, "terminal_inventory", lambda value: calls.append(value) or expected)
    assert terminals.startup_terminal_inventory(launcher) is expected
    assert calls == [launcher]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platform_startup_retains_existing_discovery(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    expected = object()
    monkeypatch.setattr(terminals, "terminal_inventory", lambda _: expected)
    assert terminals.startup_terminal_inventory({"terminal": "current"}, attached=True) is expected


def test_application_default_catalog_and_startup_use_distinct_read_paths(monkeypatch):
    from backend.core import terminal_metrics
    launcher = {"terminal": "current"}
    monkeypatch.setattr(ConfigManager, "load", lambda: type("Configuration", (), {"launcher": launcher})())
    monkeypatch.setattr(terminals, "terminal_inventory", lambda value: {"source": "full", "launcher": value})
    monkeypatch.setattr(terminals, "startup_terminal_inventory", lambda value, attached: {"source": "startup", "attached": attached})
    metrics = {"kind": "windows_console", "codepoint_widths": {"183": 2}}
    monkeypatch.setattr(terminal_metrics, "console_display_metrics", lambda: metrics)
    services = ApplicationServices.__new__(ApplicationServices)
    assert services.config_terminals() == {"source": "full", "launcher": launcher, "display_metrics": metrics}
    assert services.config_terminals(startup=True, attached=True) == {"source": "startup", "attached": True, "display_metrics": metrics}
    with pytest.raises(OperationError):
        services.config_terminals(startup="true")
