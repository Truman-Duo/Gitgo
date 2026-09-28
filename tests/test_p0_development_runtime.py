from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from backend.core.contract import build_function_graph
from backend.core.dependency_graph import (
    build_dependency_graph,
    dependency_graph_affected_by,
    load_dependency_graph,
    query_dependents,
    record_dependency_feedback,
    record_tool_observation,
)
from backend.core.loop.agent_tool import AgentTool, ApprovalMode, ToolEffect
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.completion_protocol import HostCompletionEvaluator
from backend.core.loop.event_bus import EventBus
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import RingLevel
from backend.core.loop.models import ProcessStatus
from backend.core.loop.process_tool_runner import ProcessToolRunner
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tool_execution import ToolExecution
from backend.core.loop.tools import ToolRegistry
from backend.core.tools.catalog import build_workspace_tools
from backend.core.tools.dynamic_tools import validate_definition
from backend.core.tools.workspace_tools import (
    delete_file, edit_file, exec_command, list_files, read_file, write_file,
)


def test_multisignal_graph_handles_typescript_and_path_references(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    (tmp_path / "src").mkdir()
    (tmp_path / "templates").mkdir()
    (tmp_path / "src" / "api.ts").write_text(
        "import { authenticate } from './auth';\n"
        "const template = '../templates/login.html';\n"
        "authenticate();\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "auth.ts").write_text(
        "export function authenticate() { return true; }\n", encoding="utf-8",
    )
    (tmp_path / "templates" / "login.html").write_text("login", encoding="utf-8")

    graph = build_dependency_graph(tmp_path)
    auth_edges = graph.get_dependents("src/auth.ts")
    template_edges = graph.get_dependents("templates/login.html")

    assert any(item["dependent"] == "src/api.ts" for item in auth_edges)
    assert any(item["dependent"] == "src/api.ts" for item in template_edges)
    assert any(
        evidence["signal"] == "language_reference"
        for item in auth_edges for evidence in item["evidence"]
    )


def test_dependency_refresh_skips_only_files_outside_graph_model():
    assert not dependency_graph_affected_by([
        "notes/readme.txt", "assets/image.png", "tests/fixtures/marker.txt",
    ])
    assert dependency_graph_affected_by(["backend/service.py"])
    assert dependency_graph_affected_by(["config/runtime.yaml"])
    assert dependency_graph_affected_by([".gitgo/dependency_observations.jsonl"])


def test_dependency_dismissal_expires_when_code_changes(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    (tmp_path / "a.py").write_text("from b import run\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def run(): pass\n", encoding="utf-8")
    build_dependency_graph(tmp_path)
    record_dependency_feedback(
        tmp_path, dependent="a.py", dependency="b.py", confirmed=False,
        reason="reviewed false positive",
    )
    assert query_dependents(tmp_path, "b.py") == []

    (tmp_path / "a.py").write_text("from b import run\nrun()\n", encoding="utf-8")
    assert any(item["dependent"] == "a.py" for item in query_dependents(tmp_path, "b.py"))


def test_tool_coaccess_becomes_dependency_evidence(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    record_tool_observation(
        tmp_path, task_id="task-1", tool_name="read_file", files=["a.py", "b.py"],
    )
    graph = build_dependency_graph(tmp_path)
    assert any(
        edge.dependent == "a.py" and edge.dependency == "b.py"
        and any(item.signal == "tool_coaccess" for item in edge.evidence)
        for edge in graph.edges.values()
    )


def test_function_graph_is_not_filesystem_order_dependent(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    (tmp_path / "a_caller.py").write_text("target()\n", encoding="utf-8")
    (tmp_path / "z_callee.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    graph = build_function_graph(tmp_path)
    assert "a_caller.py:target" in graph["z_callee.py"]["called_by"]["target"]


def test_workspace_file_tools_use_hash_cas_and_confinement(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    target = tmp_path / "sample.txt"
    target.write_text("before\n", encoding="utf-8")
    observed = read_file({"_workspace": str(tmp_path), "path": "sample.txt"})
    result = edit_file({
        "_workspace": str(tmp_path), "path": "sample.txt",
        "old_string": "before", "new_string": "after",
        "expected_sha256": observed["sha256"],
    })
    assert "error" not in result
    stale = edit_file({
        "_workspace": str(tmp_path), "path": "sample.txt",
        "old_string": "after", "new_string": "again",
        "expected_sha256": observed["sha256"],
    })
    assert stale["error"] == "FILE_CHANGED"
    with pytest.raises(PermissionError):
        read_file({"_workspace": str(tmp_path), "path": "../outside.txt"})


def test_write_file_is_create_only_by_default(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    first = write_file({"_workspace": str(tmp_path), "path": "new.txt", "content": "one"})
    assert first["action"] == "created"
    second = write_file({"_workspace": str(tmp_path), "path": "new.txt", "content": "two"})
    assert second["error"] == "FILE_EXISTS"
    assert second["error_info"]["catalog_id"] == "GITGO-E3502"
    assert second["error_info"]["next_actions"][0]["arguments"] == {
        "path": "new.txt",
        "create_only": False,
        "expected_sha256": hashlib.sha256(b"one").hexdigest(),
    }
    expected = hashlib.sha256(b"one").hexdigest()
    updated = write_file({
        "_workspace": str(tmp_path), "path": "new.txt", "content": "two",
        "create_only": False, "expected_sha256": expected,
    })
    assert updated["action"] == "updated"


def test_delete_file_is_version_pinned_recoverable_and_protects_gitgo(tmp_path_factory: Path):
    root = tmp_path_factory / "delete-file"
    root.mkdir()
    target = root / "artifact.txt"
    target.write_text("recover me\n", encoding="utf-8")
    current = read_file({"_workspace": str(root), "path": "artifact.txt"})

    missing_hash = delete_file({"_workspace": str(root), "path": "artifact.txt"})
    assert missing_hash["error"] == "EXPECTED_HASH_REQUIRED"
    assert target.exists()

    stale = delete_file({
        "_workspace": str(root), "path": "artifact.txt",
        "expected_sha256": "0" * 64,
    })
    assert stale["error"] == "FILE_CHANGED"
    assert target.exists()

    deleted = delete_file({
        "_workspace": str(root), "path": "artifact.txt",
        "expected_sha256": current["sha256"],
    })
    assert deleted["action"] == "deleted"
    assert deleted["recoverable"] is True
    assert not target.exists()
    assert (root / deleted["trash_ref"]).read_text(encoding="utf-8") == "recover me\n"
    assert "--- a/artifact.txt" in deleted["diff"]

    protected = root / ".gitgo" / "state.json"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("{}", encoding="utf-8")
    blocked = delete_file({
        "_workspace": str(root), "path": ".gitgo/state.json",
        "expected_sha256": hashlib.sha256(b"{}").hexdigest(),
    })
    assert blocked["error"] == "PROTECTED_PATH"
    assert protected.exists()


def test_list_files_treats_a_missing_target_directory_as_an_empty_fact(
    tmp_path_factory: Path,
):
    result = list_files({
        "_workspace": str(tmp_path_factory), "path": "future/output",
    })
    assert result == {
        "files": [], "count": 0, "truncated": False, "exists": False,
        "path": "future/output",
    }


def test_exec_command_resolves_managed_python_without_global_path(
    tmp_path_factory: Path,
):
    result = exec_command({
        "_workspace": str(tmp_path_factory),
        "argv": ["python", "-c", "print('managed-python-ok')"],
    })
    assert result["success"] is True
    assert result["stdout"].strip() == "managed-python-ok"
    assert Path(result["argv"][0]).resolve() == Path(sys.executable).resolve()


def test_exec_command_managed_python_imports_workspace_siblings(
    tmp_path_factory: Path,
):
    (tmp_path_factory / "answer.py").write_text("VALUE = 42\n", encoding="utf-8")
    (tmp_path_factory / "check.py").write_text(
        "from answer import VALUE\nassert VALUE == 42\nprint('sibling-ok')\n",
        encoding="utf-8",
    )
    direct = exec_command({
        "_workspace": str(tmp_path_factory),
        "argv": ["python", "check.py"],
    })
    module = exec_command({
        "_workspace": str(tmp_path_factory),
        "argv": ["py", "-3", "-m", "check"],
    })
    assert direct["success"] is True, direct
    assert direct["stdout"].strip() == "sibling-ok"
    assert module["success"] is True, module
    assert module["stdout"].strip() == "sibling-ok"


def test_exec_command_pwd_is_a_cross_platform_host_shortcut(tmp_path_factory: Path):
    nested = tmp_path_factory / "nested"
    nested.mkdir()
    result = exec_command({
        "_workspace": str(tmp_path_factory), "cwd": "nested", "argv": ["pwd"],
    })
    assert result["success"] is True
    assert result["stdout"] == "nested\n"
    assert result["host_shortcut"] == "workspace_relative_cwd"


def test_dynamic_tool_cannot_compose_unapproved_or_noncomposable_tool(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    workspace_tools = build_workspace_tools(tmp_path)
    spec = validate_definition({
        "name": "find_todos",
        "description": "Find TODO markers",
        "parameters": {"type": "object", "properties": {}},
        "steps": [{
            "id": "search_todos", "tool": "search_text",
            "arguments": {"pattern": "TODO", "literal": True},
        }],
    }, workspace_tools)
    assert spec["effect"] == "read"

    unsafe = AgentTool(
        name="unsafe", description="unsafe", parameters={"type": "object"},
        execute=lambda args: {}, read_only=False, effect=ToolEffect.EXTERNAL,
        isolated=True, runner_name="unsafe",
    )
    with pytest.raises(PermissionError):
        validate_definition({
            "name": "escape_tool", "description": "bad",
            "parameters": {"type": "object"},
            "steps": [{"tool": "unsafe", "arguments": {}}],
        }, {"unsafe": unsafe})


def test_dynamic_tool_executes_through_isolated_registry(tmp_path_factory: Path):
    (tmp_path_factory / "note.txt").write_text("composite works", encoding="utf-8")
    spec = validate_definition({
        "name": "read_named_file",
        "description": "Read the named file",
        "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "steps": [{
            "id": "read_note", "tool": "read_file",
            "arguments": {"path": "${input.path}"},
        }],
    }, build_workspace_tools(tmp_path_factory))
    result = ProcessToolRunner(timeout=10).run("dynamic_composite", {
        "_workspace": str(tmp_path_factory),
        "_dynamic_spec": spec,
        "path": "note.txt",
    })
    assert result.success
    assert result.data["steps"]["read_note"]["content"] == "composite works"


def test_dynamic_tool_host_plan_reuses_canonical_pipeline_once(tmp_path_factory: Path):
    (tmp_path_factory / "note.txt").write_text("TODO host pipeline", encoding="utf-8")
    base_tools = build_workspace_tools(tmp_path_factory)
    spec = validate_definition({
        "name": "inspect_note",
        "description": "Read and search one note",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "steps": [
            {"id": "read_note", "tool": "read_file", "arguments": {
                "path": "${input.path}",
            }},
            {"id": "find_todo", "tool": "search_text", "arguments": {
                "path": "${input.path}", "pattern": "TODO", "literal": True,
            }},
        ],
    }, base_tools)
    dynamic = AgentTool(
        name=spec["name"], description=spec["description"],
        parameters=spec["parameters"], execute=lambda _args: {},
        effect=spec["effect"], resources=spec["resources"],
        composite_spec=spec,
    )
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker",
        tool_registry=ToolRegistry([*base_tools, dynamic.name]),
        max_steps=3, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="dynamic-host",
    )
    authorized = []
    events = []
    bus = EventBus()
    bus.subscribe("CompositeStepCompleted", lambda event: events.append(event.data))
    ctx = ExecutionContext(
        process=process, session=process.session,
        workspace_path=str(tmp_path_factory), event_bus=bus,
        cancellation=process.cancellation_event,
        tool_authorizer=lambda name, args: authorized.append(name) or {"allowed": True},
        artifacts={"tool_catalog": {**base_tools, dynamic.name: dynamic}},
    )
    result = ToolPipeline().execute(
        {"name": dynamic.name, "args": {"path": "note.txt"}},
        dynamic, ctx, "execution", 0,
    )
    assert not result.is_error
    assert authorized == ["read_file", "search_text"]
    assert [event["step_id"] for event in events] == ["read_note", "find_todo"]
    assert len(result.receipt["child_receipt_ids"]) == 2
    assert result.data["step_receipt_ids"] == result.receipt["child_receipt_ids"]
    assert "child_receipts" not in result.data
    assert all(
        receipt["composite_tool"] == "inspect_note"
        for receipt in result.receipt["child_receipts"]
    )
    assert result.receipt["dynamic_tool_name"] == "inspect_note"
    assert result.receipt["definition_digest"] == spec["digest"]


def test_dynamic_tool_rebinds_private_workspace_to_child_execution(tmp_path_factory: Path):
    root = tmp_path_factory / "root"
    child = tmp_path_factory / "child"
    root.mkdir()
    child.mkdir()
    base_tools = build_workspace_tools(root)
    spec = validate_definition({
        "name": "create_and_read_child",
        "description": "Create a child-scoped file and read it back",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}, "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        "steps": [
            {"id": "create", "tool": "write_file", "arguments": {
                "path": "${input.path}", "content": "${input.content}",
                "create_only": True,
            }},
            {"id": "read", "tool": "read_file", "arguments": {
                "path": "${input.path}",
            }},
        ],
    }, base_tools)
    dynamic = AgentTool(
        name=spec["name"], description=spec["description"],
        parameters=spec["parameters"], execute=lambda _args: {},
        effect=spec["effect"], resources=spec["resources"],
        composite_spec=spec,
    )
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker",
        tool_registry=ToolRegistry([*base_tools, dynamic.name]),
        max_steps=3, ring_level=RingLevel.RING_3,
        workspace_path=str(child), task_id="child-workspace",
    )
    ctx = ExecutionContext(
        process=process, session=process.session, workspace_path=str(child),
        cancellation=process.cancellation_event,
        artifacts={"tool_catalog": {**base_tools, dynamic.name: dynamic}},
    )
    result = ToolPipeline().execute(
        {"name": dynamic.name, "args": {"path": "only-child.txt", "content": "ok"}},
        dynamic, ctx, "child-execution", 0,
    )
    assert not result.is_error, result.error
    assert result.data["steps"]["read"]["content"] == "ok"
    assert (child / "only-child.txt").read_text(encoding="utf-8") == "ok"
    assert not (root / "only-child.txt").exists()


def test_dynamic_post_write_verification_failure_rolls_back(tmp_path_factory: Path):
    target = tmp_path_factory / "note.txt"
    target.write_text("before", encoding="utf-8")
    base_tools = build_workspace_tools(tmp_path_factory)
    spec = validate_definition({
        "name": "write_then_verify",
        "description": "Write and then verify through a read",
        "parameters": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
        "steps": [
            {"id": "write", "tool": "write_file", "arguments": {
                "path": "note.txt", "content": "${input.content}",
                "create_only": False,
                "expected_sha256": hashlib.sha256(b"before").hexdigest(),
            }},
            {"id": "verify", "tool": "read_file", "arguments": {
                "path": "missing-verification.txt",
            }},
        ],
    }, base_tools)
    dynamic = AgentTool(
        name=spec["name"], description=spec["description"],
        parameters=spec["parameters"], execute=lambda _args: {},
        effect=spec["effect"], resources=spec["resources"],
        composite_spec=spec,
    )
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker",
        tool_registry=ToolRegistry([*base_tools, dynamic.name]),
        max_steps=2, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="rollback-child",
    )
    ctx = ExecutionContext(
        process=process, session=process.session,
        workspace_path=str(tmp_path_factory), cancellation=process.cancellation_event,
        artifacts={"tool_catalog": {**base_tools, dynamic.name: dynamic}},
    )
    execution = ToolExecution(
        execution_id="post-write-failure", ctx=ctx,
        tool_calls=[{"name": dynamic.name, "args": {"content": "after"}}],
    )
    execution.begin()
    results = execution.execute_batch({**base_tools, dynamic.name: dynamic})
    assert results[0].is_error
    assert results[0].diagnostics["rollback_required"] is True
    assert execution.status == "failed"
    assert target.read_text(encoding="utf-8") == "before"


def test_dynamic_tool_rejects_partial_commit_shape(tmp_path_factory: Path):
    tools = build_workspace_tools(tmp_path_factory)
    with pytest.raises(ValueError, match="at most one effectful step"):
        validate_definition({
            "name": "two_writes", "description": "unsafe partial commit",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
            "steps": [
                {"id": "first_write", "tool": "write_file", "arguments": {
                    "path": "one.txt", "content": "${input.content}",
                }},
                {"id": "second_write", "tool": "write_file", "arguments": {
                    "path": "two.txt", "content": "${input.content}",
                }},
            ],
        }, tools)


def test_dynamic_write_snapshot_uses_effect_not_tool_name(tmp_path_factory: Path):
    target = tmp_path_factory / "note.txt"
    target.write_text("before", encoding="utf-8")
    base_tools = build_workspace_tools(tmp_path_factory)
    spec = validate_definition({
        "name": "replace_note", "description": "Replace a note safely",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}, "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        "steps": [{
            "id": "write_note", "tool": "write_file",
            "arguments": {
                "path": "${input.path}", "content": "${input.content}",
                "create_only": False,
                "expected_sha256": hashlib.sha256(b"before").hexdigest(),
            },
        }],
    }, base_tools)
    dynamic = AgentTool(
        name=spec["name"], description=spec["description"],
        parameters=spec["parameters"], execute=lambda _args: {},
        read_only=False, effect=ToolEffect.WORKSPACE_WRITE,
        resources=spec["resources"], composite_spec=spec,
    )
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker",
        tool_registry=ToolRegistry([*base_tools, dynamic.name]),
        max_steps=2, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="snapshot",
    )
    ctx = ExecutionContext(
        process=process, session=process.session,
        workspace_path=str(tmp_path_factory), event_bus=EventBus(),
        cancellation=process.cancellation_event,
        artifacts={"tool_catalog": {**base_tools, dynamic.name: dynamic}},
    )
    execution = ToolExecution(
        execution_id="snapshot-execution", ctx=ctx,
        tool_calls=[{"name": dynamic.name, "args": {
            "path": "note.txt", "content": "after",
        }}],
    )
    execution.begin()
    target.write_text("corrupted", encoding="utf-8")
    execution.rollback("test")
    assert target.read_text(encoding="utf-8") == "before"


