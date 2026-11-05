"""Exact approvals expire when the implementation that can use them changes."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from backend.core.tools.publish_service import build_publish_tools
from backend.core.loop.permission_broker import tool_contract_digest, grant_is_current
from tests.test_publish_commands import setup


@pytest.mark.parametrize("module", ["publish/distribution.py", "publish/sharing.py", "remote/publish_platform.py"])
def test_publish_approval_cannot_survive_changed_host_business_code(setup, monkeypatch, module):
    tool = build_publish_tools(setup[1])["publish_release"]
    process = SimpleNamespace(process_id="operator")
    grant = {"process_id": "operator", "tool_contract_digest": tool_contract_digest(tool),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    assert grant_is_current(grant, process, tool)
    original = Path.read_bytes
    def changed(path):
        body = original(path)
        return body + b"\n# changed implementation\n" if path.as_posix().endswith(module) else body
    monkeypatch.setattr(Path, "read_bytes", changed)
    assert not grant_is_current(grant, process, tool)
