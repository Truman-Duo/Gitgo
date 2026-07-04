"""MCP tools — sync, formalize, trial, workflow, and gate operations."""

from mcp_tools.helpers import get_config, get_project, init_session, build_suggest_result


def register(mcp):
    """Register sync/workflow/formal/trial tools on FastMCP instance."""

    @mcp.tool(description="从选中的 workspace commits 创建 formal commit。支持可选 template 参数选择模板。不指定 indices 时使用当前 selected_workspace。")
    def gitgo_formalize(project: str, indices: list[int] | None = None, message: str | None = None, template: str | None = None) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_scan()
        session.step_load_commits()
        if indices is not None:
            session.selected_workspace = set(indices)
        fc = session.step_create_formal_commit(message=message, template_name=template)
        if fc:
            return {"commit": f"[{fc.prefix}-{fc.number}]", "message": fc.message,
                    "source_indices": list(fc.source_indices), "synced": fc.synced, "pushed": fc.pushed}
        return {"error": "FORMALIZE_FAILED", "reason": "No workspace commits selected or commit creation failed"}

    @mcp.tool(description="将所有未同步的 formal commits 同步到备份仓库。")
    def gitgo_sync(project: str) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_scan()
        session.step_load_commits()
        ok = session.step_sync()
        return {"synced": ok, "formal_commits": [
            {"commit": f"[{fc.prefix}-{fc.number}]", "synced": fc.synced, "pushed": fc.pushed}
            for fc in session.formal_commits
        ]}

    @mcp.tool(description="将已同步的 formal commits 推送到远程仓库。可选跳过安全检查。strip_authorship=True 时清除 AI 合作痕迹。")
    def gitgo_push(project: str, skip_security: bool = False, strip_authorship: bool = False, aggressive: bool = False) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_scan()
        session.step_load_commits()
        if strip_authorship:
            from backend.core.authorship import apply_authorship_filter
            apply_authorship_filter(session, aggressive=aggressive)
        ok, warning = session.step_push(skip_scan=True)
        return {"pushed": ok, "security_warning": warning, "formal_commits": [
            {"commit": f"[{fc.prefix}-{fc.number}]", "synced": fc.synced, "pushed": fc.pushed}
            for fc in session.formal_commits
        ]}

    @mcp.tool(description="列出 Trial 仓库的所有 incoming changes（外部提交），含 triage 状态。")
    def gitgo_trial_list(project: str) -> list[dict]:
        cfg, proj = get_project(project)
        if proj is None:
            return [{"error": "PROJECT_NOT_FOUND", "project": project}]
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_check_trial()
        return [{"index": i, "hash": c.hash, "message": c.message,
                 "author": c.author, "date": c.date, "triage": c.triage.value}
                for i, c in enumerate(session.incoming_changes)]

    @mcp.tool(description="对 Trial incoming change 执行三叉决策：accept（接受入备份）/ promote（提升到工作区）/ discard（丢弃）")
    def gitgo_trial_triage(project: str, index: int, action: str) -> dict:
        if action not in ("accept", "promote", "discard"):
            return {"error": "INVALID_ACTION", "action": action, "valid": ["accept", "promote", "discard"]}
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_check_trial()
        ok = session.step_triage_incoming(index, action)
        return {"triaged": ok, "action": action, "index": index}

    @mcp.tool(description="运行完整的一键同步工作流：scan → formalize → sync → push。适用于 CI/自动化场景。")
    def gitgo_run_workflow(project: str, message: str | None = None, template: str | None = None, skip_push: bool = False) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        success = session.run_full_workflow(commit_message=message, skip_push=skip_push)
        return {"success": success, "status": session.status_dict(semantic=True)}

    # ── Suggest Tools ──

    @mcp.tool(description="获取 AI commit 分组和 message 建议的 context。包含 workspace commits 列表、diff 统计（新增/删除行数+顶层符号）、编号信息。Agent 拿到 context 后自行调用 LLM 分析，生成分组+message 建议，人确认后通过 formalize 逐组执行。")
    def gitgo_suggest_formalize(project: str) -> dict:
        return build_suggest_result(project, "formalize")

    @mcp.tool(description="获取 trial triage 建议的 context。包含 incoming changes 列表、diff 统计、release 仓库上下文。Agent 分析后建议 accept/promote/discard，含 confidence 和 reason，人确认后通过 gitgo_trial_triage 逐项执行。")
    def gitgo_suggest_triage(project: str) -> dict:
        return build_suggest_result(project, "triage")

    @mcp.tool(description="获取变更语义摘要的 context。包含 workspace/trial/release 三段统计信息。Agent 基于 context 生成自然语言变更叙述，供状态展示用。")
    def gitgo_suggest_summary(project: str) -> dict:
        return build_suggest_result(project, "summary")

    # ── Formal Management Tools ──

    @mcp.tool(description="列出项目的所有 formal commits（含索引、状态、来源）。")
    def gitgo_formal_list(project: str) -> list[dict]:
        cfg, proj = get_project(project)
        if proj is None:
            return [{"error": "PROJECT_NOT_FOUND", "project": project}]
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_scan()
        session.step_load_commits()
        return [{"index": i, "prefix": fc.prefix, "number": fc.number,
                 "message": fc.message, "synced": fc.synced, "pushed": fc.pushed,
                 "is_incoming": fc.is_incoming, "sources_cleared": fc.sources_cleared,
                 "source_indices": sorted(fc.source_indices), "created_at": fc.created_at}
                for i, fc in enumerate(session.formal_commits)]

    @mcp.tool(description="删除指定索引的 formal commit。")
    def gitgo_formal_delete(project: str, index: int) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_scan()
        session.step_load_commits()
        ok = session.step_delete_formal(index)
        return {"deleted": ok, "index": index}

    @mcp.tool(description="编辑 formal commit 的 message 文本。")
    def gitgo_formal_edit_message(project: str, index: int, message: str) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_scan()
        session.step_load_commits()
        ok = session.step_edit_formal_message(index, message)
        return {"updated": ok, "index": index}

    @mcp.tool(description="重新编号 formal commit。更新 message 中的 [PREFIX-N] 标签。")
    def gitgo_formal_edit_number(project: str, index: int, new_number: int) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_scan()
        session.step_load_commits()
        ok = session.step_edit_formal_number(index, new_number)
        return {"updated": ok, "index": index, "new_number": new_number}

    @mcp.tool(description="Dissolve formal commit — 删除 formal commit 并恢复 workspace commit 可选状态。")
    def gitgo_formal_dissolve(project: str, index: int) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_scan()
        session.step_load_commits()
        ok = session.step_dissolve_formal(index)
        return {"dissolved": ok, "index": index}

    @mcp.tool(description="清除 formal commit 的 workspace 来源引用（解除 source_indices 关联）。")
    def gitgo_formal_clear_sources(project: str, index: int) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_scan()
        session.step_load_commits()
        ok = session.step_clear_formal_sources(index)
        return {"sources_cleared": ok, "index": index}

    # ── Gate Tool ──

    @mcp.tool(description="完成本轮开发。Gate A 检查：scan → formalize → sync（含 contract/drift/lesson 全部政策）。通过返回 passed=true + commit 编号，不通过返回 passed=false + 原因。Agent 必须收到 passed=true 才算本轮完成。")
    def gitgo_round_complete(project: str) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"passed": False, "error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        session.step_scan()
        session.step_load_commits()
        if not session.commits:
            return {"passed": True, "skipped": True, "reason": "no workspace commits"}
        fc = session.step_create_formal_commit()
        if not fc:
            return {"passed": False, "reason": "no commits to formalize — agent may need to git commit first"}
        ok = session.step_sync()
        if ok:
            return {"passed": True, "commit": f"[{fc.prefix}-{fc.number}]"}
        return {"passed": False, "reason": "Gate A blocked — check governance_drift and integrity_warning events for details",
                "commit": f"[{fc.prefix}-{fc.number}]"}
