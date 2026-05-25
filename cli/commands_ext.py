"""CLI extensions — governance, export, template, formal, memory, contract."""
from __future__ import annotations

import json
import sys

from backend.core.config import Config
from .commands import _init_session
def _cmd_governance(cfg: Config, project_name: str, governance_type: str,
                    message: str = "", json_output: bool = False):
    """--mode governance: 治理度量与自省。

    --governance-type quality: 建议采纳率/修改率/拒绝率
    """
    if governance_type == "quality":
        from backend.core.governance import load_suggestion_pairs, compute_quality_metrics

        pairs = load_suggestion_pairs(project_name)
        metrics = compute_quality_metrics(pairs)
        if json_output:
            import json
            print(json.dumps(metrics, indent=2, ensure_ascii=False))
        else:
            _print_quality(metrics)

    elif governance_type == "patterns":
        from backend.core.governance import build_patterns_report

        report = build_patterns_report(project_name)
        if json_output:
            import json
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            _print_patterns(report)

    elif governance_type == "graph":
        from backend.core.governance import build_graph

        graph = build_graph(project_name)
        if json_output:
            import json
            print(json.dumps(graph, indent=2, ensure_ascii=False))
        else:
            _print_graph(graph)

    elif governance_type == "releases":
        from backend.core.governance import list_releases

        data = list_releases(project_name)
        if json_output:
            import json
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            _print_releases(data)

    elif governance_type == "release-note":
        from backend.core.governance import add_release_note

        if not message:
            if json_output:
                import json
                print(json.dumps({"error": "MISSING_MESSAGE",
                                  "detail": "release-note requires --message"}))
            else:
                print("错误: release-note 需要 --message 参数")
            sys.exit(1)

        ok = add_release_note(project_name, message)
        if json_output:
            import json
            print(json.dumps({"ok": ok, "message": message}))
        else:
            if ok:
                print(f"已为 {project_name} 最新发布记录 release note")
            else:
                print(f"未找到 {project_name} 的 push 记录")

    else:
        if json_output:
            import json
            print(json.dumps({"error": "UNKNOWN_GOVERNANCE_TYPE",
                              "governance_type": governance_type}))
        else:
            print(f"错误: 未知 governance 类型: {governance_type}")
            print("支持: quality, patterns, graph, releases, release-note")
        sys.exit(1)


def _print_quality(metrics: dict):
    """人类可读的 quality 输出。"""
    if metrics["suggestion_count"] == 0:
        print("暂无 AI 建议记录")
        return

    print(f"\nAI 建议质量度量 ({metrics['suggestion_count']} 条):\n")

    for stype, data in metrics.get("by_type", {}).items():
        type_name = {"formalize": "分组建议", "triage": "审查建议"}.get(stype, stype)
        print(f"  [{type_name}] 共 {data['total']} 条")
        print(f"    采纳: {data['accepted']} ({data['acceptance_rate']:.0%})")
        print(f"    修改: {data['modified']} ({data['modification_rate']:.0%})")
        print(f"    拒绝: {data['rejected']} ({data['rejection_rate']:.0%})")
        if data.get("avg_index_jaccard") is not None:
            print(f"    平均 Jaccard: {data['avg_index_jaccard']:.3f}")
        print()

    if metrics.get("by_commit_type"):
        print("  按 Commit 类型:")
        for ct, d in sorted(metrics["by_commit_type"].items()):
            print(f"    {ct}: {d['total']} 条, 采纳率 {d['acceptance_rate']:.0%}")
        print()

    if metrics.get("by_module"):
        print("  按模块:")
        for mod, d in sorted(metrics["by_module"].items()):
            print(f"    {mod}/: {d['total']} 条, 采纳率 {d['acceptance_rate']:.0%}")
        print()


