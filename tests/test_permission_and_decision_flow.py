from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.loop.agent_tool import AgentTool, ApprovalMode, ToolEffect
from backend.core.loop.decision_support import confirmed_user_state, create_user_decision
from backend.core.loop.event_bus import EventBus
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import RingLevel
from backend.core.loop.models import ProcessStatus
from backend.core.loop.outcome import OutcomeStatus, TaskOutcome
from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
from backend.core.loop.runtime import AgentRuntimeFactory, RuntimeSpec
from backend.core.loop.executor import agent_step
from backend.core.tools.catalog import build_workspace_tools
from backend.core.loop.permission_broker import (
    create_permission_request,
    grant_from_decision,
)
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry
from backend.core.tools import workspace_tools


def _read_tool(root: Path) -> AgentTool:
    def prepare(args: dict) -> dict:
        return {**args, "_workspace": str(root)}

    return AgentTool(
        name="read_file", description="read", parameters={
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        execute=workspace_tools.read_file,
        prepare_args=prepare,
        effect=ToolEffect.READ,
        idempotent=True,
    )


def _process(root: Path, *, actor_kind: str = "supervisor"):
    manager = AgentProcessManager()
    return manager.fork(
        parent_id=None, role="supervisor" if actor_kind == "supervisor" else "worker",
        tool_registry=ToolRegistry(["read_file"]), max_steps=8,
        ring_level=RingLevel.RING_0 if actor_kind == "supervisor" else RingLevel.RING_3,
        workspace_path=str(root), task_id="permission-task", actor_kind=actor_kind,
        task_kind="action",
    )


def _execute(process, tool, workspace: Path, path: str):
    return ToolPipeline().execute(
        {"name": "read_file", "args": {"path": path}}, tool,
        ExecutionContext(
            process=process, session=process.session, workspace_path=str(workspace),
            event_bus=EventBus(), cancellation=process.cancellation_event,
        ),
        "permission-execution", 0,
    )


def test_project_root_absolute_path_is_remapped_into_isolated_worktree(tmp_path_factory: Path):
    root = tmp_path_factory / "project"
    worktree = tmp_path_factory / "worktree"
    root.mkdir(); worktree.mkdir()
    (root / "note.txt").write_text("stale root", encoding="utf-8")
    (worktree / "note.txt").write_text("isolated current", encoding="utf-8")
    process = _process(root, actor_kind="worker")
    process.worktree_path = str(worktree)
    process.worktree = {"isolated": True, "path": str(worktree), "state": "leased"}

    result = _execute(process, _read_tool(root), worktree, str(root / "note.txt"))

    assert not result.is_error
    assert result.data["content"] == "isolated current"
    assert result.data["path"] == "note.txt"


def test_external_resource_requires_structured_permission(tmp_path_factory: Path):
    root = tmp_path_factory / "project"
    external = tmp_path_factory / "external"
    root.mkdir(); external.mkdir()
    target = external / "note.txt"
    target.write_text("external", encoding="utf-8")
    process = _process(root)
    result = _execute(process, _read_tool(root), root, str(target))

    assert result.is_error
    assert result.diagnostics["code"] == "RESOURCE_SCOPE_APPROVAL_REQUIRED"
    info = result.diagnostics["error_info"]
    assert info["catalog_id"] == "GITGO-E3201"
    assert info["next_actions"][0]["action"] == "request_permission"


def test_permission_decision_grants_one_exact_external_read(tmp_path_factory: Path):
    root = tmp_path_factory / "project"
    external = tmp_path_factory / "external"
    root.mkdir(); external.mkdir()
    target = external / "note.txt"
    target.write_text("approved external", encoding="utf-8")
    process = _process(root)
    tool = _read_tool(root)
    request = create_permission_request(process, {
        "purpose": "read the external note needed for comparison",
        "tool_name": "read_file",
        "arguments": {"path": str(target)},
        "resource": str(target),
    }, {"read_file": tool}, str(root))
    assert request["kind"] == "permission"
    grant = grant_from_decision(process, process.pending_decision, "allow_once")
    assert grant and grant["remaining_uses"] == 1
    process.pending_decision = None

    first = _execute(process, tool, root, str(target))
    second = _execute(process, tool, root, str(target))

    assert not first.is_error
    assert first.data["content"] == "approved external"
    assert second.is_error
    assert second.diagnostics["code"] == "RESOURCE_SCOPE_APPROVAL_REQUIRED"


def test_sensitive_tool_requires_a_new_exact_grant_for_every_invocation(tmp_path_factory: Path):
    root = tmp_path_factory / "sensitive"
    root.mkdir()
    process = _process(root, actor_kind="worker")
    process.tool_registry = ToolRegistry(["read_file", "shell_script"])
    calls = []
    tool = AgentTool(
        name="shell_script", description="sensitive shell",
        parameters={
            "type": "object",
            "properties": {
                "script": {"type": "string"}, "purpose": {"type": "string"},
            },
            "required": ["script", "purpose"],
        },
        execute=lambda args: calls.append(args["script"]) or {"success": True},
        read_only=False, effect=ToolEffect.PROCESS, approval=ApprovalMode.ASK,
        approval_per_invocation=True,
    )
    invocation = {"script": "printf approved", "purpose": "verify the build pipeline"}
    ctx = ExecutionContext(
        process=process, session=process.session, workspace_path=str(root),
        event_bus=EventBus(), cancellation=process.cancellation_event,
    )
    blocked = ToolPipeline().execute(
        {"name": "shell_script", "args": invocation}, tool, ctx, "blocked", 0,
    )
    assert blocked.is_error
    assert blocked.diagnostics["code"] == "SENSITIVE_TOOL_APPROVAL_REQUIRED"
    assert blocked.diagnostics["error_info"]["catalog_id"] == "GITGO-E3204"
    request = create_permission_request(process, {
        "purpose": "verify the build pipeline",
        "tool_name": "shell_script", "arguments": invocation,
    }, {"shell_script": tool}, str(root))
    assert [option["action"] for option in request["options"]] == ["allow_once", "deny"]
    assert "printf approved" in request["permission_request"]["arguments_preview"]
    grant = grant_from_decision(process, process.pending_decision, "allow_once")
    assert grant and grant["scope"] == "once"
    process.pending_decision = None

    first = ToolPipeline().execute(
        {"name": "shell_script", "args": invocation}, tool, ctx, "approved", 0,
    )
    second = ToolPipeline().execute(
        {"name": "shell_script", "args": invocation}, tool, ctx, "spent", 0,
    )
    assert not first.is_error
    assert second.is_error
    assert calls == ["printf approved"]


def test_host_suspends_and_resumes_the_exact_sensitive_call_without_model_retry(
    tmp_path_factory: Path,
):
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="governance.observe",
        ring_level=RingLevel.RING_3,
        tool_registry=ToolRegistry([
            "web_search", "request_user_decision", "request_permission",
            "publish_interface_update", "escalate_to_supervisor",
        ]),
        max_steps=4, task_kind="answer", task_id="permission-suspend",
        workspace_path=str(tmp_path_factory),
    ))
    catalog = build_workspace_tools(tmp_path_factory)
    # Exercise the generic sensitive-call suspension path. Public web search
    # is low-risk by default, so this test explicitly upgrades the fixture.
    catalog["web_search"].approval = ApprovalMode.ASK
    calls: list[str] = []
    catalog["web_search"].isolated = False
    catalog["web_search"].execute = lambda args: (
        calls.append(str(args["query"])) or {"answer": "approved result"}
    )
    dispatcher = type("Dispatcher", (), {"_executors": catalog})()

    class Provider:
        protocol = type("Protocol", (), {"value": "openai_responses"})()
        capabilities = type("Capabilities", (), {"prompt_cache": "automatic"})()
        context_window = 8192
        invocations = 0

        def stream_events(self, *_args, **_kwargs):
            self.invocations += 1
            if self.invocations == 1:
                yield ProviderEvent(
                    ProviderEventType.TOOL_CALL_STARTED,
                    tool_call_id="search-call", tool_name="web_search", output_index=0,
                )
                yield ProviderEvent(
                    ProviderEventType.TOOL_CALL_DONE,
                    tool_call_id="search-call", tool_name="web_search", output_index=0,
                    arguments='{"query":"latest weather"}',
                )
            else:
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="done\nTASK_COMPLETE")
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)

    provider = Provider()
    paused = TaskOutcome.from_dict(agent_step(
        process, provider, instruction="find the weather",
        dispatcher=dispatcher, workspace_path=str(tmp_path_factory),
    ))
    assert paused.status == OutcomeStatus.AWAITING_USER, paused.to_dict()
    assert calls == []
    assert provider.invocations == 1
    assert process.pending_tool_batch["tool_calls"][0]["args"] == {
        "query": "latest weather",
    }
    pending = dict(process.pending_decision)
    grant = grant_from_decision(process, pending, "allow_once")
    assert grant is not None
    process.pending_tool_batch["decision_action"] = "allow_once"
    process.pending_decision = None
    process.status = ProcessStatus.RUNNING

    completed = TaskOutcome.from_dict(agent_step(
        process, provider, instruction="", dispatcher=dispatcher,
        workspace_path=str(tmp_path_factory),
    ))
    assert completed.status == OutcomeStatus.COMPLETED
    assert calls == ["latest weather"]
    assert provider.invocations == 2
    tool_outputs = [
        item for item in process.session.messages
        if item.get("message_type") == "tool_result"
        and item.get("tool_call_id") == "search-call"
    ]
    assert len(tool_outputs) == 1
    assert "approved result" in tool_outputs[0]["content"]


