"""The first-release UI is read-only and cannot confer update authority."""

import pytest

from backend.core.application import ApplicationServices, OperationError
from backend.core.config import ConfigManager
from backend.core.llm_config import LLMConfigManager
from backend.version import __version__


def test_status_and_version_need_no_user_configuration_or_network(monkeypatch):
    services = ApplicationServices()

    def forbidden(*args, **kwargs):
        pytest.fail("read-only status accessed configuration or network")

    monkeypatch.setattr(ConfigManager, "load", forbidden)
    monkeypatch.setattr(LLMConfigManager, "load", forbidden)
    import socket
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setenv("GITGO_UPDATE_ENABLED", "1")
    monkeypatch.setenv("GITGO_UPDATE_MANIFEST_URL", "https://example.invalid/metadata")
    assert services.invoke("product.version") == {
        "product_id": "gitgo-terminal", "version": __version__,
    }
    expected = {
        "schema_version": 1, "product_id": "gitgo-terminal", "version": __version__,
        "status": "disabled", "enabled": False, "automatic_check": False,
        "channel": "stable", "can_apply": False,
        "blockers": ["MANAGED_INSTALLATION_REQUIRED", "RELEASE_TRUST_REQUIRED", "PLATFORM_ACCEPTANCE_REQUIRED"],
    }
    actual = services.invoke("update.status")
    assert actual == expected
    actual["blockers"].clear()
    assert services.invoke("update.status") == expected


@pytest.mark.parametrize("operation", ["product.version", "update.status"])
def test_no_update_enable_or_arbitrary_path_arguments(operation):
    services = ApplicationServices()
    with pytest.raises(OperationError) as exc:
        services.invoke(operation, {"enabled": True, "root": "/not-an-installation"})
    assert exc.value.code == "INVALID_ARGUMENTS"
    assert "update.apply" not in services.operations
    assert "update.enable" not in services.operations


def test_status_uses_executing_host_version_not_editable_config(monkeypatch):
    import backend.version
    monkeypatch.setattr(backend.version, "__version__", "2.0.0-preview.9")
    services = ApplicationServices()
    assert services.invoke("product.version")["version"] == "2.0.0-preview.9"
    assert services.invoke("update.status")["version"] == "2.0.0-preview.9"


def test_native_host_advertises_and_dispatches_only_readonly_update_operations():
    import io
    from backend.core.native_host import NativeHost
    host = NativeHost(stdout=io.StringIO())
    try:
        capabilities = host._dispatch({"operation": "host.capabilities"})
        assert "update.status" in capabilities["operations"]
        assert "product.version" in capabilities["operations"]
        assert "update.apply" not in capabilities["operations"]
        status = host._dispatch({"operation": "update.status"})
        assert status["enabled"] is False
        assert status["version"] == __version__
        assert host._daemons == {}
    finally:
        host.close()