def _print_patterns(report: dict):
    """人类可读的 patterns 输出。"""
    co = report.get("co_changing_modules", [])
    tc = report.get("commit_type_clusters", [])
    ti = report.get("trial_impact", {})

    if not co and not tc:
        print("暂无足够数据检测变更模式")
        return

    if co:
        print(f"\n共变模块 (Top {len(co)}):\n")
        for p in co:
            m = p["modules"]
            print(f"  {m[0]} ⇄ {m[1]}  "
                  f"({p['co_occurrence']}/{p['total_formal']} formal commits)")

    if tc:
        print(f"\nCommit 类型聚类:\n")
        for c in tc:
            print(f"  {c['type']}: {c['count']} 次 formalize, "
                  f"平均 {c['avg_sources']} 个源 commit, "
                  f"多源合并率 {c['multi_source_ratio']:.0%}")

    if ti and ti.get("total_accepted", 0) > 0:
        print(f"\nTrial 后续影响:\n"
              f"  总 accept: {ti['total_accepted']}\n"
              f"  触发 workspace 变更: {ti['triggered_workspace_change']}\n"
              f"  触发率: {ti['avg_trigger_rate']:.0%}")
    print()


def _print_graph(graph: dict):
    """人类可读的 graph 输出。"""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    if not nodes:
        print("暂无 formal commit 记录")
        return

    print(f"\n语义变更图 ({len(nodes)} nodes, {len(edges)} edges):\n")

    formal_nodes = [n for n in nodes if n["type"] == "formal"]
    incoming_nodes = [n for n in nodes if n["type"] == "incoming"]
    print(f"  Formal commits: {len(formal_nodes)}")
    print(f"  Incoming (trial accept): {len(incoming_nodes)}")

    if formal_nodes:
        print("\n  Formal nodes:")
        for n in formal_nodes:
            files = n.get("files_changed", [])
            print(f"    {n['id']}  ({n['source_commits']} sources, "
                  f"{len(files)} files)")

    if incoming_nodes:
        print("\n  Incoming nodes:")
        for n in incoming_nodes:
            msg = n.get("message", "")[:60]
            print(f"    {n['id']}  {msg}")

    if edges:
        print(f"\n  Edges:")
        for e in edges:
            if e["type"] == "file_overlap":
                print(f"    {e['from']} → {e['to']}  "
                      f"file_overlap ({e['overlap_ratio']:.2f}) "
                      f"[{', '.join(e.get('overlap_files', []))}]")
            elif e["type"] == "same_push":
                print(f"    {e['from']} → {e['to']}  "
                      f"same_push ({e.get('pushed_at', '')[:19]})")
            elif e["type"] == "trial_source":
                print(f"    {e['from']} → {e['to']}  trial_source")
    print()


def _print_releases(data: dict):
    """人类可读的 releases 输出。"""
    releases = data.get("releases", [])
    if not releases:
        print("暂无发布记录")
        return

    print(f"\n发布历史 ({len(releases)} 次):\n")
    for i, r in enumerate(releases):
        pushed = r["pushed_at"][:19]
        commits = r.get("commits", [])
        reason = r.get("reason")
        print(f"  [{i+1}] {pushed}  ({len(commits)} commits)")
        if commits:
            for c in commits:
                print(f"      {c}")
        if reason:
            print(f"      理由: {reason}")
        print()