def test_define_tool_lifecycle_is_host_compiled_and_task_scoped(tmp_path_factory: Path):
    from backend.core.loop import executor as executor_module

    base_tools = build_workspace_tools(tmp_path_factory)
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("development.workspace")),
        max_steps=3, ring_level=RingLevel.RING_3,
        actor_kind="worker", capability_profile_id="development.workspace",
        workspace_path=str(tmp_path_factory), task_id="define-lifecycle",
    )
    tools = executor_module._build_internal_tools(
        process, base_tools, str(tmp_path_factory), object(), None,
    )
    definition = {
        "name": "read_note", "description": "Read one selected note",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "steps": [{
            "id": "read_file_step", "tool": "read_file",
            "arguments": {"path": "${input.path}"},
        }],
    }
    created = tools["define_tool"].execute(definition)
    assert created["defined"] is True
    assert created["version"] == 1
    assert len(created["digest"]) == 64
    assert process.dynamic_tools["read_note"].composite_spec["execution_mode"] == "host_pipeline"

    replaced = tools["define_tool"].execute({
        **definition, "operation": "replace", "description": "Read a selected text note",
    })
    assert replaced["defined"] is True
    assert replaced["version"] == 2
    assert replaced["digest"] != created["digest"]
    listed = tools["define_tool"].execute({"operation": "list"})
    assert listed["tools"][0]["version"] == 2
    revoked = tools["define_tool"].execute({"operation": "revoke", "name": "read_note"})
    assert revoked["revoked"] is True
    assert "read_note" not in process.dynamic_tools
    assert not process.tool_registry.has("read_note")


