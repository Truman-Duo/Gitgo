"""CLI verb implementations — 每个 --mode 对应一个函数。

所有函数均为 headless，不依赖 Qt / Rich。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from backend.core.config import Config, ConfigManager
from backend.core.sync_session import SyncSession


# ── 辅助 ────────────────────────────────────────────────────

def _stream_progress(op: str):
    """返回 line-delimited JSON 进度回调 (c,t,m) -> print."""
    import sys
    def _fn(c: int, t: int, m: str):
        print(json.dumps({"event": "progress", "op": op, "current": c, "total": t, "message": m}), flush=True)
    return _fn

def _init_session(cfg: Config, project_name: str, json_output: bool = False,
                  with_scan: bool = False):
    """初始化 SyncSession — 所有 CLI verb 共用。找不到项目时 exit(1)。"""
    matched = [p for p in cfg.projects if p.name == project_name]
    if not matched:
        if json_output:
            print(json.dumps({"error": "PROJECT_NOT_FOUND", "name": project_name}))
        else:
            print(f"错误: 未找到项目「{project_name}」")
            print(f"可用项目: {', '.join(p.name for p in cfg.projects)}")
        sys.exit(1)

    session = SyncSession(matched[0], cfg)
    if with_scan:
        session.step_scan()
        session.step_load_commits()
    session.step_check_trial()
    return session


# ── CLI verbs ───────────────────────────────────────────────

def _cmd_list(cfg: Config):
    """--mode list: 列出所有项目"""
    if not cfg.projects:
        print("未配置任何项目")
        return
    print(f"共 {len(cfg.projects)} 个项目:\n")
    for i, p in enumerate(cfg.projects, 1):
        ws = p.workspace_path or "(使用当前目录)"
        bk = p.backup_path or "未设置"
        base = p.sync_base[:12] if p.sync_base else "无"
        prefix = p.commit_format.get("prefix", "ANBM")
        print(f"  [{i}] {p.name}")
        print(f"      工作区: {ws}")
        print(f"      备份库: {bk}")
        print(f"      Commit前缀: {prefix}  Sync基点: {base}")
        print()


def _cmd_status(cfg: Config, project_name: str, json_output: bool = False,
                 raw: bool = False, semantic_only: bool = False):
    """--mode status --project NAME: 查询项目状态"""
    matched = [p for p in cfg.projects if p.name == project_name]
    if not matched:
        if json_output:
            print(json.dumps({"error": "PROJECT_NOT_FOUND", "name": project_name}))
        else:
            print(f"错误: 未找到项目「{project_name}」")
            print(f"可用项目: {', '.join(p.name for p in cfg.projects)}")
        sys.exit(1)

    session = SyncSession(matched[0], cfg)
    session.step_check_trial()

    if semantic_only:
        d = session.status_dict(semantic=True)
        print(json.dumps(d.get("semantic", {}), indent=2, ensure_ascii=False))
    elif json_output:
        print(json.dumps(session.status_dict(semantic=not raw), indent=2, ensure_ascii=False))
    else:
        d = session.status_dict()
        print(f"项目: {d['project']}")
        print(f"阶段: {d['stage']}")
        print(f"工作区: {d['workspace']['path']}")
        print(f"  文件总数: {d['workspace']['entries_total']}")
        print(f"  变更文件: {d['workspace']['entries_changed']}")
        print(f"Workspace commit: {d['commits']['workspace_total']}")
        print(f"Formal commit: {d['commits']['formal_total']}")
        print(f"  已同步: {d['commits']['formal_synced']}")
        print(f"  已推送: {d['commits']['formal_pushed']}")
        trial_info = d['trial']
        if trial_info['configured']:
            print(f"Trial: {trial_info['total']} incoming ({trial_info['pending']} pending)")
        else:
            print("Trial: 未配置")
        # 语义摘要
        if "semantic" in d:
            sem = d["semantic"]
            print(f"\n推荐操作: {sem['suggested_next_action']}")
            print(f"操作队列: {' → '.join(sem['action_queue']) if sem['action_queue'] else '无'}")
            if sem['blocked_reason']:
                print(f"阻塞原因: {sem['blocked_reason']}")


def _cmd_sync(cfg: Config, project_name: str, message: str | None, json_output: bool = False,
              stream: bool = False):
    """--mode sync --project NAME: 通过 SyncSession 同步（scan → formalize → sync）"""
    matched = [p for p in cfg.projects if p.name == project_name]
    if not matched:
        if json_output:
            print(json.dumps({"error": "PROJECT_NOT_FOUND", "name": project_name}))
        else:
            print(f"错误: 未找到项目「{project_name}」")
            print(f"可用项目: {', '.join(p.name for p in cfg.projects)}")
        sys.exit(1)

    session = SyncSession(matched[0], cfg)
    if stream:
        session.on_log = lambda m: print(json.dumps({"event": "log", "message": m}), flush=True)
        session.on_progress = _stream_progress("sync")
        print(json.dumps({"event": "operation_started", "op": "sync", "project": project_name}))
    else:
        session.on_log = print
        session.on_progress = lambda c, t, m: print(f"  [{c}/{t}] {m}") if m else None

    if not stream and not json_output:
        print(f"扫描工作区: {session.workspace_path}")
        print(f"备份仓库: {session.backup_path}")

    success = session.run_full_workflow(commit_message=message, skip_push=True)
    if success:
        if stream:
            print(json.dumps({"event": "operation_complete", "op": "sync", "status": "success"}))
        elif json_output:
            print(json.dumps({"result": "ok", "project": project_name}))
    else:
        if stream:
            print(json.dumps({"event": "operation_complete", "op": "sync", "status": "failed"}))
        elif json_output:
            print(json.dumps({"result": "failed", "project": project_name}))
        else:
            print(f"\n[FAIL] 同步失败")
        sys.exit(1)


def _cmd_daemon(cfg: Config, project_name: str, commit_message: str | None,
                 skip_push: bool = False, force_on_warning: bool = False,
                 json_output: bool = False, stream: bool = False,
                 daemon_action: str = "run",
                 trial_interval: float = 300.0, debounce_sec: float = 2.0):
    """--mode daemon: 守护进程（start/stop/status）或一次性全流程（run）"""
    matched = [p for p in cfg.projects if p.name == project_name]
    if not matched:
        if json_output:
            print(json.dumps({"error": "PROJECT_NOT_FOUND", "name": project_name}))
        else:
            print(f"错误: 未找到项目「{project_name}」")
            print(f"可用项目: {', '.join(p.name for p in cfg.projects)}")
        sys.exit(1)

    project = matched[0]

    if daemon_action == "start":
        # Persistent daemon mode
        from backend.core.daemon import run_daemon
        run_daemon(cfg, project, trial_interval=trial_interval,
                   debounce_sec=debounce_sec)
        return

    if daemon_action == "stop":
        from backend.core.daemon import _pid_file_path
        import os
        pid_path = _pid_file_path(project)
        if not pid_path.exists():
            if json_output:
                print(json.dumps({"result": "not_running", "project": project_name}))
            else:
                print("Daemon 未在运行")
            sys.exit(1)
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 15)  # SIGTERM
            if json_output:
                print(json.dumps({"result": "stopped", "project": project_name, "pid": pid}))
            else:
                print(f"已发送停止信号 (PID {pid})")
        except (OSError, ProcessLookupError):
            pid_path.unlink(missing_ok=True)
            if json_output:
                print(json.dumps({"result": "stale_cleaned", "project": project_name}))
            else:
                print("PID 文件已过期，已清理")
        return

    if daemon_action == "status":
        from backend.core.daemon import _pid_file_path
        import os
        pid_path = _pid_file_path(project)
        running = False
        pid = None
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                os.kill(pid, 0)
                running = True
            except (OSError, ProcessLookupError, ValueError):
                pass
        if json_output:
            print(json.dumps({"running": running, "pid": pid, "project": project_name}))
        else:
            print(f"Daemon: {'运行中' if running else '未运行'}" +
                  (f" (PID {pid})" if pid else ""))
        return

    # Legacy one-shot mode (daemon_action == "run")
    session = SyncSession(project, cfg)
    if stream:
        session.on_log = lambda m: print(json.dumps({"event": "log", "message": m}), flush=True)
        session.on_progress = _stream_progress("daemon")
        print(json.dumps({"event": "daemon_started", "project": project.name}))
    else:
        session.on_log = print
        session.on_progress = lambda c, t, m: print(f"  [{c}/{t}] {m}") if m else None

    if not stream:
        print(f"Daemon 模式: {project.name}")
        print(f"  工作区: {session.workspace_path}")
        print(f"  备份库: {session.backup_path}")
        print()

    success = session.run_full_workflow(
        commit_message=commit_message,
        skip_push=skip_push,
        force_on_warning=force_on_warning,
    )

    if stream:
        print(json.dumps({"event": "daemon_stopped", "project": project_name,
                          "status": "ok" if success else "fail"}))
        if not success:
            sys.exit(1)
    elif success:
        if json_output:
            print(json.dumps({"result": "ok", "project": project_name}))
        else:
            print("\n[OK] 全流程完成")
    else:
        if json_output:
            print(json.dumps({"result": "fail", "project": project_name}))
        else:
            print("\n[FAIL] 流程中断")
        sys.exit(1)


def _cmd_trial(cfg: Config, project_name: str, action: str,
               index: int | None = None, json_output: bool = False):
    """--mode trial: 三叉决策操作"""
    from backend.models import TrialAction

    session = _init_session(cfg, project_name, json_output=json_output)

    if action == "list":
        if not session.incoming_changes:
            if json_output:
                print(json.dumps([]))
            else:
                print("无 Trial incoming changes")
            return
        if json_output:
            result = []
            for i, c in enumerate(session.incoming_changes):
                result.append({
                    "index": i,
                    "hash": c.hash,
                    "message": c.message,
                    "author": c.author,
                    "date": c.date,
                    "triage": c.triage.value,
                })
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"共 {len(session.incoming_changes)} 个 Trial incoming changes:\n")
            for i, c in enumerate(session.incoming_changes):
                tag = {"ACCEPTED": "[✓]", "PROMOTED": "[↑]", "DISCARDED": "[✗]"}
                status = tag.get(c.triage.name, "[ ]")
                print(f"  [{i}] {status} {c.hash[:12]}  {c.message.split(chr(10))[0][:60]}")
                print(f"      author: {c.author}  date: {c.date}")
            print()

    elif action in ("accept", "promote", "discard"):
        if index is None:
            msg = "错误: --index N 参数必填" if not json_output else \
                  json.dumps({"error": "INDEX_REQUIRED", "message": "--index N required"})
            print(msg)
            sys.exit(1)
        ok = session.step_triage_incoming(index, action)
        if json_output:
            print(json.dumps({"result": "ok" if ok else "fail", "action": action, "index": index}))
        else:
            action_cn = {"accept": "Accept", "promote": "Promote", "discard": "Discard"}
            print(f"[{'OK' if ok else 'FAIL'}] {action_cn.get(action, action)} 完成")
        if not ok:
            sys.exit(1)
    else:
        print(f"错误: 未知 trial 操作: {action}" if not json_output else
              json.dumps({"error": "UNKNOWN_ACTION", "action": action}))
        sys.exit(1)


def _cmd_formalize(cfg: Config, project_name: str, indices: str | None = None,
                   message: str | None = None, template_name: str | None = None,
                   json_output: bool = False):
    """--mode formalize: 从 workspace commit 创建 formal commit"""
    session = _init_session(cfg, project_name, json_output=json_output,
                            with_scan=True)

    selected = None
    if indices:
        selected = set(int(i.strip()) for i in indices.split(",") if i.strip())

    fc = session.step_create_formal_commit(selected_indices=selected, message=message,
                                           template_name=template_name)
    if fc is None:
        if json_output:
            print(json.dumps({"result": "fail", "reason": "no_commit_created"}))
        else:
            print("[FAIL] 未创建 formal commit（无选中 commit 或用户取消）")
        sys.exit(1)

    if json_output:
        print(json.dumps({
            "result": "ok",
            "commit": {
                "message": fc.message,
                "number": fc.number,
                "prefix": fc.prefix,
                "source_indices": list(fc.source_indices),
                "created_at": fc.created_at,
            }
        }, indent=2, ensure_ascii=False))
    else:
        print(f"[OK] Formal commit 已创建: [{fc.prefix}-{fc.number}]")
        print(f"  message: {fc.message.split(chr(10))[0]}")
        print(f"  source indices: {sorted(fc.source_indices)}")


def _cmd_scan(cfg: Config, project_name: str, json_output: bool = False,
              stream: bool = False):
    """--mode scan: 仅扫描变更文件"""
    session = _init_session(cfg, project_name, json_output=json_output,
                            with_scan=False)
    if stream:
        session.on_progress = _stream_progress("scan")
        print(json.dumps({"event": "operation_started", "op": "scan"}))
    session.step_scan()

    if stream:
        entries = [{"path": e.path, "status": e.status, "selected": e.selected}
                   for e in session.entries]
        print(json.dumps({"event": "operation_complete", "op": "scan",
                          "entries": entries, "total": len(session.entries)}))
    elif json_output:
        entries = []
        for e in session.entries:
            entries.append({
                "path": e.path,
                "status": e.status,
                "selected": e.selected,
            })
        print(json.dumps({"result": "ok", "entries": entries}, indent=2, ensure_ascii=False))
    else:
        print(f"扫描完成: {len(session.entries)} 个文件")
        changed = [e for e in session.entries if e.status != "same"]
        if not changed:
            print("  无变更")
            return
        for e in changed:
            print(f"  [{e.status}] {e.path}")


def _cmd_push(cfg: Config, project_name: str, skip_security: bool = False,
              strip_authorship: bool = False, aggressive: bool = False,
              json_output: bool = False, stream: bool = False):
    """--mode push: 推送已 synced 的 formal commit"""
    session = _init_session(cfg, project_name, json_output=json_output)

    # ── Authorship 过滤 ──
    if strip_authorship:
        from backend.core.authorship import apply_authorship_filter
        stats = apply_authorship_filter(session, aggressive=aggressive)
        if json_output:
            print(json.dumps({"event": "authorship_filter", **stats}))
        elif stream:
            print(json.dumps({"event": "log",
                              "message": f"authorship filter: {stats}"}))

    if stream:
        session.on_progress = _stream_progress("push")
        print(json.dumps({"event": "operation_started", "op": "push"}))

    ready = [fc for fc in session.formal_commits if fc.synced and not fc.pushed]
    if not ready:
        if stream:
            print(json.dumps({"event": "operation_complete", "op": "push", "status": "skipped",
                              "reason": "no synced commits"}))
        elif json_output:
            print(json.dumps({"error": "NO_SYNCED_COMMITS",
                              "message": "no synced formal commits to push — must sync first"}))
        else:
            print("错误: 没有已同步待推送的 formal commit — 请先 sync")
        sys.exit(1)

    # ── Drift Detection ──
    from backend.core.contract import ContractManager, detect_drift
    contract = ContractManager.load(session.workspace_path)
    if contract:
        changed = set()
        for fc in ready:
            changed.update(fc.source_indices)
        changed_files = [
            c.rel_path for c in session.commits
            if session.commits.index(c) in changed
        ] if session.commits else []
        drift_alerts = detect_drift(session.workspace_path, list(changed_files), contract)
        if drift_alerts:
            if json_output:
                print(json.dumps({"event": "drift_detected", "alerts": drift_alerts}))
            elif stream:
                print(json.dumps({"event": "log",
                                  "message": f"[DRIFT] {len(drift_alerts)} alerts"}))
                for a in drift_alerts:
                    print(json.dumps({"event": "log",
                                      "message": f"  [{a['rule']}] {a['message']}"}))
            else:
                print(f"\n[DRIFT] 检测到 {len(drift_alerts)} 项合约偏差:")
                for a in drift_alerts:
                    print(f"  [{a['rule'].upper()}] {a['message'][:120]}")
                print("  (使用 --force 跳过，或先修复偏差)\n")

    success, warnings = session.step_push(skip_scan=skip_security)
    if stream:
        print(json.dumps({"event": "operation_complete", "op": "push",
                          "status": "success" if success else "fail",
                          "warnings": warnings}))
    elif json_output:
        print(json.dumps({
            "result": "ok" if success else "fail",
            "warnings": warnings,
        }, indent=2, ensure_ascii=False))
    else:
        if success:
            print("[OK] Push 成功")
        else:
            if warnings:
                print(f"[WARN] 安全检查发现 {len(warnings)} 项敏感信息 — 推送已取消")
            else:
                print("[FAIL] Push 失败")
        if warnings:
            for w in warnings[:5]:
                print(f"  - {w.get('pattern', '?')}: {w.get('match', '?')[:60]}")
    if not success:
        sys.exit(1)


def _cmd_session(cfg: Config, project_name: str, action: str,
                 json_output: bool = False):
    """--mode session: 管理 .gitgo/session.json"""
    matched = [p for p in cfg.projects if p.name == project_name]
    if not matched:
        if json_output:
            print(json.dumps({"error": "PROJECT_NOT_FOUND", "name": project_name}))
        else:
            print(f"错误: 未找到项目「{project_name}」")
        sys.exit(1)

    project = matched[0]

    if action == "save":
        session = SyncSession(project, cfg)
        session.step_check_trial()
        path = session.save_session()
        if json_output:
            print(json.dumps({"result": "ok", "path": str(path)}))
        else:
            print(f"[OK] Session 已保存: {path}")

    elif action == "status":
        restored = SyncSession.load_session(project, cfg)
        if restored is None:
            if json_output:
                print(json.dumps({"result": "no_session"}))
            else:
                print("无已保存的 session")
            return
        if json_output:
            print(json.dumps(restored.status_dict(), indent=2, ensure_ascii=False))
        else:
            d = restored.status_dict()
            print(f"已保存 session: {d['project']}")
            print(f"  阶段: {d['stage']}")
            print(f"  Formal commit: {d['commits']['formal_total']} "
                  f"(synced: {d['commits']['formal_synced']}, "
                  f"pushed: {d['commits']['formal_pushed']})")

    elif action == "resume":
        restored = SyncSession.load_session(project, cfg)
        if restored is None:
            if json_output:
                print(json.dumps({"error": "NO_SESSION"}))
            else:
                print("错误: 无已保存的 session 可恢复")
            sys.exit(1)
        if json_output:
            print(json.dumps({
                "result": "ok",
                "formal_commits_restored": len(restored.formal_commits),
                "stage": restored.stage.name,
            }))
        else:
            print(f"[OK] Session 已恢复: {len(restored.formal_commits)} formal commits")
            for fc in restored.formal_commits:
                print(f"  [{fc.prefix}-{fc.number}] synced={fc.synced} pushed={fc.pushed}")
    else:
        print(f"错误: 未知 session 操作: {action}")
        sys.exit(1)


def _cmd_history(project_name: str = "", op: str | None = None,
                 limit: int = 20, json_output: bool = False):
    """--mode history: 查询操作历史"""
    from backend.core.history import HistoryManager

    entries = HistoryManager.load()
    if not entries:
        if json_output:
            print(json.dumps([]))
        else:
            print("暂无操作记录")
        return

    # 按项目过滤
    if project_name:
        entries = [e for e in entries if e.project_name == project_name]

    # 按操作类型过滤
    if op:
        entries = [e for e in entries if e.operation == op]

    # 限制数量（取最近 N 条）
    if limit and limit > 0:
        entries = entries[-limit:]

    if json_output:
        result = []
        for e in entries:
            item = {
                "timestamp": e.timestamp,
                "project": e.project_name,
                "operation": e.operation or "sync",
                "status": e.status,
                "detail": e.detail or {},
            }
            # 向后兼容：旧条目可能只有 file_count/commit_hash 没有 operation
            if not e.operation and e.commit_hash:
                item["operation"] = "sync"
            result.append(item)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        title = "操作历史"
        if project_name:
            title += f" ({project_name})"
        if op:
            title += f" [{op}]"
        print(f"\n{title} ({len(entries)}):\n")
        for i, e in enumerate(reversed(entries), 1):
            op_display = {
                "scan": "扫描", "formalize": "正式化", "sync": "同步",
                "push": "推送", "triage_accept": "接受", "triage_promote": "提升",
                "triage_discard": "丢弃", "delete_formal": "删除", "dissolve_formal": "溶解",
            }
            op_name = op_display.get(e.operation, e.operation or "同步")
            status_icon = "[OK]" if (e.status or "success") == "success" else "[FAIL]"
            print(f"  [{i}] {e.timestamp[:19]}  {status_icon} {op_name}  {e.project_name}")
            # 显示操作详情
            if e.detail:
                if e.detail.get("commit"):
                    print(f"       commit: {e.detail['commit']}")
                if e.detail.get("trial_hash"):
                    print(f"       trial: {e.detail['trial_hash'][:12]}")
                if e.detail.get("file_count"):
                    print(f"       文件: {e.detail['file_count']}个")
                if e.detail.get("entries_changed"):
                    print(f"       变更: {e.detail['entries_changed']}个")
                if e.detail.get("commit_hash"):
                    print(f"       hash: {e.detail['commit_hash'][:12]}")
            elif e.commit_message:
                print(f"       信息: {e.commit_message}")
            print()


def _cmd_release(cfg: Config, project_name: str, action: str,
                 tag: str = "", name: str = "", body: str = "",
                 json_output: bool = False):
    """--mode release: 管理远程仓库 Release（GitHub/GitLab）。

    --release-action create-release: 创建 Release（需要 --tag）
    --release-action get-info: 获取远程仓库基本信息
    """
    from backend.remote import create_connector

    matched = [p for p in cfg.projects if p.name == project_name]
    if not matched:
        if json_output:
            print(json.dumps({"error": "PROJECT_NOT_FOUND", "name": project_name}))
        else:
            print(f"错误: 未找到项目「{project_name}」")
        sys.exit(1)

    project = matched[0]
    node = project.release
    if not node or not node.remote:
        if json_output:
            print(json.dumps({"error": "NO_REMOTE_CONFIGURED",
                              "message": "release node 未配置 remote 目标"}))
        else:
            print("错误: release node 未配置 remote 目标（在 sync_config.json 中设置 release.remote）")
        sys.exit(1)

    connector = create_connector(node.remote)
    if connector is None:
        if json_output:
            print(json.dumps({"error": "UNSUPPORTED_REMOTE_KIND",
                              "kind": node.remote.kind}))
        else:
            print(f"错误: 不支持的 remote 类型「{node.remote.kind}」，或缺少 token（设置 GITHUB_TOKEN / GITLAB_TOKEN 环境变量）")
        sys.exit(1)

    if action == "get-info":
        info = connector.get_repo_info()
        if "error" in info:
            if json_output:
                print(json.dumps({"error": "API_ERROR", "detail": info}))
            else:
                print(f"错误: {info['error']} — {info.get('message', '')}")
            sys.exit(1)
        if json_output:
            print(json.dumps({"result": "ok", "repo": info}, indent=2, ensure_ascii=False))
        else:
            print(f"[OK] {info.get('full_name', info.get('path_with_namespace', '?'))}")
            print(f"  描述: {info.get('description', '—')}")
            print(f"  URL: {info.get('html_url', info.get('web_url', '—'))}")

    elif action == "create-release":
        if not tag:
            if json_output:
                print(json.dumps({"error": "MISSING_TAG", "message": "--tag 参数必填"}))
            else:
                print("错误: --release-action create-release 需要 --tag 参数")
            sys.exit(1)
        release_name = name or tag
        success, msg = connector.create_release(tag, release_name, body)
        if json_output:
            print(json.dumps({
                "result": "ok" if success else "fail",
                "message": msg,
            }))
        else:
            if success:
                print(f"[OK] Release 已创建: {msg}")
            else:
                print(f"[FAIL] 创建 Release 失败: {msg}")
        if not success:
            sys.exit(1)

    else:
        print(f"错误: 未知 release 操作: {action}")
        print("支持: get-info, create-release")
        sys.exit(1)


# ── _cmd_suggest ───────────────────────────────────────────────

def _cmd_suggest(cfg: Config, project_name: str, suggest_type: str,
                 indices: str = "", json_output: bool = False):
    """--mode suggest: 输出 AI 建议 context JSON。

    suggest_type: "formalize" | "triage" | "summary"
    indices: 逗号分隔的 commit 索引（仅 formalize 子动作使用）
    """
    import json

    project = cfg.get_project(project_name)
    if not project:
        if json_output:
            print(json.dumps({"error": "PROJECT_NOT_FOUND", "name": project_name}))
        else:
            print(f"错误: 项目 {project_name} 未找到")
        sys.exit(1)

    from backend.core.sync_session import SyncSession
    session = SyncSession(project, cfg)

    if suggest_type == "formalize":
        session.step_scan()
        session.step_load_commits()
        context = _build_formalize_context(session, indices)

    elif suggest_type == "triage":
        session.step_check_trial()
        context = _build_triage_context(session)

    elif suggest_type == "summary":
        context = _build_summary_context(session)

    else:
        if json_output:
            print(json.dumps({"error": "UNKNOWN_SUGGEST_TYPE",
                              "suggest_type": suggest_type}))
        else:
            print(f"错误: 未知 suggest 类型: {suggest_type}")
            print("支持: formalize, triage, summary")
        sys.exit(1)

    output = {"suggest": suggest_type, "project": project_name,
              "context": context}
    print(json.dumps(output, ensure_ascii=False, indent=2))


def _build_formalize_context(session: "SyncSession", indices_str: str = "") -> dict:
    """收集 commit proposal context：commits + diff 统计 + 编号信息。"""
    commits = session.commits
    if indices_str:
        selected_idx = {int(i.strip()) for i in indices_str.split(",") if i.strip()}
        commits = [c for i, c in enumerate(commits) if i in selected_idx]

    prefix = session.project.commit_format.get("prefix", "PROJ")
    number_start = session.project.commit_format.get("number_start", 0)
    max_n = number_start
    for fc in session.formal_commits:
        if fc.number > max_n:
            max_n = fc.number
    next_number = max_n + 1

    commit_contexts = []
    git_runner = session.ws_git_runner
    from backend.core.operations.diff import get_diff_summary
    for ci, commit in enumerate(commits):
        if git_runner and git_runner.is_git_repo():
            files_info = get_diff_summary(commit.hash, git_runner)
        else:
            files_info = []

        commit_contexts.append({
            "index": ci if not indices_str else
                     next((i for i in range(len(session.commits))
                           if session.commits[i].hash == commit.hash), ci),
            "hash": commit.hash,
            "type": commit.commit_type,
            "subject": commit.subject,
            "body": commit.body[:500] if commit.body else "",
            "files_changed": files_info,
        })

    return {
        "commits": commit_contexts,
        "prefix": prefix,
        "next_number": next_number,
    }


def _build_triage_context(session: "SyncSession") -> dict:
    """收集 triage context：incoming changes + diff 统计 + release 上下文。"""
    from backend.core.operations.diff import get_diff_summary

    incoming = []
    for i, ch in enumerate(session.incoming_changes):
        # 获取每个 incoming change 的 diff 统计
        trial_runner = session.trial_git_runner
        if trial_runner and trial_runner.is_git_repo():
            files_info = get_diff_summary(ch.hash, trial_runner)
        else:
            files_info = []

        incoming.append({
            "index": i,
            "hash": ch.hash,
            "message": ch.message,
            "author": ch.author,
            "date": ch.timestamp,
            "body": ch.body[:500] if ch.body else "",
            "files_changed": files_info,
        })

    release_context = {
        "recent_formal_commits": [
            f"[{fc.prefix}-{fc.number}] {fc.message.split(chr(10))[0][:80]}"
            for fc in session.formal_commits[-5:]
        ],
    }

    return {
        "incoming_changes": incoming,
        "release_context": release_context,
    }



def _build_summary_context(session: "SyncSession") -> dict:
    """收集 summary context：workspace/trial/release 三段统计。"""
    from collections import Counter

    session.step_scan()
    session.step_load_commits()
    session.step_check_trial()

    # workspace
    changed_entries = [e for e in session.entries if e.status != "same"]
    dir_counter = Counter()
    for e in changed_entries:
        parts = e.rel_path.replace("\\", "/").split("/")
        if len(parts) > 1:
            dir_counter[parts[0]] += 1
        else:
            dir_counter["(root)"] += 1

    # trial
    trial_summary = [
        {"hash": ch.hash, "subject": ch.message.split(chr(10))[0][:80],
         "author": ch.author}
        for ch in session.incoming_changes
    ]

    # release
    pushed = [fc for fc in session.formal_commits if fc.pushed]
    unpushed = [fc for fc in session.formal_commits if fc.synced and not fc.pushed]

    return {
        "workspace": {
            "entries_changed": len(changed_entries),
            "commits_since_base": len(session.commits),
            "top_changed_dirs": [d for d, _ in dir_counter.most_common(5)],
        },
        "trial": {
            "pending": sum(1 for ch in session.incoming_changes
                           if ch.triage.value == "pending"),
            "incoming_summary": trial_summary,
        },
        "release": {
            "formal_commits_waiting_push": len(unpushed),
            "recent_tags": [
                f"[{fc.prefix}-{fc.number}]" for fc in pushed[-5:]
            ],
        },
    }


# ── Template 管理 ──────────────────────────────────────────

