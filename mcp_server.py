#!/usr/bin/env python
"""MCP Server for Gitgo — exposes workflow tools to AI agents via MCP protocol.

Usage:
    python mcp_server.py                    # stdio transport (Claude Desktop)
    python mcp_server.py --sse              # SSE transport (web UIs)
    python mcp_server.py --http             # Streamable HTTP transport

Configure Claude Desktop (`claude_desktop_config.json`):
    {
      "mcpServers": {
        "gitgo": {
          "command": "python",
          "args": ["mcp_server.py"],
          "cwd": "/path/to/gitgo"
        }
      }
    }
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure gitgo is on sys.path when run from repo root or as standalone script
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "gitgo",
    instructions="Gitgo 工作区同步工具 — 扫描文件变更、创建正式提交、同步到备份仓库并推送到远程。",
)


# ── Helpers ────────────────────────────────────────────────────

def _get_config():
    from backend.core.config import ConfigManager
    return ConfigManager.load()


def _get_project(project_name: str):
    cfg = _get_config()
    for p in cfg.projects:
        if p.name == project_name:
            return cfg, p
    return cfg, None


def _init_session(project_name: str):
    """Initialize SyncSession with scan + trial check."""
    from cli.commands import _init_session as cli_init  # noqa: F811
    cfg = _get_config()
    return cli_init(cfg, project_name, with_scan=False)


# ── Tools ──────────────────────────────────────────────────────

@mcp.tool(
    description="列出所有已配置的 Gitgo 项目"
)
def gitgo_list_projects() -> list[dict]:
    cfg = _get_config()
    return [
        {
            "name": p.name,
            "workspace": p.workspace.file_access.path,
            "backup": p.backup_path,
            "commit_prefix": p.commit_format.get("prefix", ""),
        }
        for p in cfg.projects
    ]


@mcp.tool(
    description="获取项目完整状态，包含语义分析（workspace_entropy / suggested_next_action / action_queue / blocked_reason）"
)
def gitgo_status(project: str) -> dict:
    cfg, proj = _get_project(project)
    if proj is None:
        return {"error": "PROJECT_NOT_FOUND", "project": project}
    from backend.core.sync_session import SyncSession
    session = SyncSession(proj, cfg)
    session.step_scan()
    session.step_load_commits()
    session.step_check_trial()
    return session.status_dict(semantic=True)


@mcp.tool(
    description="扫描工作区文件变更，返回变更条目列表和语义状态。不创建提交。"
)
def gitgo_scan(project: str) -> dict:
    cfg, proj = _get_project(project)
    if proj is None:
        return {"error": "PROJECT_NOT_FOUND", "project": project}
    from backend.core.sync_session import SyncSession
    session = SyncSession(proj, cfg)
    entries = session.step_scan()
    session.step_load_commits()
    return {
        "entries_total": len(entries),
        "entries_changed": sum(1 for e in entries if e.selected),
        "entries": [
            {"path": e.path, "status": e.status, "selected": e.selected}
            for e in entries
        ],
        "semantic": session.status_dict(semantic=True).get("semantic", {}),
    }


@mcp.tool(
    description="从选中的 workspace commits 创建 formal commit。不指定 indices 时使用当前 selected_workspace。"
)
def gitgo_formalize(
    project: str,
    indices: list[int] | None = None,
    message: str | None = None,
) -> dict:
    cfg, proj = _get_project(project)
    if proj is None:
        return {"error": "PROJECT_NOT_FOUND", "project": project}
    from backend.core.sync_session import SyncSession
    session = SyncSession(proj, cfg)
    session.step_scan()
    session.step_load_commits()

    if indices is not None:
        session.selected_workspace = set(indices)

    fc = session.step_create_formal_commit(message=message)
    if fc:
        return {
            "commit": f"[{fc.prefix}-{fc.number}]",
            "message": fc.message,
            "source_indices": list(fc.source_indices),
            "synced": fc.synced,
            "pushed": fc.pushed,
        }
    return {"error": "FORMALIZE_FAILED", "reason": "No workspace commits selected or commit creation failed"}


@mcp.tool(
    description="将所有未同步的 formal commits 同步到备份仓库。"
)
def gitgo_sync(project: str) -> dict:
    cfg, proj = _get_project(project)
    if proj is None:
        return {"error": "PROJECT_NOT_FOUND", "project": project}
    from backend.core.sync_session import SyncSession
    session = SyncSession(proj, cfg)
    session.step_scan()
    session.step_load_commits()
    ok = session.step_sync()
    return {
        "synced": ok,
        "formal_commits": [
            {"commit": f"[{fc.prefix}-{fc.number}]", "synced": fc.synced, "pushed": fc.pushed}
            for fc in session.formal_commits
        ],
    }


@mcp.tool(
    description="将已同步的 formal commits 推送到远程仓库。可选跳过安全检查。"
)
def gitgo_push(project: str, skip_security: bool = False) -> dict:
    cfg, proj = _get_project(project)
    if proj is None:
        return {"error": "PROJECT_NOT_FOUND", "project": project}
    from backend.core.sync_session import SyncSession
    session = SyncSession(proj, cfg)
    session.step_scan()
    session.step_load_commits()
    ok, warning = session.step_push(skip_scan=True)
    return {
        "pushed": ok,
        "security_warning": warning,
        "formal_commits": [
            {"commit": f"[{fc.prefix}-{fc.number}]", "synced": fc.synced, "pushed": fc.pushed}
            for fc in session.formal_commits
        ],
    }


@mcp.tool(
    description="列出 Trial 仓库的所有 incoming changes（外部提交），含 triage 状态。"
)
def gitgo_trial_list(project: str) -> list[dict]:
    cfg, proj = _get_project(project)
    if proj is None:
        return [{"error": "PROJECT_NOT_FOUND", "project": project}]
    from backend.core.sync_session import SyncSession
    session = SyncSession(proj, cfg)
    session.step_check_trial()
    return [
        {
            "index": i,
            "hash": c.hash,
            "message": c.message,
            "author": c.author,
            "date": c.date,
            "triage": c.triage.value,
        }
        for i, c in enumerate(session.incoming_changes)
    ]


@mcp.tool(
    description="对 Trial incoming change 执行三叉决策：accept（接受入备份）/ promote（提升到工作区）/ discard（丢弃）"
)
def gitgo_trial_triage(project: str, index: int, action: str) -> dict:
    if action not in ("accept", "promote", "discard"):
        return {"error": "INVALID_ACTION", "action": action, "valid": ["accept", "promote", "discard"]}
    cfg, proj = _get_project(project)
    if proj is None:
        return {"error": "PROJECT_NOT_FOUND", "project": project}
    from backend.core.sync_session import SyncSession
    session = SyncSession(proj, cfg)
    session.step_check_trial()
    ok = session.step_triage_incoming(index, action)
    return {"triaged": ok, "action": action, "index": index}


@mcp.tool(
    description="运行完整的一键同步工作流：scan → formalize → sync → push。适用于 CI/自动化场景。"
)
def gitgo_run_workflow(
    project: str,
    message: str | None = None,
    skip_push: bool = False,
) -> dict:
    cfg, proj = _get_project(project)
    if proj is None:
        return {"error": "PROJECT_NOT_FOUND", "project": project}
    from backend.core.sync_session import SyncSession
    session = SyncSession(proj, cfg)
    success = session.run_full_workflow(commit_message=message, skip_push=skip_push)
    return {
        "success": success,
        "status": session.status_dict(semantic=True),
    }


# ── P3 Suggest Tools ───────────────────────────────────────────

def _build_suggest_result(project_name: str, suggest_type: str) -> dict:
    """Shared helper: init session, build suggest context, return full result dict."""
    import json
    cfg, proj = _get_project(project_name)
    if proj is None:
        return {"error": "PROJECT_NOT_FOUND", "project": project_name}
    from backend.core.sync_session import SyncSession
    from cli.commands import (
        _build_formalize_context,
        _build_triage_context,
        _build_summary_context,
    )
    session = SyncSession(proj, cfg)
    if suggest_type == "formalize":
        session.step_scan()
        session.step_load_commits()
        context = _build_formalize_context(session)
    elif suggest_type == "triage":
        session.step_check_trial()
        context = _build_triage_context(session)
    elif suggest_type == "summary":
        context = _build_summary_context(session)
    else:
        return {"error": "UNKNOWN_SUGGEST_TYPE", "suggest_type": suggest_type}
    return {"suggest": suggest_type, "project": project_name,
            "context": context}


@mcp.tool(
    description="获取 AI commit 分组和 message 建议的 context。包含 workspace commits 列表、diff 统计（新增/删除行数+顶层符号）、编号信息。Agent 拿到 context 后自行调用 LLM 分析，生成分组+message 建议，人确认后通过 formalize 逐组执行。"
)
def gitgo_suggest_formalize(project: str) -> dict:
    return _build_suggest_result(project, "formalize")


@mcp.tool(
    description="获取 trial triage 建议的 context。包含 incoming changes 列表、diff 统计、release 仓库上下文。Agent 分析后建议 accept/promote/discard，含 confidence 和 reason，人确认后通过 gitgo_trial_triage 逐项执行。"
)
def gitgo_suggest_triage(project: str) -> dict:
    return _build_suggest_result(project, "triage")


@mcp.tool(
    description="获取变更语义摘要的 context。包含 workspace/trial/release 三段统计信息。Agent 基于 context 生成自然语言变更叙述，供状态展示用。"
)
def gitgo_suggest_summary(project: str) -> dict:
    return _build_suggest_result(project, "summary")


# ── P4 Governance Tools ────────────────────────────────────────

@mcp.tool(
    description="获取 AI 建议质量度量：采纳率/修改率/拒绝率，按类型、commit type、模块切片。仅用 indices Jaccard 重叠度，不做 message 文本比较。"
)
def gitgo_governance_quality(project: str) -> dict:
    from backend.core.governance import load_suggestion_pairs, compute_quality_metrics
    pairs = load_suggestion_pairs(project)
    return compute_quality_metrics(pairs)


@mcp.tool(
    description="检测变更模式：共变模块（哪些目录总是一起变更）、commit 类型聚类（formalize 的类型分布和多源合并率）、trial 后续影响（accept 后触发 workspace 变更的概率）。"
)
def gitgo_governance_patterns(project: str) -> dict:
    from backend.core.governance import build_patterns_report
    return build_patterns_report(project)


@mcp.tool(
    description="构建语义变更图：formal commit 节点 + file_overlap（文件重叠 Jaccard≥0.3）/ same_push（同次推送）/ trial_source（来自 trial accept）三种边。"
)
def gitgo_governance_graph(project: str) -> dict:
    from backend.core.governance import build_graph
    return build_graph(project)


@mcp.tool(
    description="列出项目的发布历史：从所有 push 记录提取推送时间、包含的 commits 列表、以及 release note（如有）。发布按时间倒序排列。"
)
def gitgo_governance_releases(project: str) -> dict:
    from backend.core.governance import list_releases
    return list_releases(project)


@mcp.tool(
    description="为项目的最新一次 push 记录添加 release note（发布理由/说明）。用于 agent 发布后记录本次发布的背景和目的。message 为必填。"
)
def gitgo_governance_release_note(project: str, message: str) -> dict:
    from backend.core.governance import add_release_note
    ok = add_release_note(project, message)
    return {"ok": ok, "message": message}


# ── Entry Point ────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Gitgo MCP Server")
    parser.add_argument("--sse", action="store_true", help="Use SSE transport")
    parser.add_argument("--http", action="store_true", help="Use streamable HTTP transport")
    args = parser.parse_args()

    if args.sse:
        mcp.run(transport="sse")
    elif args.http:
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