def _cmd_export(cfg: Config, project_name: str, export_type: str,
                minimal: bool = False, include_identity: bool = False,
                json_output: bool = False):
    """--mode export: 导出治理状态。

    --export-type state-bundle: 完整状态快照
    """
    from backend.core.governance import collect_state_bundle

    if export_type == "state-bundle":
        session = _init_session(cfg, project_name, json_output=json_output)
        bundle = collect_state_bundle(session, minimal=minimal,
                                      include_identity=include_identity)
        if json_output:
            import json
            print(json.dumps(bundle, indent=2, ensure_ascii=False))
        else:
            print(f"State Bundle: {project_name}")
            print(f"  Protocol: {bundle['gitgo_protocol_version']}")
            print(f"  Exported: {bundle['exported_at'][:19]}")
            state = bundle["current_state"]
            print(f"  Stage: {state.get('stage', '?')}")
            ws = state.get("workspace", {})
            cm = state.get("commits", {})
            print(f"  Workspace: {ws.get('entries_changed', 0)}/{ws.get('entries_total', 0)} changed")
            print(f"  Commits: {cm.get('formal_total', 0)} formal, "
                  f"{cm.get('formal_synced', 0)} synced, {cm.get('formal_pushed', 0)} pushed")
            gs = bundle.get("governance_summary", {})
            q = gs.get("quality", {})
            print(f"  Suggestions: {q.get('suggestion_count', 0)}")
            hist = bundle.get("recent_history", [])
            sugg = bundle.get("recent_suggestions", [])
            if hist:
                print(f"  History: {len(hist)} entries")
            if sugg:
                print(f"  Suggestions: {len(sugg)} entries")
    else:
        if json_output:
            import json
            print(json.dumps({"error": "UNKNOWN_EXPORT_TYPE", "export_type": export_type}))
        else:
            print(f"错误: 未知 export 类型: {export_type}")
            print("支持: state-bundle")
        sys.exit(1)


def _cmd_template(cfg: Config, action: str,
                  name: str | None = None,
                  description: str = "",
                  header_format: str = "",
                  body_format: str = "",
                  prefix_override: str | None = None,
                  json_output: bool = False):
    """--mode template: 管理 commit message 模板"""
    from backend.core.template_manager import TemplateManager, CommitTemplate

    templates = TemplateManager.load()

    if action == "list":
        if json_output:
            result = [
                {
                    "name": t.name, "description": t.description,
                    "header_format": t.header_format, "body_format": t.body_format,
                    "prefix_override": t.prefix_override,
                }
                for t in templates
            ]
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            for t in templates:
                print(f"  [{t.name}] {t.description}")
                print(f"    header: {t.header_format}")
                if t.prefix_override:
                    print(f"    prefix_override: {t.prefix_override}")

    elif action == "add":
        if not name:
            print("错误: --template-name 不能为空")
            sys.exit(1)
        if any(t.name == name for t in templates):
            print(f"错误: 模板 '{name}' 已存在")
            sys.exit(1)
        default_tpl = TemplateManager.get_default()
        new_tpl = CommitTemplate(
            name=name,
            description=description,
            header_format=header_format or default_tpl.header_format,
            body_format=body_format or default_tpl.body_format,
            prefix_override=prefix_override,
        )
        templates.append(new_tpl)
        path = TemplateManager.save(templates)
        if json_output:
            print(json.dumps({"result": "ok", "name": name, "path": str(path)}))
        else:
            print(f"[OK] 模板 '{name}' 已保存 → {path}")

    elif action == "edit":
        if not name:
            print("错误: --template-name 不能为空")
            sys.exit(1)
        idx = next((i for i, t in enumerate(templates) if t.name == name), None)
        if idx is None:
            print(f"错误: 未找到模板 '{name}'")
            sys.exit(1)
        t = templates[idx]
        if description:
            t.description = description
        if header_format:
            t.header_format = header_format
        if body_format:
            t.body_format = body_format
        if prefix_override is not None:
            t.prefix_override = prefix_override
        path = TemplateManager.save(templates)
        if json_output:
            print(json.dumps({"result": "ok", "name": name, "path": str(path)}))
        else:
            print(f"[OK] 模板 '{name}' 已更新 → {path}")

    elif action == "delete":
        if not name:
            print("错误: --template-name 不能为空")
            sys.exit(1)
        if name == "default":
            print("错误: 不能删除 'default' 模板")
            sys.exit(1)
        idx = next((i for i, t in enumerate(templates) if t.name == name), None)
        if idx is None:
            print(f"错误: 未找到模板 '{name}'")
            sys.exit(1)
        templates.pop(idx)
        path = TemplateManager.save(templates)
        if json_output:
            print(json.dumps({"result": "ok", "name": name, "path": str(path)}))
        else:
            print(f"[OK] 模板 '{name}' 已删除")


