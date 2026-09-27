"""Canonical definitions for built-in Agent tools.

Business handlers, model-visible schemas and execution contracts meet here.
The daemon binds a workspace once; callers cannot replace the injected private
arguments or manufacture a stronger tool contract.
"""

from __future__ import annotations

from pathlib import Path

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
                "Read a workspace text file by line window. Returns a SHA-256 version "
                "token; pass it to edit_file/write_file to prevent stale overwrites."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
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
            description="List workspace files with a glob, stable paths and bounded output.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "pattern": {"type": "string"},
                    "max_results": {"type": "integer"},
                    "include_hidden": {"type": "boolean"},
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
                "Search text across workspace files. Uses ripgrep when available and "
                "returns structured file/line matches."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "literal": {"type": "boolean"},
                    "case_sensitive": {"type": "boolean"},
                    "include": {"type": "array", "items": {"type": "string"}},
                    "max_results": {"type": "integer"},
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
                "Extract a bounded text page from a workspace document. Supports "
                "txt/md, PDF, Word, PowerPoint and Excel through one stable interface."
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
                "Search the public web through the active provider's native search "
                "capability, with the configured SearXNG transport as fallback. This is "
                "an anonymous public read and does not need a permission request. Use "
                "web_fetch on selected result URLs when source text is needed."
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
                "Retrieve one anonymous public HTTP(S) page selected from search results. "
                "The Host blocks credentials, local/private addresses, unsafe redirects, "
                "binary content and oversized responses. No permission request is needed."
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
                "Replace a small exact unique string in an existing workspace file "
                "atomically. First read the file, then use its expected_sha256. Keep "
                "old_string to the smallest stable unique anchor; for multiple distant "
                "changes, make bounded calls and reread after each successful write. "
                "Do not resend the whole file as old_string."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "Write a complete workspace file atomically. For a new path, omit "
                "create_only (it defaults to true). For an existing path, first call "
                "read_file, then set create_only=false and pass that current "
                "expected_sha256. Never delete an existing file merely to overwrite it."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                "Recoverably delete one workspace file after reading its current SHA-256. "
                "The Host moves it into project-local .gitgo/trash, returns a restore "
                "reference and emits a deletion diff. Directories and .gitgo internals "
                "cannot be deleted through this tool."
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
                "Apply a bounded standard unified diff inside the workspace. Each file "
                "must have --- a/path and +++ b/path headers followed by valid "
                "@@ -old_start,old_count +new_start,new_count @@ hunks. The complete "
                "patch is preflight-checked before any file is changed. Prefer "
                "edit_file for a small exact replacement."
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
                "Run one argv-based build, test, inspection or local development command "
                "inside the workspace. Network tools, nested shells, governed git writes "
                "and external absolute paths are denied."
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
                "Run one Bash script inside the workspace when argv-based exec_command "
                "cannot express the required pipes, redirections or shell control flow. "
                "This is a sensitive capability: explain the user-visible purpose with "
                "request_permission and obtain approval for these exact arguments before "
                "every invocation. Never use it merely for command convenience."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "script": {"type": "string"},
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