def test_author_tool_runs_registration_tests_and_stays_pure(tmp_path_factory: Path):
    from backend.core.loop import executor as executor_module

    source = tmp_path_factory / "word_count.py"
    source.write_text(
        "def run(args):\n    return {'count': len(args['words'])}\n",
        encoding="utf-8",
    )
    base_tools = build_workspace_tools(tmp_path_factory)
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("development.workspace")),
        max_steps=3, ring_level=RingLevel.RING_3,
        actor_kind="worker", capability_profile_id="development.workspace",
        workspace_path=str(tmp_path_factory), task_id="author-lifecycle",
    )
    tools = executor_module._build_internal_tools(
        process, base_tools, str(tmp_path_factory), object(), None,
    )
    created = tools["author_tool"].execute({
        "name": "count_words", "description": "Count supplied words",
        "source_path": "word_count.py",
        "parameters": {"type": "object", "properties": {
            "words": {"type": "array", "items": {"type": "string"}},
        }, "required": ["words"]},
        "tests": [{"input": {"words": ["a", "b"]}, "expected": {"count": 2}}],
    })
    assert created["registered"] is True
    dynamic = process.dynamic_tools["count_words"]
    assert dynamic.composite_spec["execution_mode"] == "authored_pure_python"
    result = ProcessToolRunner(timeout=10).run("authored_python", {
        "_workspace": str(tmp_path_factory), "_dynamic_spec": dynamic.composite_spec,
        "words": ["a", "b", "c"],
    })
    assert result.success and result.data == {"count": 3}

    (tmp_path_factory / "unsafe.py").write_text(
        "import os\ndef run(args):\n    return {'cwd': os.getcwd()}\n", encoding="utf-8",
    )
    denied = tools["author_tool"].execute({
        "name": "escape_host", "description": "unsafe",
        "source_path": "unsafe.py", "parameters": {"type": "object"},
        "tests": [{"input": {}, "expected": {}}],
    })
    assert denied["registered"] is False
    assert denied["error"] == "CUSTOM_TOOL_INVALID"
    assert "function definitions" in denied["message"] or "forbidden" in denied["message"]
    assert denied["error_info"]["catalog_id"] == "GITGO-E3301"