# ── Formal 管理 ────────────────────────────────────────────

def _cmd_formal(cfg: Config, project_name: str, action: str,
                formal_index: int | None = None,
                message: str | None = None,
                new_number: int | None = None,
                json_output: bool = False):
    """--mode formal: 管理 formal commits（list/delete/edit/dissolve/clear-sources）"""
    session = _init_session(cfg, project_name, json_output=json_output)

    if action == "list":
        if not session.formal_commits:
            if json_output:
                print(json.dumps([]))
            else:
                print("（无 formal commits）")
            return
        if json_output:
            result = [
                {
                    "index": i,
                    "prefix": fc.prefix,
                    "number": fc.number,
                    "message": fc.message,
                    "synced": fc.synced,
                    "pushed": fc.pushed,
                    "is_incoming": fc.is_incoming,
                    "sources_cleared": fc.sources_cleared,
                    "source_indices": sorted(fc.source_indices),
                    "created_at": fc.created_at,
                }
                for i, fc in enumerate(session.formal_commits)
            ]
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            for i, fc in enumerate(session.formal_commits):
                status = []
                if fc.synced:
                    status.append("synced")
                if fc.pushed:
                    status.append("pushed")
                if fc.is_incoming:
                    status.append("incoming")
                st = ",".join(status) if status else "draft"
                print(f"  [{i}] [{fc.prefix}-{fc.number}] ({st})")
                print(f"      {fc.message.split(chr(10))[0][:80]}")
        return

    # 写操作需要索引
    if formal_index is None:
        msg = f"错误: --formal-index 不能为空（action={action}）"
        if json_output:
            print(json.dumps({"result": "fail", "error": msg}))
        else:
            print(msg)
        sys.exit(1)

    ok = False
    if action == "delete":
        ok = session.step_delete_formal(formal_index)
    elif action == "edit-message":
        if not message:
            print("错误: --message 不能为空（action=edit-message）")
            sys.exit(1)
        ok = session.step_edit_formal_message(formal_index, message)
    elif action == "edit-number":
        if new_number is None:
            print("错误: --new-number 不能为空（action=edit-number）")
            sys.exit(1)
        ok = session.step_edit_formal_number(formal_index, new_number)
    elif action == "dissolve":
        ok = session.step_dissolve_formal(formal_index)
    elif action == "clear-sources":
        ok = session.step_clear_formal_sources(formal_index)

    if json_output:
        print(json.dumps({"result": "ok" if ok else "fail", "action": action,
                          "index": formal_index}))
    else:
        print(f"[{'OK' if ok else 'FAIL'}] {action} formal[{formal_index}]")


# ── Memory 快照管理 ───────────────────────────────────────

def _cmd_memory(cfg: Config, project_name: str, action: str,
                snapshot_ts: str | None = None,
                json_output: bool = False):
    """--mode memory: 管理工具记忆快照（snapshot/restore/list）"""
    session = _init_session(cfg, project_name, json_output=json_output)
    bp = session.backup_path
    if not bp:
        if json_output:
            print(json.dumps({"error": "NO_BACKUP_CONFIGURED"}))
        else:
            print("错误: 未配置 release 仓库")
        sys.exit(1)

    from backend.core.identity.snapshot import (
        snapshot_tool_memories, restore_tool_memories, list_memory_snapshots,
    )

    if action == "list":
        snapshots = list_memory_snapshots(bp)
        if json_output:
            print(json.dumps(snapshots, indent=2, ensure_ascii=False))
        else:
            if not snapshots:
                print("（无可用快照）")
            else:
                for s in snapshots:
                    print(f"  [{s['source']}] {s['timestamp']} "
                          f"({'dir' if s['is_dir'] else 'file'})")

    elif action == "snapshot":
        result = snapshot_tool_memories(
            session.workspace_path, bp, session.project,
        )
        if json_output:
            print(json.dumps({"result": "ok", **result}))
        else:
            print(f"[OK] 已快照 {len(result['snapped'])} 个记忆源 → {result['dest']}")

    elif action == "restore":
        result = restore_tool_memories(
            bp, session.workspace_path, snapshot_timestamp=snapshot_ts,
        )
        if json_output:
            print(json.dumps(result))
        else:
            if result.get("error"):
                print(f"[FAIL] {result['error']}")
            else:
                print(f"[OK] 已恢复 {len(result.get('restored', []))} 个记忆源")


