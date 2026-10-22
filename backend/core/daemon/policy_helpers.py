"""Policy/snapshot helpers — workspace snapshot + rejection-chain harvest + LLM config.

Extracted from daemon/__init__.py (pure structural refactor).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from backend.core.config import ProjectConfig
from backend.core.sync_session import SyncSession
from backend.core.daemon.emit import _emit


def _snapshot_workspace(session: SyncSession, project: ProjectConfig) -> list[str] | None:
    """每轮结束时在 workspace 做 git commit 快照。"""
    from backend.core.loop.worktree import AgentWorktreeManager, WorktreeError

    ws = Path(session.workspace_path)
    changed = [e.rel_path for e in session.entries if e.status != "same"]

    try:
        AgentWorktreeManager._run(ws, ["git", "add", "-A"], timeout_seconds=30)
        staged = AgentWorktreeManager._run(ws, ["git", "diff", "--cached", "--quiet"],
                                         check=False, timeout_seconds=30)
        if staged.returncode == 0:
            return []
        if staged.returncode != 1:
            raise WorktreeError(staged.stderr.decode("utf-8", errors="replace"))

        msg = f"gitgo: round snapshot [{datetime.now().strftime('%H:%M:%S')}]\n\n"
        msg += f"变更文件: {len(changed)}\n"
        if changed:
            msg += "\n".join(f"  {f}" for f in changed[:20])
            if len(changed) > 20:
                msg += f"\n  ... 还有 {len(changed) - 20} 个文件"

        result = AgentWorktreeManager._run(ws,
            ["git", "-c", "user.name=Gitgo Snapshot", "-c", "user.email=snapshot@local.gitgo.invalid",
             "commit", "-m", msg], timeout_seconds=30)

        if result.returncode == 0:
            from backend.core.history import HistoryManager
            HistoryManager.add_operation(
                project.name, "workspace_state_snapshot", "success",
                {"files_changed": changed,
                 "round_time": datetime.now().isoformat()},
                correlation_id=session._correlation_id,
            )
            _emit({"event": "workspace_snapshot", "files": len(changed)})
            return changed
        return []  # no changes to commit
    except (WorktreeError, OSError) as e:
        event = {"event": "snapshot_error", "error": str(e), "effect_state": "not_inferred"}
        if getattr(e, "diagnostic", None) is not None:
            event["diagnostic"] = e.diagnostic
        _emit(event)
        return None


def _harvest_from_rejection_chain(project_name: str, rejections: list, session: SyncSession) -> None:
    """Actual task revisions are not frequency-derived knowledge."""
    from backend.core.history import HistoryManager
    from backend.core.knowledge.harvest import capture_signal
    with HistoryManager.workspace_scope(str(session.workspace_path)):
        for revision in rejections[-3:]:
            detail = revision.detail if isinstance(revision.detail, dict) else {}
            identity = str(getattr(revision, "correlation_id", "") or "") + ":" + str(revision.timestamp)
            capture_signal("user_task_revision", {"information_kind": "task",
                "reason": detail.get("reason", ""), "instruction": detail.get("instruction", ""),
                "source": "user_task_revision"}, project_name, source_event_id=identity)


def _runtime_llm_from_config(cfg):
    """Honor explicit environment overrides; otherwise use configured routing."""
    if cfg is None:
        return None
    from backend.core.loop.llm import LLMProvider
    if all(os.environ.get(k) for k in ("GITGO_LLM_BASE_URL", "GITGO_LLM_API_KEY", "GITGO_LLM_MODEL")):
        return LLMProvider(cfg[0], cfg[1], cfg[2], protocol=cfg[3], capabilities=cfg[4])
    from backend.core.llm_config import LLMConfigManager
    from backend.core.loop.provider_router import configured_runtime_provider
    config = LLMConfigManager.load()
    active = next((p for p in config.get("providers", []) if p.get("id") == config.get("active_provider")), None)
    if active and (active.get("base_url"), active.get("api_key"), active.get("model_id")) == cfg[:3]:
        return configured_runtime_provider(config)
    # Older embedded callers can still provide the canonical tuple resolver.
    return LLMProvider(cfg[0], cfg[1], cfg[2], protocol=cfg[3], capabilities=cfg[4])


def _resolve_llm_config(workspace: str) -> tuple | None:
    """解析 LLM 配置。优先级：环境变量 > llm_config.json active_provider。"""
    base_url = os.environ.get("GITGO_LLM_BASE_URL", "")
    api_key = os.environ.get("GITGO_LLM_API_KEY", "")
    model_id = os.environ.get("GITGO_LLM_MODEL", "")
    protocol = os.environ.get("GITGO_LLM_PROTOCOL", "openai_chat")
    env_capabilities = {
        "context_window": int(os.environ.get("GITGO_LLM_CONTEXT_WINDOW", "128000")),
        "max_output_tokens": int(os.environ.get("GITGO_LLM_MAX_OUTPUT_TOKENS", "4096")),
    }

    if base_url and api_key and model_id:
        return (base_url, api_key, model_id, protocol, env_capabilities)

    if workspace:
        try:
            from backend.core.llm_config import LLMConfigManager
            active = LLMConfigManager.get_active()
            if active:
                return (
                    active.base_url, active.api_key, active.model_id,
                    active.protocol, active.runtime_capabilities(),
                )
        except Exception:
            pass

    return None


def bind_project_tool_context(tool, workspace, project_name):
    """Bind facts after registry assembly without rewriting execution authority."""
    previous = tool.prepare_args
    def prepare(args):
        prepared = dict(previous(args) if previous else args or {})
        prepared["_workspace"] = str(workspace)
        properties = tool.parameters.get("properties", {})
        if "workspace_path" in properties:
            prepared["workspace_path"] = str(workspace)
        if "project_name" in properties:
            prepared["project_name"] = project_name
        return prepared
    tool.prepare_args = prepare