def test_privileged_authored_tool_binds_source_and_requires_exact_user_approval(
    tmp_path_factory: Path,
):
    from backend.core.loop import executor as executor_module
    from backend.core.loop.permission_broker import (
        create_permission_request, grant_from_decision,
    )
    from backend.core.storage import get_storage

    source = tmp_path_factory / "sqrt_tool.py"
    source.write_text(
        "import math\ndef run(args):\n    return {'root': math.sqrt(args['value'])}\n",
        encoding="utf-8",
    )
    payload = {
        "operation": "register", "authority_mode": "privileged",
        "name": "approved_sqrt", "description": "Calculate a square root",
        "purpose": "run an imported numerical helper",
        "source_path": "sqrt_tool.py",
        "source_sha256": hashlib.sha256(
            source.read_text(encoding="utf-8-sig").encode("utf-8")
        ).hexdigest(),
        "effect": "process", "resources": ["process:python"],
        "parameters": {"type": "object", "properties": {
            "value": {"type": "number"},
        }, "required": ["value"]},
        "tests": [{"input": {"value": 9}, "expected": {"root": 3.0}}],
    }
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("development.workspace")),
        max_steps=3, ring_level=RingLevel.RING_3, actor_kind="worker",
        capability_profile_id="development.workspace",
        workspace_path=str(tmp_path_factory), task_id="author-privileged",
    )
    tools = executor_module._build_internal_tools(
        process, build_workspace_tools(tmp_path_factory),
        str(tmp_path_factory), object(), None,
    )
    blocked = tools["author_tool"].execute(payload)
    assert blocked["error"] == "CUSTOM_TOOL_PRIVILEGED_APPROVAL_REQUIRED", blocked
    assert blocked["error_info"]["catalog_id"] == "GITGO-E3309"

    pending = create_permission_request(process, {
        "tool_name": "author_tool", "arguments": payload,
        "purpose": payload["purpose"], "resource": "capability://author_tool",
    }, tools, str(tmp_path_factory))
    assert grant_from_decision(process, pending, "allow_once")
    process.pending_decision = None
    created = tools["author_tool"].execute(payload)
    assert created["registered"] is True
    dynamic = process.dynamic_tools["approved_sqrt"]
    assert dynamic.approval == ApprovalMode.ASK
    assert dynamic.approval_per_invocation is True
    assert dynamic.composite_spec["source_sha256"] == payload["source_sha256"]

    ctx = ExecutionContext(
        process=process, session=process.session,
        workspace_path=str(tmp_path_factory), event_bus=EventBus(),
        cancellation=process.cancellation_event,
        storage=get_storage(tmp_path_factory),
    )
    denied = ToolPipeline().execute(
        {"name": "approved_sqrt", "args": {"value": 16}},
        dynamic, ctx, "privileged", 0,
    )
    assert denied.is_error
    assert denied.diagnostics["code"] == "SENSITIVE_TOOL_APPROVAL_REQUIRED"
    pending = create_permission_request(process, {
        "tool_name": "approved_sqrt", "arguments": {"value": 16},
        "purpose": "calculate this requested square root",
        "resource": "process:python",
    }, {**tools, "approved_sqrt": dynamic}, str(tmp_path_factory))
    assert grant_from_decision(process, pending, "allow_once")
    process.pending_decision = None
    approved = ToolPipeline().execute(
        {"name": "approved_sqrt", "args": {"value": 16}},
        dynamic, ctx, "privileged", 1,
    )
    assert not approved.is_error
    assert approved.data == {"root": 4.0}