def test_sensitive_batch_pauses_at_each_distinct_invocation_without_error_driven_recovery(
    tmp_path_factory: Path,
):
    root = tmp_path_factory / "project"
    root.mkdir()
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="governance.observe",
        ring_level=RingLevel.RING_3,
        tool_registry=ToolRegistry([
            "web_search", "request_user_decision", "request_permission",
            "publish_interface_update", "escalate_to_supervisor",
        ]),
        max_steps=6, task_kind="answer", task_id="permission-multi-suspend",
        workspace_path=str(root),
    ))
    catalog = build_workspace_tools(root)
    catalog["web_search"].approval = ApprovalMode.ASK
    calls: list[str] = []
    catalog["web_search"].isolated = False
    catalog["web_search"].execute = lambda args: (
        calls.append(str(args["query"])) or {"answer": f"result for {args['query']}"}
    )
    dispatcher = type("Dispatcher", (), {"_executors": catalog})()

    class Provider:
        protocol = type("Protocol", (), {"value": "openai_responses"})()
        capabilities = type("Capabilities", (), {"prompt_cache": "automatic"})()
        context_window = 8192
        invocations = 0

        def stream_events(self, *_args, **_kwargs):
            self.invocations += 1
            if self.invocations == 1:
                for index, query in enumerate(("Los Angeles weather", "source verification")):
                    call_id = f"search-{index}"
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL_STARTED,
                        tool_call_id=call_id, tool_name="web_search", output_index=index,
                    )
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL_DONE,
                        tool_call_id=call_id, tool_name="web_search", output_index=index,
                        arguments=f'{{"query":"{query}"}}',
                    )
            else:
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="done\nTASK_COMPLETE")
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)

    provider = Provider()
    first_pause = TaskOutcome.from_dict(agent_step(
        process, provider, instruction="find and verify the weather",
        dispatcher=dispatcher, workspace_path=str(root),
    ))
    assert first_pause.status == OutcomeStatus.AWAITING_USER, first_pause.to_dict()
    assert calls == []

    first_pending = dict(process.pending_decision)
    assert grant_from_decision(process, first_pending, "allow_once") is not None
    process.pending_tool_batch["decision_action"] = "allow_once"
    process.pending_decision = None
    process.status = ProcessStatus.RUNNING
    second_pause = TaskOutcome.from_dict(agent_step(
        process, provider, instruction="", dispatcher=dispatcher,
        workspace_path=str(root),
    ))
    assert second_pause.status == OutcomeStatus.AWAITING_USER
    assert calls == ["Los Angeles weather"]
    assert provider.invocations == 1
    assert process.pending_tool_batch["tool_calls"] == [{
        "id": "search-1", "name": "web_search",
        "args": {"query": "source verification"},
    }]
    assert not any(
        "APPROVAL_GRANT_INVALID" in str(message.get("content") or "")
        for message in process.session.messages
    )

    second_pending = dict(process.pending_decision)
    assert grant_from_decision(process, second_pending, "allow_once") is not None
    process.pending_tool_batch["decision_action"] = "allow_once"
    process.pending_decision = None
    process.status = ProcessStatus.RUNNING
    completed = TaskOutcome.from_dict(agent_step(
        process, provider, instruction="", dispatcher=dispatcher,
        workspace_path=str(root),
    ))
    assert completed.status == OutcomeStatus.COMPLETED
    assert calls == ["Los Angeles weather", "source verification"]
    assert provider.invocations == 2


