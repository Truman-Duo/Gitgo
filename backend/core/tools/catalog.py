"""Canonical definitions for built-in Agent tools.

Business handlers, model-visible schemas and execution contracts meet here.
The daemon binds a workspace once; callers cannot replace the injected private
arguments or manufacture a stronger tool contract.
"""

from __future__ import annotations

from pathlib import Path
import sys

from backend.core.prompt_templates import load_prompt, render_prompt

from backend.core.loop.agent_tool import (
    AgentTool,
    ApprovalMode,
    CancellationMode,
    ToolEffect,
)


def build_workspace_tools(workspace_path: str | Path) -> dict[str, AgentTool]:
    workspace = str(Path(workspace_path).resolve())

    def inject_workspace(args: dict) -> dict:
        prepared = dict(args or {})
        prepared["_workspace"] = workspace
        return prepared

    common = {
        "prepare_args": inject_workspace,
        "isolated": True,
        "cancellation": CancellationMode.ISOLATED_PROCESS,
        "composable": True,
    }
    return {
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
            **common,
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
            description=(
                "Create a text/Markdown, Word, PowerPoint, Excel or PDF artifact "
                "inside the task workspace. Use content for text-oriented output, "
                "slides for PPTX and sheets for XLSX. Existing files require an "
                "explicit overwrite flag plus the current SHA-256."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "slides": {"type": "array", "items": {"type": "object"}},
                    "sheets": {"type": "array", "items": {"type": "object"}},
                    "overwrite": {"type": "boolean"},
                    "expected_sha256": {"type": "string"},
                },
                "required": ["path"],
            },
            execute=_unreachable,
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:*"],
            runner_name="document_create",
            idempotent=False,
            timeout=120,
            **common,
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
            execute=_unreachable,
            read_only=True,
            effect=ToolEffect.EXTERNAL_READ,
            approval=ApprovalMode.ALLOW,
            resources=["network:public-search"],
            runner_name="web_search",
            idempotent=True,
            timeout=45,
            **common,
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
            execute=_unreachable,
            read_only=True,
            effect=ToolEffect.EXTERNAL_READ,
            approval=ApprovalMode.ALLOW,
            resources=["network:public-fetch"],
            runner_name="web_fetch",
            idempotent=True,
            timeout=45,
            **common,
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
            execute=_unreachable,
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:*"],
            runner_name="edit_file",
            idempotent=False,
            **common,
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
            execute=_unreachable,
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:*"],
            runner_name="write_file",
            idempotent=False,
            **common,
        ),
        "delete_file": AgentTool(
            name="delete_file",
            description=(
                load_prompt('tools/delete_file')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "expected_sha256": {"type": "string"},
                },
                "required": ["path", "expected_sha256"],
                "additionalProperties": False,
            },
            execute=_unreachable,
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:*"],
            runner_name="delete_file",
            idempotent=False,
            **common,
        ),
        "apply_patch": AgentTool(
            name="apply_patch",
            description=(
                load_prompt('tools/apply_patch')
            ),
            parameters={
                "type": "object",
                "properties": {"patch": {"type": "string", "description": "A complete git-compatible unified diff, including file headers for every fragment."}},
                "required": ["patch"],
                "additionalProperties": False,
            },
            execute=_unreachable,
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:*"],
            runner_name="apply_patch",
            idempotent=False,
            timeout=60,
            **common,
        ),
        "exec_command": AgentTool(
            name="exec_command",
            description=(
                load_prompt('tools/exec_command')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["argv"],
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
            **common,
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
                    "script": {"type": "string"},
                    "shell": {"type": "string", "enum": ["bash", "powershell", "auto"], "default": "bash"},
                    "purpose": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 1800},
                },
                "required": ["script", "purpose"],
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
            **common,
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
            execute=_unreachable,
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:.gitgo/dependency_graph.v2.json"],
            runner_name="rebuild_dependency_graph",
            idempotent=True,
            timeout=120,
            **common,
        ),
    }


def _unreachable(args: dict) -> dict:
    raise RuntimeError("isolated workspace tool was invoked outside ProcessToolRunner")