def test_permission_tool_resolves_authored_tools_mounted_after_construction(
    tmp_path_factory: Path,
):
    """The model-facing broker must see the live task-scoped tool surface."""
    from backend.core.loop import executor as executor_module
    from backend.core.loop.permission_broker import grant_from_decision

    source = tmp_path_factory / "echo_tool.py"
    source.write_text(
        "def run(args):\n    return {'value': args['value']}\n",
        encoding="utf-8",
    )
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("development.workspace")),
        max_steps=3, ring_level=RingLevel.RING_3, actor_kind="worker",
        capability_profile_id="development.workspace",
        workspace_path=str(tmp_path_factory), task_id="late-mounted-permission",
    )
    tools = executor_module._build_internal_tools(
        process, build_workspace_tools(tmp_path_factory),
        str(tmp_path_factory), object(), None,
    )
    payload = {
        "operation": "register", "authority_mode": "privileged",
        "name": "privileged_echo", "description": "Echo through authored code",
        "purpose": "register the requested local helper",
        "source_path": "echo_tool.py",
        "source_sha256": hashlib.sha256(
            source.read_text(encoding="utf-8-sig").encode("utf-8")
        ).hexdigest(),
        "effect": "process", "resources": ["process:python"],
        "parameters": {"type": "object", "properties": {
            "value": {"type": "string"},
        }, "required": ["value"]},
        "tests": [{"input": {"value": "ok"}, "expected": {"value": "ok"}}],
    }
    registration = tools["request_permission"].execute({
        "tool_name": "author_tool", "arguments": payload,
        "purpose": payload["purpose"], "resource": "capability://author_tool",
    })
    assert grant_from_decision(process, registration, "allow_once")
    process.pending_decision = None
    assert tools["author_tool"].execute(payload)["registered"] is True

    invocation = {"value": "live"}
    pending = tools["request_permission"].execute({
        "tool_name": "privileged_echo", "arguments": invocation,
        "purpose": "run the newly mounted helper", "resource": "process:python",
    })
    request = pending["permission_request"]
    assert request["tool_name"] == "privileged_echo"
    assert request["approval_scope"] == "exact_invocation"


def test_explicit_user_grant_overrides_ordinary_governance_for_its_scope(
    tmp_path_factory: Path,
):
    from backend.core.loop.permission_broker import (
        create_permission_request, grant_from_decision,
    )
    from backend.core.storage import get_storage

    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker", tool_registry=ToolRegistry(["sensitive"]),
        max_steps=3, ring_level=RingLevel.RING_3, actor_kind="worker",
        workspace_path=str(tmp_path_factory), task_id="user-authority",
    )
    tool = AgentTool(
        name="sensitive", description="approved action",
        parameters={"type": "object"},
        execute=lambda args: {"done": args["value"]},
        approval=ApprovalMode.ASK, effect=ToolEffect.PROCESS,
        resources=["process:approved"],
    )
    pending = create_permission_request(process, {
        "tool_name": "sensitive", "arguments": {"value": 7},
        "purpose": "perform the user-approved action",
        "resource": "process:approved",
    }, {"sensitive": tool}, str(tmp_path_factory))
    assert grant_from_decision(process, pending, "allow_task")
    process.pending_decision = None
    ctx = ExecutionContext(
        process=process, session=process.session,
        workspace_path=str(tmp_path_factory), event_bus=EventBus(),
        cancellation=process.cancellation_event,
        storage=get_storage(tmp_path_factory),
    )
    ctx.tool_authorizer = lambda _name, _args: {
        "allowed": False, "reason": "ordinary policy",
    }
    result = ToolPipeline().execute(
        {"name": "sensitive", "args": {"value": 7}},
        tool, ctx, "authority", 0,
    )
    assert not result.is_error
    assert result.data == {"done": 7}


def test_document_open_reads_text_through_unified_adapter(tmp_path_factory: Path):
    from backend.core.tools.document_tools import document_open

    (tmp_path_factory / "guide.md").write_text("first\nsecond\nthird", encoding="utf-8")
    result = document_open({
        "_workspace": str(tmp_path_factory), "path": "guide.md", "max_chars": 1000,
    })
    assert result["format"] == "md"
    assert result["content"] == "first\nsecond\nthird"
    assert result["truncated"] is False


def test_process_runner_does_not_depend_on_caller_working_directory(
    tmp_path_factory: Path, monkeypatch,
):
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("cwd independent ✅", encoding="utf-8")
    monkeypatch.chdir(tmp_path_factory)
    result = ProcessToolRunner(timeout=10).run("read_file", {
        "_workspace": str(workspace),
        "path": "note.txt",
    })
    assert result.success
    assert result.data["content"] == "cwd independent ✅"