def test_questions_can_be_incremental_and_latest_confirmed_topic_wins(tmp_path_factory: Path):
    root = tmp_path_factory / "project"
    root.mkdir()
    process = _process(root)
    first = create_user_decision(process, {
        "kind": "preference", "state_topic": "ui.visual_style",
        "question": "Which visual direction?", "why_user_must_decide": "It is subjective.",
        "options": [
            {"label": "Minimal", "principle": "Reduce noise", "immediate_effect": "Use fewer elements", "downstream_effect": "Simpler system", "risks": "May feel sparse", "reversibility": "Can be enriched"},
            {"label": "Dense", "principle": "Expose detail", "immediate_effect": "Show more data", "downstream_effect": "More controls", "risks": "May feel busy", "reversibility": "Can be simplified"},
        ],
    })
    process.session.host_ledger.append({
        "event": "user_decision_received", "decision_id": first["decision_id"],
        "answer": "Choose option 1: Minimal", "selected_label": "Minimal",
        "created_at": "2026-01-01T00:00:00Z",
    })
    process.pending_decision = None
    second = create_user_decision(process, {
        "kind": "verification", "state_topic": "ui.visual_style",
        "supersedes_decision_id": first["decision_id"],
        "question": "Keep the minimal direction?", "why_user_must_decide": "New evidence changed the layout.",
        "options": [
            {"label": "Keep", "principle": "Preserve intent", "immediate_effect": "Keep minimal", "downstream_effect": "Stable style", "risks": "Less detail", "reversibility": "Can revisit"},
            {"label": "Revise", "principle": "Respond to evidence", "immediate_effect": "Adjust style", "downstream_effect": "New direction", "risks": "More changes", "reversibility": "Can revert"},
        ],
    })
    process.session.host_ledger.append({
        "event": "user_decision_received", "decision_id": second["decision_id"],
        "answer": "Choose option 2: Revise", "selected_label": "Revise",
        "created_at": "2026-01-02T00:00:00Z",
    })
    state = confirmed_user_state(process.session.host_ledger)
    assert state == [{
        "topic": "ui.visual_style", "kind": "verification", "value": "Revise",
        "conditions": "Choose option 2: Revise", "decision_id": second["decision_id"],
        "confirmed_at": "2026-01-02T00:00:00Z",
        "supersedes_decision_id": first["decision_id"],
    }]