# ── Contract 管理 ────────────────────────────────────────

def _cmd_contract(cfg: Config, project_name: str,
                  json_output: bool = False):
    """--mode contract: 查看/刷新项目合约"""
    session = _init_session(cfg, project_name, json_output=json_output)

    from backend.core.contract import ContractManager, detect_drift
    contract = ContractManager.load(session.workspace_path)

    if contract is None:
        if json_output:
            print(json.dumps({"contract": None, "message": "no contract yet — sync 后自动生成"}))
        else:
            print("（尚无合约 — sync 成功后自动生成）")
        return

    if json_output:
        result = {
            "project": contract.project,
            "updated": contract.updated,
            "tech_stack": contract.tech_stack,
            "decided_features": [
                {
                    "name": f.name, "location": f.location,
                    "signature": f.signature,
                    "confirmed_count": f.confirmed_count,
                    "introduced": f.introduced, "last_modified": f.last_modified,
                }
                for f in contract.decided_features
            ],
            "architecture_constraints": contract.architecture_constraints,
        }
        # 同时运行漂移检测
        entries = session.entries or []
        changed_files = [e.rel_path for e in entries if e.status != "same"]
        if changed_files:
            result["drift_alerts"] = detect_drift(
                session.workspace_path, changed_files, contract,
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"合约: {contract.project} (更新于 {contract.updated})")
        print(f"技术栈: {', '.join(contract.tech_stack) if contract.tech_stack else '(未声明)'}")
        print(f"已确认功能 ({len(contract.decided_features)}):")
        for f in contract.decided_features:
            loc = f" → {f.location}" if f.location else ""
            print(f"  [{f.confirmed_count}x] {f.name}{loc}")
        if contract.architecture_constraints:
            print(f"架构约束 ({len(contract.architecture_constraints)}):")
            for c in contract.architecture_constraints:
                print(f"  - {c}")


# ── Lesson 管理 ──────────────────────────────────────────

def _cmd_lesson(cfg: Config, project_name: str,
                json_output: bool = False):
    """--mode lesson: 查看/管理知识教训"""
    session = _init_session(cfg, project_name, json_output=json_output)
    ws = session.workspace_path

    from backend.core.knowledge.lesson import LessonManager

    abstract = LessonManager.load_abstract(ws)
    instances = LessonManager.load_instance(ws, project_name)
    pending = LessonManager.load_pending(ws, project_name)

    if json_output:
        result = {
            "abstract": [l.to_dict() for l in abstract],
            "instances": [l.to_dict() for l in instances],
            "pending": [l.to_dict() for l in pending],
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if pending:
            print(f"\n待确认 ({len(pending)}):")
            for l in pending:
                print(f"  [{l.id}] {l.trigger[:80]}")
        if instances:
            print(f"\n实例知识 ({len(instances)}):")
            for l in instances:
                print(f"  [{l.severity}] {l.trigger[:80]}")
                print(f"    → {l.rule[:80]}")
        if abstract:
            print(f"\n抽象知识 ({len(abstract)}):")
            for l in abstract:
                print(f"  [{l.tech_stack}] [{l.severity}] {l.trigger[:80]}")
        if not pending and not instances and not abstract:
            print("（暂无知识记录 — sync 成功后自动收割）")
