"""Prepared resource grants cover actual fixed Publish effect/recovery paths."""
import pytest

from backend.core.config import Config, ConfigManager
from backend.core.loop.permission_broker import out_of_scope_resources
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.publish.state import Repository, read_ledger
from backend.core.storage import get_storage
from backend.core.tools.publish_service import build_publish_tools
from tests.test_file_feedback_contract import context
from tests.test_publish_commands import setup, prepare


def _outside_formal(setup, root):
    project = setup[1]
    path = root / "outside-formal"
    path.mkdir()
    project.backup_path = str(path)
    ConfigManager.save(Config(projects=[project]))
    return path


def test_import_package_requires_formal_scope_even_when_archive_is_in_workspace(setup, tmp_path_factory):
    path = _outside_formal(setup, tmp_path_factory)
    item = prepare(setup)["item"]
    tool = build_publish_tools(setup[1])["publish_import_package"]
    args = {"request_id": "import-scope", "item_id": item["id"], "expected_revision": item["revision"],
        "path": str(setup[2] / "input.zip"), "sha256": "a" * 64}
    assert out_of_scope_resources(tool.prepare_args(args), str(setup[2])) == [str(path.resolve())]
    result = ToolPipeline().execute({"name": tool.name, "args": args}, tool, context(setup[2], [tool.name]), "import")
    assert result.is_error and result.diagnostics["code"] == "RESOURCE_SCOPE_APPROVAL_REQUIRED"
    assert read_ledger(setup[2])[0]["items"][item["id"]]["revision"] == item["revision"]


def test_branch_merge_verification_requires_formal_scope(setup, tmp_path_factory):
    path = _outside_formal(setup, tmp_path_factory)
    item = prepare(setup)["item"]
    tool = build_publish_tools(setup[1])["publish_branch"]
    args = {"request_id": "branch-scope", "item_id": item["id"], "expected_revision": item["revision"],
        "action": "prepare_delete", "merge_item_id": "formal-item", "owner": "owner", "reason": "retain archive"}
    assert out_of_scope_resources(tool.prepare_args(args), str(setup[2])) == [str(path.resolve())]
    result = ToolPipeline().execute({"name": tool.name, "args": args}, tool, context(setup[2], [tool.name]), "branch")
    assert result.is_error and result.diagnostics["code"] == "RESOURCE_SCOPE_APPROVAL_REQUIRED"


@pytest.mark.parametrize("kind,key", [("tag", "repository"), ("export", "destination"), ("download", "destination")])
def test_recovery_grants_fixed_historical_location_not_current_formal(setup, tmp_path_factory, kind, key):
    current = _outside_formal(setup, tmp_path_factory)
    historical = tmp_path_factory / (kind + "-historical")
    historical.mkdir()
    item = prepare(setup)["item"]
    repo = Repository(str(setup[2]), get_storage(str(setup[2])))
    ledger, ref = repo.load()
    ledger["items"][item["id"]]["distribution_operation"] = {"id": "old-op", "kind": kind, "state": "uncertain",
        "binding": item["binding"], key: str(historical)}
    repo.save(ledger, ref)
    tool = build_publish_tools(setup[1])["publish_distribution_reconcile"]
    args = {"request_id": "recover-scope", "item_id": item["id"], "expected_revision": item["revision"]}
    prepared = tool.prepare_args(args)
    assert out_of_scope_resources(prepared, str(setup[2])) == [str(historical.resolve())]
    assert str(current) not in out_of_scope_resources(prepared, str(setup[2]))
    result = ToolPipeline().execute({"name": tool.name, "args": args}, tool, context(setup[2], [tool.name]), "recover")
    assert result.is_error and result.diagnostics["code"] == "RESOURCE_SCOPE_APPROVAL_REQUIRED"
    assert read_ledger(setup[2])[0]["items"][item["id"]]["distribution_operation"]["state"] == "uncertain"
    # A path swapped after admission cannot use the original grant.
    ledger, ref = repo.load()
    ledger["items"][item["id"]]["distribution_operation"][key] = str(current)
    repo.save(ledger, ref)
    response = tool.execute({**prepared, "_publish_actor": "test-process"})
    assert response["error"] == "PUBLISH_CONTEXT_UNAVAILABLE"


def test_external_mutations_cannot_inherit_unscoped_agent_permission(setup):
    from backend.core.loop.agent_tool import ApprovalMode, ToolEffect
    from backend.core.loop.operation_policy import decide_tool_operation, PolicyDisposition
    tools = build_publish_tools(setup[1])
    for name in ("share_publish", "collaborate_send", "tag", "release", "branch_apply"):
        tool = tools["publish_" + name]
        assert tool.effect == ToolEffect.EXTERNAL and tool.approval == ApprovalMode.ASK
        assert tool.approval_per_invocation
        assert decide_tool_operation(tool).disposition == PolicyDisposition.ASK


@pytest.mark.parametrize("name", ["publish_history", "publish_diff"])
def test_history_queries_require_the_fixed_external_repository_grant(setup, tmp_path_factory, monkeypatch, name):
    from backend.core.publish import history
    outside = tmp_path_factory / "history-source"
    outside.mkdir()
    scope = {"repository": str(outside.resolve())}
    monkeypatch.setattr(history, "history_scope", lambda *args: scope)
    tool = build_publish_tools(setup[1])[name]
    args = {"item_id": "candidate", "head": "contribution:0", **({"base_oid": "a" * 40} if name == "publish_diff" else {})}
    prepared = tool.prepare_args(args)
    assert tool.read_only and out_of_scope_resources(prepared, str(setup[2])) == [str(outside.resolve())]
    result = ToolPipeline().execute({"name": name, "args": args}, tool, context(setup[2], [name]), "history")
    assert result.is_error and result.diagnostics["code"] == "RESOURCE_SCOPE_APPROVAL_REQUIRED"
    scope["repository"] = str(setup[2])
    assert tool.execute(prepared)["error"] == "PUBLISH_CONTEXT_UNAVAILABLE"
