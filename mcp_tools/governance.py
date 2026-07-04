"""MCP tools — governance, contract, history, releases, and remote operations."""

from mcp_tools.helpers import get_config, get_project


def register(mcp):
    """Register governance/contract/history/release tools on FastMCP instance."""

    @mcp.tool(description="获取 AI 建议质量度量：采纳率/修改率/拒绝率，按类型、commit type、模块切片。仅用 indices Jaccard 重叠度，不做 message 文本比较。")
    def gitgo_governance_quality(project: str) -> dict:
        from backend.core.governance import load_suggestion_pairs, compute_quality_metrics
        pairs = load_suggestion_pairs(project)
        return compute_quality_metrics(pairs)

    @mcp.tool(description="检测变更模式：共变模块（哪些目录总是一起变更）、commit 类型聚类（formalize 的类型分布和多源合并率）、trial 后续影响（accept 后触发 workspace 变更的概率）。")
    def gitgo_governance_patterns(project: str) -> dict:
        from backend.core.governance import build_patterns_report
        return build_patterns_report(project)

    @mcp.tool(description="构建语义变更图：formal commit 节点 + file_overlap（文件重叠 Jaccard≥0.3）/ same_push（同次推送）/ trial_source（来自 trial accept）三种边。")
    def gitgo_governance_graph(project: str) -> dict:
        from backend.core.governance import build_graph
        return build_graph(project)

    @mcp.tool(description="列出项目的发布历史：从所有 push 记录提取推送时间、包含的 commits 列表、以及 release note（如有）。发布按时间倒序排列。")
    def gitgo_governance_releases(project: str) -> dict:
        from backend.core.governance import list_releases
        return list_releases(project)

    @mcp.tool(description="为项目的最新一次 push 记录添加 release note（发布理由/说明）。用于 agent 发布后记录本次发布的背景和目的。message 为必填。")
    def gitgo_governance_release_note(project: str, message: str) -> dict:
        from backend.core.governance import add_release_note
        ok = add_release_note(project, message)
        return {"ok": ok, "message": message}

    @mcp.tool(description="获取项目最近的治理事件流（policy check / drift / snapshot / rejection / lesson），适合 Dashboard 实时展示。")
    def gitgo_governance_feed(project: str, limit: int = 20) -> list[dict]:
        from backend.core.history import HistoryManager
        from dataclasses import asdict
        entries = HistoryManager.load()
        gov_types = {
            "policy_check_result", "governance_drift", "governance_lesson",
            "workspace_state_snapshot", "rejection", "integrity_warning",
            "governance_synced", "governance_pushed", "governance_dissolved",
        }
        filtered = [e for e in entries if e.project_name == project and e.operation in gov_types]
        return [asdict(e) for e in filtered[-limit:]]

    @mcp.tool(description="查询项目操作历史（scan/formalize/sync/push/triage 等）。可按 project 和 operation 过滤。")
    def gitgo_history(project: str = "", op: str | None = None, limit: int = 20) -> list[dict]:
        from backend.core.history import HistoryManager
        from dataclasses import asdict
        entries = HistoryManager.load()
        if project:
            entries = [e for e in entries if e.project_name == project]
        if op:
            entries = [e for e in entries if e.operation == op]
        entries = entries[-limit:]
        return [asdict(e) for e in entries]

    @mcp.tool(description="获取远程仓库基本信息（GitHub/GitLab）。需要项目配置了 remote 和 token。")
    def gitgo_release_info(project: str) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.remote import create_connector
        node = proj.release
        if not node or not node.remote:
            return {"error": "NO_REMOTE_CONFIGURED", "project": project}
        connector = create_connector(node.remote)
        if connector is None:
            return {"error": "REMOTE_NOT_SUPPORTED", "kind": node.remote.kind}
        return connector.get_repo_info()

    @mcp.tool(description="在远程仓库创建 Release（GitHub/GitLab）。自动从最新 pushed formal commit 生成 tag。")
    def gitgo_release_create(project: str, tag: str = "", name: str = "", body: str = "") -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        ok, msg = session.step_create_release(tag=tag, name=name, body=body)
        return {"created": ok, "message": msg}

    @mcp.tool(description="列出远程仓库的 Issues（GitHub/GitLab）。需要项目配置了 remote 和 token（或环境变量 GITHUB_TOKEN/GITLAB_TOKEN）。")
    def gitgo_remote_issues(project: str, state: str = "open") -> list[dict]:
        cfg, proj = get_project(project)
        if proj is None:
            return [{"error": "PROJECT_NOT_FOUND", "project": project}]
        from backend.remote import create_connector
        node = proj.release
        if not node or not node.remote:
            return [{"error": "NO_REMOTE_CONFIGURED", "project": project}]
        connector = create_connector(node.remote)
        if connector is None:
            return [{"error": "REMOTE_NOT_SUPPORTED", "kind": node.remote.kind}]
        return connector.list_issues(state=state)

    # ── Contract Tools ──

    @mcp.tool(description="查看项目合约：技术栈、已确认功能、架构约束。可选附漂移检测结果。")
    def gitgo_contract_show(project: str) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        from backend.core.contract import ContractManager, detect_drift
        from pathlib import Path
        session = SyncSession(proj, cfg)
        ws = Path(session.workspace_path)
        contract = ContractManager.load(ws)
        if contract is None:
            return {"contract": None}
        result = {
            "project": contract.project, "updated": contract.updated,
            "tech_stack": contract.tech_stack,
            "decided_features": [
                {"name": f.name, "location": f.location,
                 "signature": f.signature, "confirmed_count": f.confirmed_count}
                for f in contract.decided_features
            ],
            "architecture_constraints": contract.architecture_constraints,
        }
        entry_files = [e.rel_path for e in (session.entries or []) if e.status != "same"]
        if entry_files:
            result["drift_alerts"] = detect_drift(ws, entry_files, contract)
        return result

    @mcp.tool(description="更新项目合约：新增或确认 decided feature。")
    def gitgo_contract_update(project: str, feature_name: str, location: str = "", signature: str = "") -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        from backend.core.contract import ContractManager
        from pathlib import Path
        session = SyncSession(proj, cfg)
        ws = Path(session.workspace_path)
        contract = ContractManager.update_feature(
            ws, proj.name, feature_name=feature_name, location=location, signature=signature)
        return {"updated": feature_name, "confirmed_count": next(
            (f.confirmed_count for f in contract.decided_features if f.name == feature_name), 0)}
