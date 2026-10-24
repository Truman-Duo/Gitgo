"""Old checkpoint data is readable, but never an unbound execution grant."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from backend.core.loop.agent_tool import AgentTool, ApprovalMode
from backend.core.loop.execution_contract import HOST_COMPUTE
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.permission_broker import (
    arguments_digest, authorize_external_resources, consume_exact_grant,
    create_permission_request, grant_from_decision, grant_is_current, matching_grant,
    tool_contract_digest,
)


def fixture(root):
    calls = []
    tool = AgentTool("approved", "test", {}, lambda args: calls.append(args) or {"ok": True},
                     execution_contract=HOST_COMPUTE, approval=ApprovalMode.ASK)
    process = AgentProcess(process_id="legacy", role="worker", ring_level=RingLevel.RING_0,
                           active_task_id="task", worktree_path=str(root), session=AgentSession())
    return tool, process, calls


def grant_for(tool, process, resource):
    return {"process_id": process.process_id, "task_id": process.active_task_id,
            "tool_name": tool.name, "effect": "read", "resource": str(resource),
            "scope": "task", "remaining_uses": 1, "arguments_digest": arguments_digest({}),
            "tool_contract_digest": tool_contract_digest(tool),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}


@pytest.mark.parametrize("missing", ["tool_contract_digest", "expires_at", "both"])
def test_all_admission_paths_refuse_legacy_grant(tmp_path_factory, missing):
    tool, process, calls = fixture(tmp_path_factory)
    grant = grant_for(tool, process, tmp_path_factory)
    for field in (["tool_contract_digest", "expires_at"] if missing == "both" else [missing]):
        grant.pop(field)
    process.approval_grants = [grant]
    assert matching_grant(process, tool.name, {}, per_invocation=False, tool=tool, consume=True) is None
    assert consume_exact_grant(process, tool.name, {}) is None
    allowed, error = authorize_external_resources(process, tool.name, "read", {}, [str(tmp_path_factory)], tool=tool)
    assert allowed == [] and error
    ctx = ExecutionContext(process=process, session=process.session, workspace_path=str(tmp_path_factory))
    result = ToolPipeline().execute({"name": tool.name, "args": {}}, tool, ctx, "legacy")
    assert result.diagnostics["code"] == "APPROVAL_GRANT_INVALID"
    assert grant["remaining_uses"] == 1 and not calls


def test_name_only_artifact_does_not_approve_execution(tmp_path_factory):
    tool, process, calls = fixture(tmp_path_factory)
    ctx = ExecutionContext(process=process, session=process.session, workspace_path=str(tmp_path_factory),
                           artifacts={"approvals": {tool.name}})
    result = ToolPipeline().execute({"name": tool.name, "args": {}}, tool, ctx, "legacy-name")
    assert result.diagnostics["code"] == "APPROVAL_GRANT_INVALID" and not calls


@pytest.mark.parametrize("digest,expiry", [
    (None, "valid"), ("", "valid"), ("old", "valid"), ("g" * 64, "valid"),
    ("a" * 64, ""), ("a" * 64, "invalid"), ("a" * 64, "2026-01-01T00:00:00"),
    ("a" * 64, 123),
])
def test_malformed_grants_refused_without_tool(digest, expiry):
    if expiry == "valid":
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    assert not grant_is_current({"process_id": "worker", "tool_contract_digest": digest,
                                 "expires_at": expiry}, SimpleNamespace(process_id="worker"))


@pytest.mark.parametrize("missing", ["tool_contract_digest", "expires_at"])
def test_old_pending_decision_cannot_mint_valid_grant(tmp_path_factory, missing):
    tool, process, _ = fixture(tmp_path_factory)
    create_permission_request(process, {"purpose": "legacy request test", "tool_name": tool.name,
                                       "arguments": {}}, {tool.name: tool}, str(tmp_path_factory))
    process.pending_decision["permission_request"].pop(missing)
    assert grant_from_decision(process, process.pending_decision, "allow_once") is None
    assert process.approval_grants == []


def test_new_explicit_user_decision_still_executes(tmp_path_factory):
    tool, process, calls = fixture(tmp_path_factory)
    create_permission_request(process, {"purpose": "new approval test", "tool_name": tool.name,
                                       "arguments": {}}, {tool.name: tool}, str(tmp_path_factory))
    assert grant_from_decision(process, process.pending_decision, "allow_once")
    ctx = ExecutionContext(process=process, session=process.session, workspace_path=str(tmp_path_factory))
    result = ToolPipeline().execute({"name": tool.name, "args": {}}, tool, ctx, "new-grant")
    assert not result.is_error and len(calls) == 1
    assert process.approval_grants[0]["remaining_uses"] == 0
