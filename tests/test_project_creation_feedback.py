import io
import json

import pytest

from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.core.native_host import NativeHost
from backend.models import FileAccess, RepoNode


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("conflict", ["workspace", "name"])
def test_creation_conflict_reports_owner_and_preserves_registration(
    tmp_path_factory, monkeypatch, archived, conflict,
):
    workspace = tmp_path_factory / "existing"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_text("keep", encoding="utf-8")
    config = tmp_path_factory / "config.json"
    monkeypatch.setattr(ConfigManager, "default_path", staticmethod(lambda: config))
    ConfigManager.save(Config(projects=[ProjectConfig(
        name="Existing", archived=archived,
        workspace=RepoNode(file_access=FileAccess(path=str(workspace))),
    )]))
    before = config.read_bytes()
    requested_workspace = workspace if conflict == "workspace" else tmp_path_factory / "new"
    stdout = io.StringIO()
    host = NativeHost(stdout=stdout)
    try:
        host._handle_request({
            "request_id": "create-conflict", "operation": "project.create",
            "arguments": {
                "name": "demo" if conflict == "workspace" else "existing",
                "workspace_path": str(requested_workspace),
                "workspace_mode": "create_new",
            },
        })
        response = json.loads(stdout.getvalue())
    finally:
        host.close()
    assert response["ok"] is False
    error = response["error"]
    assert error["code"] == ("WORKSPACE_ALREADY_REGISTERED" if conflict == "workspace" else "DUPLICATE_NAME")
    assert error["details"] == {
        "existing_project": "Existing", "archived": archived, "workspace": str(workspace.resolve()),
    }
    assert "Existing" in error["message"]
    assert ("restore it from bin" if archived else "different") in error["message"].lower()
    assert config.read_bytes() == before
    assert source.read_text(encoding="utf-8") == "keep"
    if conflict == "name":
        assert not requested_workspace.exists()