def test_development_profile_contains_cancellable_general_tools():
    names = set(CapabilityProfiles.resolve_tools("development.workspace"))
    assert {
        "read_file", "list_files", "search_text", "edit_file", "write_file",
        "delete_file", "apply_patch", "exec_command", "shell_script", "define_tool", "author_tool",
        "document_open", "dependency_feedback",
    } <= names


def test_shell_is_sensitive_and_available_to_worker_or_explicit_a_lease(tmp_path_factory):
    catalog = build_workspace_tools(tmp_path_factory)
    shell = catalog["shell_script"]
    assert shell.approval.value == "ask"
    assert shell.approval_per_invocation is True
    assert shell.effect == ToolEffect.PROCESS
    assert "shell_script" in CapabilityProfiles.resolve_tools("development.workspace")
    lease = CapabilityProfiles.issue_self_execute_lease(
        actor_kind="supervisor", task_id="shell-task", requested_by="a",
        profile_id="development.workspace", reason="run an approved build pipeline",
        intended_actions=["execute the exact approved Bash script"],
    )
    leased = set(CapabilityProfiles.resolve_tools("supervisor.control", lease=lease))
    assert "shell_script" in leased
    assert "publish_interface_update" not in leased
    assert "escalate_to_supervisor" not in leased


def test_approved_shell_handler_uses_bash_without_shell_true(tmp_path_factory):
    from backend.core.tools import workspace_tools
    if workspace_tools._find_bash() is None:
        pytest.skip("Bash is not installed on this host")
    result = ProcessToolRunner(timeout=15).run("shell_script", {
        "_workspace": str(tmp_path_factory),
        "script": "printf 'gitgo-shell-ok'",
        "purpose": "exercise the isolated Bash handler",
        "timeout": 10,
    })
    assert result.success is True
    assert result.data["success"] is True
    assert result.data["stdout"] == "gitgo-shell-ok"


def test_supervisor_and_worker_share_question_and_tool_authoring_shortcuts():
    supervisor = set(CapabilityProfiles.resolve_tools("supervisor.control"))
    worker = set(CapabilityProfiles.resolve_tools("development.workspace"))
    expected = {"request_user_decision", "request_permission", "define_tool", "author_tool"}
    assert expected <= supervisor
    assert expected <= worker


def test_tool_catalog_mutations_are_journaled_as_process_effects(tmp_path_factory):
    from backend.core.loop import executor as executor_module
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="supervisor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("supervisor.control")),
        max_steps=3, ring_level=RingLevel.RING_0, actor_kind="supervisor",
        capability_profile_id="supervisor.control", workspace_path=str(tmp_path_factory),
        task_id="catalog-effect",
    )
    tools = executor_module._build_internal_tools(
        process, build_workspace_tools(tmp_path_factory), str(tmp_path_factory), object(), None,
    )
    assert tools["define_tool"].effect == ToolEffect.PROCESS
    assert tools["author_tool"].effect == ToolEffect.PROCESS
    assert tools["author_tool"].read_only is False


def test_process_runner_cancels_command_process_tree():
    marker = Path(tempfile.gettempdir()) / f"gitgo-cancel-{time.time_ns()}.txt"
    event = threading.Event()
    runner = ProcessToolRunner(timeout=20)

    def cancel_soon():
        time.sleep(0.25)
        event.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    result = runner.run(
        "exec_command",
        {
            "_workspace": str(Path.cwd()),
            "argv": [
                sys.executable, "-c",
                (
                    "import pathlib,time; time.sleep(2); "
                    f"pathlib.Path({str(marker)!r}).write_text('survived')"
                ),
            ],
            "timeout": 15,
        },
        cancellation_event=event,
    )
    assert result.success is False
    assert "cancelled" in result.error
    time.sleep(2.2)
    try:
        assert not marker.exists(), "cancelled descendant continued running"
    finally:
        marker.unlink(missing_ok=True)


def test_pipeline_executes_before_inspecting_result(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker", tool_registry=ToolRegistry(["read"]),
        max_steps=2, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path), task_id="task",
    )
    tool = AgentTool(
        name="read", description="read", parameters={"type": "object", "properties": {}},
        execute=lambda args: {"ok": True}, read_only=True,
    )
    ctx = ExecutionContext(
        process=process, session=process.session, workspace_path=str(tmp_path),
        event_bus=EventBus(), cancellation=process.cancellation_event,
    )
    result = ToolPipeline().execute(
        {"name": "read", "args": {}}, tool, ctx, "execution", 0,
    )
    assert not result.is_error
    assert result.data == {"ok": True}
    assert not (tmp_path / ".gitgo" / "tool_invocations").exists()


def test_pipeline_treats_nonzero_command_as_failed_action(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker", tool_registry=ToolRegistry(["command"]),
        max_steps=2, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path), task_id="task",
    )
    tool = AgentTool(
        name="command", description="command",
        parameters={"type": "object", "properties": {}},
        execute=lambda args: {"success": False, "exit_code": 7},
        read_only=False, effect=ToolEffect.PROCESS,
    )
    ctx = ExecutionContext(
        process=process, session=process.session, workspace_path=str(tmp_path),
        event_bus=EventBus(), cancellation=process.cancellation_event,
    )
    result = ToolPipeline().execute(
        {"name": "command", "args": {}}, tool, ctx, "execution", 0,
    )
    assert result.is_error
    assert result.diagnostics["code"] == "COMMAND_EXIT_NONZERO"
    assert result.diagnostics["catalog_id"] == "GITGO-E3501"
    assert "exit_code" in result.formatted
    assert "inspect_captured_output" in result.formatted
    assert result.receipt["effect_state"] == "ambiguous"
    invocation = json.loads(Path(result.receipt["invocation_path"]).read_text(encoding="utf-8"))
    assert invocation["process_id"] == process.process_id
    assert invocation["task_id"] == "task"
    assert invocation["state"] == "business_error"
    assert invocation["started_at"] <= invocation["updated_at"]


