"""Canonical definitions for built-in Agent tools.

Business handlers, model-visible schemas and execution contracts meet here.
The daemon binds a workspace once; callers cannot replace the injected private
arguments or manufacture a stronger tool contract.
"""

from __future__ import annotations

from pathlib import Path
import sys

from backend.core.prompt_templates import load_prompt, render_prompt

from backend.core.loop.execution_contract import NATIVE_PROCESS, NATIVE_FILE_READ, NATIVE_WORKSPACE_PRODUCER, data_broker

from backend.core.loop.agent_tool import (
    AgentTool,
    ApprovalMode,
    CancellationMode,
    ToolEffect,
)


def build_workspace_tools(workspace_path: str | Path) -> dict[str, AgentTool]:
    from .document_service import document_create
    from . import workspace_tools as governance_handlers
    from .web_tools import web_search, web_fetch
    workspace = str(Path(workspace_path).resolve())

    def inject_workspace(args: dict) -> dict:
        prepared = dict(args or {})
        prepared["_workspace"] = workspace
        return prepared

    common = {
        "prepare_args": inject_workspace,
        "isolated": True,
        "execution_contract": NATIVE_PROCESS,
        "cancellation": CancellationMode.ISOLATED_PROCESS,
        "composable": True,
    }
    def file_service(name):
        from backend.core.tools import workspace_tools
        def execute(args):
            return getattr(workspace_tools, name)(args)
        return {**common, "isolated": False,
                "execution_contract": data_broker("files.workspace." + name),
                "cancellation": CancellationMode.NON_INTERRUPTIBLE,
                "execute": execute}
    tools = {
        "run_test": AgentTool(
            name="run_test", description=load_prompt("tools/run_test"),
            execution_contract=data_broker("tests.workspace.run"),
            parameters={"type": "object", "properties": {
                "test_id": {"type": "string"}, "target": {"type": "string"},
                "seeds": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "integer"}},
                "input_files": {"type": "array", "items": {"type": "string"},
                    "description": "Explicit local source/config/data dependencies; target is always included. Omit for bounded business-workspace coverage."},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 900}}, "required": ["test_id", "target"]},
            execute=_unreachable, read_only=False, effect=ToolEffect.PROCESS,
            cancellation=CancellationMode.COOPERATIVE,
            resources=["process:pytest", "filesystem:.gitgo/test_manifest.json"], timeout=900),
        "read_file": AgentTool(
            name="read_file",
            description=(
                load_prompt('tools/read_file')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                    "bind_dependency": {"type": "boolean", "description": "Pin this whole file as an important input to the task result; sealed-result promotion requires its version to remain applicable. Ordinary reads need no pin."},
                },
                "required": ["path"],
            },
            execute=_unreachable,
            read_only=True,
            effect=ToolEffect.READ,
            resources=[],
            runner_name="read_file",
            idempotent=True,
            **{**common, "execution_contract": NATIVE_FILE_READ},
        ),
        "list_files": AgentTool(
            name="list_files",
            description=(load_prompt('tools/list_files')),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "pattern": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 5000},
                    "include_hidden": {"type": "boolean"},
                    "respect_ignore": {"type": "boolean"},
                    "exclude": {"type": "array", "items": {"type": "string"}},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 30},
                },
                "required": [],
            },
            execute=_unreachable,
            read_only=True,
            effect=ToolEffect.READ,
            resources=[],
            runner_name="list_files",
            idempotent=True,
            **common,
        ),
        "search_text": AgentTool(
            name="search_text",
            description=(
                load_prompt('tools/search_text')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "literal": {"type": "boolean"},
                    "case_sensitive": {"type": "boolean"},
                    "include": {"type": "array", "items": {"type": "string"}},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 2000},
                    "output_mode": {"type": "string", "enum": ["content", "files", "count"]},
                    "context_lines": {"type": "integer", "minimum": 0, "maximum": 5},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
                    "exclude": {"type": "array", "items": {"type": "string"}},
                    "include_hidden": {"type": "boolean"},
                    "respect_ignore": {"type": "boolean"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 30},
                },
                "required": ["pattern"],
            },
            execute=_unreachable,
            read_only=True,
            effect=ToolEffect.READ,
            resources=[],
            runner_name="search_text",
            idempotent=True,
            timeout=45,
            **common,
        ),
        "document_open": AgentTool(
            name="document_open",
            description=(
                load_prompt('tools/document_open')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "max_chars": {"type": "integer", "minimum": 1000, "maximum": 100000},
                },
                "required": ["path"],
            },
            execute=_unreachable,
            read_only=True,
            effect=ToolEffect.READ,
            resources=[],
            runner_name="document_open",
            idempotent=True,
            timeout=120,
            **common,
        ),
        "document_create": AgentTool(
            name="document_create",
            description=load_prompt('tools/document_create'),
            parameters={
                "type": "object",
                "properties": {
                    "lesson_feedback_id": {"type": "string"},
                    "path": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "slides": {"type": "array", "items": {"type": "object"}},
                    "sheets": {"type": "array", "items": {"type": "object"}},
                    "overwrite": {"type": "boolean"},
                    "expected_sha256": {"type": "string"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            execute=document_create,
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:*"],
            runner_name="document_create",
            idempotent=False,
            timeout=120,
            **{**common, "isolated": False, "execution_contract": data_broker("files.workspace.document_create"),
               "cancellation": CancellationMode.NON_INTERRUPTIBLE},
        ),
        "web_search": AgentTool(
            name="web_search",
            description=(
                load_prompt('tools/web_search')
            ),
            parameters={"type": "object", "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                "language": {"type": "string"},
            }, "required": ["query"]},
            execute=web_search,
            read_only=True,
            effect=ToolEffect.EXTERNAL_READ,
            approval=ApprovalMode.ALLOW,
            resources=["network:public-search"],
            runner_name="web_search",
            idempotent=True,
            timeout=45,
            **{**common, "isolated": False,
               "execution_contract": data_broker("network.public.search"),
               "cancellation": CancellationMode.NON_INTERRUPTIBLE},
        ),
        "web_fetch": AgentTool(
            name="web_fetch",
            description=(
                load_prompt('tools/web_fetch')
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
                "additionalProperties": False,
            },
            execute=web_fetch,
            read_only=True,
            effect=ToolEffect.EXTERNAL_READ,
            approval=ApprovalMode.ALLOW,
            resources=["network:public-fetch"],
            runner_name="web_fetch",
            idempotent=True,
            timeout=45,
            **{**common, "isolated": False,
               "execution_contract": data_broker("network.public.fetch"),
               "cancellation": CancellationMode.NON_INTERRUPTIBLE},
        ),
        "edit_file": AgentTool(
            name="edit_file",
            description=(
                load_prompt('tools/edit_file')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "lesson_feedback_id": {"type": "string", "description": "Use the feedback_id returned by a before-write advisory when choosing to resubmit the same candidate. This does not grant authority or certify understanding."},
                    "path": {"type": "string", "description": "Workspace-relative path of an existing UTF-8 text file."},
                    "old_string": {"type": "string", "description": "Smallest current unique text anchor to replace."},
                    "new_string": {"type": "string", "description": "Replacement text for that exact anchor."},
                    "expected_sha256": {"type": "string", "description": "Current sha256 returned by the latest read_file call."},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string", "expected_sha256"],
                "additionalProperties": False,
            },
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:*"],
            runner_name="edit_file",
            idempotent=False,
            **file_service("edit_file"),
        ),
        "write_file": AgentTool(
            name="write_file",
            description=(
                load_prompt('tools/write_file')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "lesson_feedback_id": {"type": "string", "description": "Use the feedback_id returned by a before-write advisory when choosing to resubmit the same candidate. This does not grant authority or certify understanding."},
                    "path": {"type": "string", "description": "Workspace-relative output path."},
                    "content": {"type": "string", "description": "Complete UTF-8 file content."},
                    "create_only": {"type": "boolean", "default": True, "description": "Keep true for creation. Set false only for a CAS replacement of an existing file."},
                    "expected_sha256": {"type": "string", "description": "Required with create_only=false; use the current sha256 from read_file."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:*"],
            runner_name="write_file",
            idempotent=False,
            **file_service("write_file"),
        ),
        "delete_file": AgentTool(
            name="delete_file",
            description=(
                load_prompt('tools/delete_file')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "lesson_feedback_id": {"type": "string", "description": "Reference the received before-action advisory when resubmitting this exact deletion; this is not approval."},
                    "path": {"type": "string"},
                    "expected_sha256": {"type": "string"},
                },
                "required": ["path", "expected_sha256"],
                "additionalProperties": False,
            },
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:*"],
            runner_name="delete_file",
            idempotent=False,
            **file_service("delete_file"),
        ),
        "apply_patch": AgentTool(
            name="apply_patch",
            description=(
                load_prompt('tools/apply_patch')
            ),
            parameters={
                "type": "object",
                "properties": {"lesson_feedback_id": {"type": "string", "description": "Reference the received advisory for this entire unchanged patch candidate. This is not approval."}, "patch": {"type": "string", "description": "A complete git-compatible unified diff, including file headers for every fragment."}},
                "required": ["patch"],
                "additionalProperties": False,
            },
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:*"],
            runner_name="apply_patch",
            idempotent=False,
            timeout=60,
            **file_service("apply_patch"),
        ),
        "exec_command": AgentTool(
            name="exec_command",
            description=(
                load_prompt('tools/exec_command')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "candidate_ref": {"type": "string", "description": "Resume this exact frozen execution output without running the command again."},
                    "lesson_feedback_id": {"type": "string", "description": "Received advice for the unchanged complete output candidate; not approval."},
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
            execute=_unreachable,
            read_only=False,
            effect=ToolEffect.PROCESS,
            resources=["process:workspace"],
            runner_name="exec_command",
            idempotent=False,
            timeout=1800,
            timeout_argument="timeout",
            timeout_default=120,
            **{**common, "execution_contract": NATIVE_WORKSPACE_PRODUCER},
        ),
        "shell_script": AgentTool(
            name="shell_script",
            description=(
                render_prompt(
                    'tools/shell_script',
                    auto_shell='powershell' if sys.platform == 'win32' else 'bash',
                    platform_guidance=load_prompt(
                        'tools/shell_powershell' if sys.platform == 'win32' else 'tools/shell_posix',
                    ),
                )
            ),
            parameters={
                "type": "object",
                "properties": {
                    "candidate_ref": {"type": "string", "description": "Resume this exact frozen execution output without running the command again."},
                    "lesson_feedback_id": {"type": "string", "description": "Received advice for the unchanged complete output candidate; not approval."},
                    "script": {"type": "string"},
                    "shell": {"type": "string", "enum": ["bash", "powershell", "auto"], "default": "auto"},
                    "purpose": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 1800},
                },
                "required": ["script", "purpose"],
                "additionalProperties": False,
            },
            execute=_unreachable,
            read_only=False,
            effect=ToolEffect.PROCESS,
            approval=ApprovalMode.ASK,
            approval_per_invocation=True,
            resources=["process:shell"],
            runner_name="shell_script",
            idempotent=False,
            timeout=1800,
            timeout_argument="timeout",
            timeout_default=120,
            **{**common, "execution_contract": NATIVE_WORKSPACE_PRODUCER},
        ),
        "dependency_feedback": AgentTool(
            name="dependency_feedback",
            description=(
                "Confirm or dismiss a dependency edge after tests, review or runtime "
                "evidence. Dismissals expire when either file changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "dependent": {"type": "string"},
                    "dependency": {"type": "string"},
                    "confirmed": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["dependent", "dependency", "confirmed", "reason"],
            },
            execute=_unreachable,
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:.gitgo/dependency_feedback.json"],
            runner_name="dependency_feedback",
            idempotent=True,
            **common,
        ),
        "rebuild_dependency_graph": AgentTool(
            name="rebuild_dependency_graph",
            description="Rebuild the multi-signal dependency graph and return coverage counts.",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=governance_handlers.rebuild_dependency_graph,
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:.gitgo/dependency_graph.v2.json"],
            runner_name="rebuild_dependency_graph",
            idempotent=True,
            timeout=120,
            **{**common, "isolated": False, "execution_contract": data_broker("governance.rebuild_dependency_graph"), "cancellation": CancellationMode.NON_INTERRUPTIBLE},
        ),
    }

    from .file_compat import build_legacy_file_tools
    tools.update(build_legacy_file_tools(tools))
    return tools


def _unreachable(args: dict) -> dict:
    raise RuntimeError("isolated workspace tool was invoked outside ProcessToolRunner")