def test_host_rejects_a_fourth_business_option_and_missing_state_topic(tmp_path_factory: Path):
    root = tmp_path_factory / "decision-contract"
    root.mkdir()
    process = _process(root)
    option = {
        "label": "One", "principle": "P", "immediate_effect": "Now",
        "downstream_effect": "Later", "risks": "Risk", "reversibility": "High",
    }
    with pytest.raises(ValueError, match="2..3 business options"):
        create_user_decision(process, {
            "question": "Choose", "why_user_must_decide": "Subjective",
            "options": [{**option, "label": str(index)} for index in range(4)],
        })
    with pytest.raises(ValueError, match="state_topic is required for preference"):
        create_user_decision(process, {
            "kind": "preference", "question": "Choose",
            "why_user_must_decide": "Subjective",
            "options": [option, {**option, "label": "Two"}],
        })


def test_tool_permission_denied_has_a_canonical_recoverable_payload():
    from backend.core.errors import error_payload

    denied = error_payload(
        "TOOL_PERMISSION_DENIED",
        details={"execution_state": "not_started", "user_decision": "deny"},
        next_actions=[{"action": "change_strategy"}],
    )
    assert denied["error"] == "TOOL_PERMISSION_DENIED"
    assert denied["error_info"]["catalog_id"] == "GITGO-E3205"
    assert denied["error_info"]["retryable"] is False