def test_pipeline_preserves_command_failure_output_for_model(tmp_path_factory: Path):
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker", tool_registry=ToolRegistry(["command"]),
        max_steps=2, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="task-diagnostic",
    )
    tool = AgentTool(
        name="command", description="command",
        parameters={"type": "object", "properties": {}},
        execute=lambda args: {
            "success": False,
            "exit_code": 2,
            "stdout": "collected output\n",
            "stderr": "AssertionError: expected 3, got 2\n",
            "truncated": False,
        },
        read_only=False, effect=ToolEffect.PROCESS,
    )
    ctx = ExecutionContext(
        process=process, session=process.session,
        workspace_path=str(tmp_path_factory), event_bus=EventBus(),
        cancellation=process.cancellation_event,
    )

    result = ToolPipeline().execute(
        {"name": "command", "args": {}}, tool, ctx, "execution", 0,
    )

    assert result.is_error
    assert "collected output" in result.formatted
    assert "AssertionError: expected 3, got 2" in result.formatted
    assert result.data["exit_code"] == 2


def test_effectful_tool_fails_closed_when_invocation_journal_is_unavailable(
    tmp_path_factory: Path, monkeypatch,
):
    called = []
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker", tool_registry=ToolRegistry(["write"]),
        max_steps=2, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="task-journal-fail",
    )
    tool = AgentTool(
        name="write", description="write",
        parameters={"type": "object", "properties": {}},
        execute=lambda args: called.append(args) or {"success": True},
        read_only=False, effect=ToolEffect.WORKSPACE_WRITE,
    )
    ctx = ExecutionContext(
        process=process, session=process.session,
        workspace_path=str(tmp_path_factory), event_bus=EventBus(),
        cancellation=process.cancellation_event,
    )
    monkeypatch.setattr(ToolPipeline, "_write_invocation_state", lambda *_a, **_k: None)

    result = ToolPipeline().execute(
        {"name": "write", "args": {}}, tool, ctx, "execution", 0,
    )
    assert result.is_error
    assert result.diagnostics["code"] == "INVOCATION_JOURNAL_UNAVAILABLE"
    assert result.receipt["effect_state"] == "not_committed"
    assert called == []


def test_self_execution_completion_requires_independent_review(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="supervisor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("supervisor.control")),
        max_steps=2, ring_level=RingLevel.RING_0,
        actor_kind="supervisor", capability_profile_id="supervisor.control",
        workspace_path=str(tmp_path), task_id="task", task_kind="action",
    )
    process.review_required = True
    process.successful_actions = 1
    process.tool_receipts.append({
        "receipt_id": "self-action-1",
        "tool_name": "edit",
        "succeeded": True,
        "committed": True,
        "effect": "workspace_write",
    })
    from backend.core.loop.completion_protocol import CompletionClaim
    process.completion_claim = CompletionClaim(
        result="done", verification=({"manual": True},), files=("x.py",),
    )
    blocked = HostCompletionEvaluator.evaluate(process, "TASK_COMPLETE")
    assert not blocked.allowed
    assert any(
        "independent reviewer approval is required" in reason
        for reason in blocked.reasons
    )
    process.review_approvals.append("reviewer-1")
    assert HostCompletionEvaluator.evaluate(process, "TASK_COMPLETE").allowed


def test_bounded_self_execution_uses_receipts_without_reviewer(tmp_path_factory: Path):
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="supervisor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("supervisor.control")),
        max_steps=2, ring_level=RingLevel.RING_0,
        actor_kind="supervisor", capability_profile_id="supervisor.control",
        workspace_path=str(tmp_path_factory), task_id="task-bounded",
        task_kind="action",
        context_snapshot={"task_contract": {
            "estimated_complexity": "bounded",
            "independent_workstreams": 1,
            "deliverables": [
                {"kind": "workspace_file", "path": "x.py", "required": True},
            ],
        }},
    )
    (tmp_path_factory / "x.py").write_text("print('ok')\n", encoding="utf-8")
    process.successful_actions = 1
    process.tool_receipts.append({
        "receipt_id": "self-action-1", "tool_name": "write_file",
        "succeeded": True, "committed": True, "effect": "workspace_write",
        "task_id": "task-bounded",
    })
    from backend.core.loop.completion_protocol import CompletionClaim
    process.completion_claim = CompletionClaim(
        result="done", verification=({"receipt_id": "self-action-1"},),
        files=("x.py",),
    )
    assert HostCompletionEvaluator.evaluate(process, "TASK_COMPLETE").allowed
    from backend.core.loop import executor as executor_module

    class Dispatcher:
        _executors = {}

    tools = executor_module._build_internal_tools(
        process, {}, str(tmp_path_factory), object(), Dispatcher(),
    )
    refused = tools["request_review"].execute({
        "process_id": process.process_id,
        "focus": "Review an already verified bounded file",
    })
    assert refused["accepted"] is False
    assert refused["code"] == "REVIEW_NOT_REQUIRED"
    assert manager.children_of(process.process_id) == []


def test_high_complexity_self_execution_still_requires_reviewer(tmp_path_factory: Path):
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="supervisor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("supervisor.control")),
        max_steps=2, ring_level=RingLevel.RING_0,
        actor_kind="supervisor", capability_profile_id="supervisor.control",
        workspace_path=str(tmp_path_factory), task_id="task-high",
        task_kind="action",
        context_snapshot={"task_contract": {
            "estimated_complexity": "high", "independent_workstreams": 1,
        }},
    )
    process.successful_actions = 1
    process.tool_receipts.append({
        "receipt_id": "self-action-1", "tool_name": "write_file",
        "succeeded": True, "committed": True, "effect": "workspace_write",
        "task_id": "task-high",
    })
    from backend.core.loop.completion_protocol import CompletionClaim
    process.completion_claim = CompletionClaim(
        result="done", verification=({"receipt_id": "self-action-1"},),
    )
    blocked = HostCompletionEvaluator.evaluate(process, "TASK_COMPLETE")
    assert not blocked.allowed
    assert any("verification level 2" in reason for reason in blocked.reasons)


def test_supervisor_delegates_and_independent_reviewer_approves(
    tmp_path_factory: Path, monkeypatch,
):
    from backend.core.loop import executor as executor_module

    manager = AgentProcessManager(max_concurrency=3)
    supervisor = manager.fork(
        parent_id=None, role="supervisor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("supervisor.control")),
        max_steps=5, ring_level=RingLevel.RING_0,
        actor_kind="supervisor", capability_profile_id="supervisor.control",
        workspace_path=str(tmp_path_factory), task_id="root-task",
    )

    def fake_agent_step(*, process, **_kwargs):
        if process.actor_kind == "reviewer":
            process.review_claim = {
                "verdict": "approved", "summary": "independent review passed",
                "findings": [], "evidence": [{"kind": "component-test"}],
            }
        process.status = ProcessStatus.COMPLETED
        return {"status": "completed", "process_id": process.process_id}

    monkeypatch.setattr(executor_module, "agent_step", fake_agent_step)

    class Dispatcher:
        _executors = {}

    tools = executor_module._build_internal_tools(
        supervisor, {}, str(tmp_path_factory), object(), Dispatcher(),
    )
    declared = tools["declare_task_contract"].execute({
        "goal": "Delegate one independently reviewed concern",
        "execution_mode": "delegate",
        "delegation_required": True,
        "minimum_delegated_outcomes": 1,
        "deliverables": [],
        "acceptance_criteria": ["Return a result and obtain independent review"],
        "uncertainties": [],
        "requires_user_decision": False,
        "estimated_complexity": "moderate",
        "independent_workstreams": 1,
        "delegation_rationale": "The test explicitly validates independent worker ownership and review.",
        "routing_transition": "delegate_initial",
    })
    assert declared["accepted"] is True
    delegated = tools["delegate_task"].execute({
        "task_description": "Inspect one bounded concern",
        "capability_profile_id": "text.only",
        "task_kind": "answer",
        "target_files": [],
        "acceptance_criteria": ["Return a result"],
        "max_steps": 3,
    })
    child = manager.get(delegated["process_id"])
    assert child is not None
    assert child.session.session_id != supervisor.session.session_id
    manager.wait(child.process_id, timeout=2)
    assert child.status == ProcessStatus.COMPLETED

    review = tools["request_review"].execute({
        "process_id": child.process_id,
        "focus": "Verify the bounded result",
        "max_steps": 3,
    })
    reviewer = manager.get(review["reviewer_process_id"])
    assert reviewer is not None
    manager.wait(reviewer.process_id, timeout=2)
    assert reviewer.status == ProcessStatus.COMPLETED
    assert reviewer.process_id in child.review_approvals

    foreign_supervisor = manager.fork(
        parent_id=None, role="supervisor", tool_registry=ToolRegistry([]),
        max_steps=2, ring_level=RingLevel.RING_0,
        workspace_path=str(tmp_path_factory), task_id="foreign-root",
    )
    foreign_child = manager.fork(
        parent_id=foreign_supervisor.process_id, role="worker",
        tool_registry=ToolRegistry([]), max_steps=2, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="foreign-child",
    )
    assert tools["cancel_agent"].execute({
        "process_id": foreign_child.process_id,
    })["status"] == "not_owned"
    assert tools["send_feedback"].execute({
        "process_id": foreign_child.process_id, "message": "unauthorized",
    })["accepted"] is False

    escaped = tools["delegate_task"].execute({
        "task_description": "Invalid target claim",
        "capability_profile_id": "text.only",
        "task_kind": "answer",
        "target_files": ["../outside.py"],
        "acceptance_criteria": [],
        "max_steps": 3,
    })
    assert escaped["delegated"] is False


def test_manager_serializes_overlapping_declared_resources(tmp_path_factory: Path):
    manager = AgentProcessManager(max_concurrency=2)
    parent = manager.fork(
        parent_id=None, role="supervisor", tool_registry=ToolRegistry([]),
        max_steps=2, ring_level=RingLevel.RING_0,
        workspace_path=str(tmp_path_factory), task_id="root",
    )
    first = manager.fork(
        parent_id=parent.process_id, role="worker", tool_registry=ToolRegistry([]),
        max_steps=2, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="one",
    )
    second = manager.fork(
        parent_id=parent.process_id, role="worker", tool_registry=ToolRegistry([]),
        max_steps=2, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="two",
    )
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def run_first():
        first.status = ProcessStatus.RUNNING
        first_started.set()
        release_first.wait(2)
        first.status = ProcessStatus.COMPLETED
        return {"status": "completed"}

    def run_second():
        second.status = ProcessStatus.RUNNING
        second_started.set()
        second.status = ProcessStatus.COMPLETED
        return {"status": "completed"}

    resource = ["filesystem:src/shared.py"]
    manager.start(first.process_id, run_first, resources=resource)
    assert first_started.wait(1)
    manager.start(second.process_id, run_second, resources=resource)
    assert not second_started.wait(0.2)
    started = time.monotonic()
    assert manager.wait(second.process_id, timeout=0) is None
    assert time.monotonic() - started < 0.1
    release_first.set()
    assert second_started.wait(1)
    assert manager.wait(second.process_id, timeout=2)["status"] == "completed"


def test_manager_allows_shared_readers_but_blocks_writer(tmp_path_factory: Path):
    manager = AgentProcessManager(max_concurrency=3)
    parent = manager.fork(
        parent_id=None, role="supervisor", tool_registry=ToolRegistry([]),
        max_steps=2, ring_level=RingLevel.RING_0,
        workspace_path=str(tmp_path_factory), task_id="root-shared",
    )
    children = [
        manager.fork(
            parent_id=parent.process_id, role="worker",
            tool_registry=ToolRegistry([]), max_steps=2,
            ring_level=RingLevel.RING_3,
            workspace_path=str(tmp_path_factory), task_id=f"shared-{index}",
        )
        for index in range(3)
    ]
    reader_started = [threading.Event(), threading.Event()]
    release_readers = threading.Event()
    writer_started = threading.Event()

    def reader(index):
        reader_started[index].set()
        release_readers.wait(2)
        return {"status": "completed"}

    def writer():
        writer_started.set()
        return {"status": "completed"}

    resource = ["filesystem:src/shared.py"]
    for index in range(2):
        manager.start(
            children[index].process_id,
            lambda index=index: reader(index),
            resources=resource,
            resource_mode="shared",
        )
    assert all(event.wait(1) for event in reader_started)
    manager.start(children[2].process_id, writer, resources=resource)
    assert not writer_started.wait(0.2)
    release_readers.set()
    assert writer_started.wait(1)
    assert manager.wait(children[2].process_id, timeout=2)["status"] == "completed"
