"""CLI 入口: python -m gitgo [--mode gui|cui|config|list|sync|daemon|status|history]
   PyInstaller 打包入口。headless 模式下不加载 Qt/Rich。"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

# 确保自身在 sys.path 上（兼容 python __main__.py 直接运行）
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
_meipass = getattr(sys, "_MEIPASS", None)
if _meipass and _meipass not in sys.path:
    sys.path.insert(0, _meipass)

# 使用绝对导入（兼容 -m 和直接运行两种方式）
from backend.core.config import Config, ConfigManager
from backend.core.i18n import available_languages, load_language


def main():
    parser = argparse.ArgumentParser(
        description="工作区 -> 备份仓库同步工具"
    )
    parser.add_argument(
        "--mode",
        choices=["gui", "cui", "config", "list", "sync", "history", "daemon",
                 "status", "trial", "formalize", "scan", "push", "session", "release",
                 "suggest", "governance", "export", "template", "formal"],
        default="gui",
        help="启动模式",
    )
    parser.add_argument(
        "--project", "-p",
        default="",
        help="项目名（与 --mode sync/daemon/status/trial/formalize/scan/push 配合使用）",
    )
    parser.add_argument(
        "--message", "-m",
        default=None,
        help="commit message（与 --mode sync/daemon/formalize 配合使用）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="输出结构化 JSON",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        default=False,
        help="仅输出原始计数，不含 semantic 块（与 --mode status --json 配合使用）",
    )
    parser.add_argument(
        "--semantic-only",
        action="store_true",
        default=False,
        help="仅输出 semantic 块（与 --mode status 配合使用）",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        default=False,
        help="流式输出 line-delimited JSON 进度（与 --mode scan/sync/push/daemon --json 配合使用）",
    )
    parser.add_argument(
        "--op",
        default=None,
        help="过滤操作类型（与 --mode history 配合使用，如 --op formalize）",
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="历史记录条数限制（与 --mode history 配合使用，默认 20）",
    )
    parser.add_argument(
        "--trial-action",
        choices=["list", "accept", "promote", "discard"],
        default="list",
        help="Trial 操作类型（--mode trial 时使用）",
    )
    parser.add_argument(
        "--index", type=int, default=None,
        help="Trial incoming change 索引（--mode trial --trial-action accept/promote/discard 时使用）",
    )
    parser.add_argument(
        "--indices",
        default=None,
        help="Workspace commit 索引，逗号分隔（--mode formalize 时使用，如 --indices 0,2,3）",
    )
    parser.add_argument(
        "--skip-push",
        action="store_true",
        help="跳过 push 步骤（仅 daemon 模式有效）",
    )
    parser.add_argument(
        "--force-on-warning",
        action="store_true",
        help="安全检查命中时自动强制推送（仅 daemon 模式有效）",
    )
    parser.add_argument(
        "--skip-security",
        action="store_true",
        help="跳过安全检查（仅 push 模式有效）",
    )
    parser.add_argument(
        "--daemon-action",
        choices=["start", "stop", "status", "run"],
        default="run",
        help="Daemon 操作（--mode daemon 时使用，默认 run 为一次性全流程）",
    )
    parser.add_argument(
        "--trial-interval",
        type=float,
        default=300.0,
        help="Trial 轮询间隔秒数（--mode daemon --daemon-action start 时使用，默认 300）",
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=2.0,
        help="文件变更去抖秒数（--mode daemon --daemon-action start 时使用，默认 2.0）",
    )
    parser.add_argument(
        "--session-action",
        choices=["save", "status", "resume"],
        default="status",
        help="Session 操作（--mode session 时使用）",
    )
    parser.add_argument(
        "--release-action",
        choices=["get-info", "create-release"],
        default="get-info",
        help="Release 操作类型（--mode release 时使用）",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Release tag（--mode release --release-action create-release 时使用）",
    )
    parser.add_argument(
        "--release-name",
        default="",
        help="Release 名称（--mode release --release-action create-release 时使用）",
    )
    parser.add_argument(
        "--release-body",
        default="",
        help="Release 说明（--mode release --release-action create-release 时使用）",
    )
    parser.add_argument(
        "--suggest-type",
        choices=["formalize", "triage", "summary"],
        default="formalize",
        help="Suggest 子动作（--mode suggest 时使用）",
    )
    parser.add_argument(
        "--governance-type",
        choices=["quality", "patterns", "graph", "releases", "release-note"],
        default="quality",
        help="Governance 子动作（--mode governance 时使用）",
    )
    parser.add_argument(
        "--export-type",
        choices=["state-bundle"],
        default="state-bundle",
        help="Export 子动作（--mode export 时使用）",
    )
    parser.add_argument(
        "--minimal",
        action="store_true",
        default=False,
        help="仅导出状态快照，不含 history（--mode export --export-type state-bundle 时使用）",
    )
    parser.add_argument(
        "--template",
        default=None,
        help="Template 名称（--mode formalize 时使用，覆盖项目默认模板）",
    )
    parser.add_argument(
        "--template-action",
        choices=["list", "add", "edit", "delete"],
        default="list",
        help="Template 操作类型（--mode template 时使用）",
    )
    parser.add_argument(
        "--template-name",
        default=None,
        help="Template 名称（--mode template --template-action add/edit/delete 时使用）",
    )
    parser.add_argument(
        "--template-desc",
        default="",
        help="Template 描述（--mode template --template-action add/edit 时使用）",
    )
    parser.add_argument(
        "--template-header",
        default="",
        help="header_format（--mode template --template-action add/edit 时使用）",
    )
    parser.add_argument(
        "--template-body",
        default="",
        help="body_format（--mode template --template-action add/edit 时使用）",
    )
    parser.add_argument(
        "--template-prefix",
        default=None,
        help="prefix_override（--mode template --template-action add/edit 时使用）",
    )
    parser.add_argument(
        "--formal-action",
        choices=["list", "delete", "edit-message", "edit-number", "dissolve", "clear-sources"],
        default="list",
        help="Formal 管理操作（--mode formal 时使用）",
    )
    parser.add_argument(
        "--formal-index", type=int, default=None,
        help="Formal commit 索引（--mode formal --formal-action delete/edit-message/edit-number/dissolve/clear-sources 时使用）",
    )
    parser.add_argument(
        "--new-number", type=int, default=None,
        help="新编号（--mode formal --formal-action edit-number 时使用）",
    )
    args = parser.parse_args()

    try:
        # 加载界面语言（非 GUI/CUI 模式不影响）
        cfg = ConfigManager.load()
        if cfg.language:
            load_language(cfg.language)

        if args.mode == "gui":
            from frontend.gui_main import entry as gui_entry
            if getattr(sys, "frozen", False):
                import tempfile
                _log = Path(tempfile.gettempdir()) / "gitgo_startup.log"
                _log.write_text(f"Starting GUI mode at {__import__('datetime').datetime.now()}\n", encoding="utf-8")
            gui_entry()
        elif args.mode == "cui":
            from cui.main import entry as cui_entry
            cui_entry()
        elif args.mode == "config":
            from cli import _cmd_list
            _cmd_list(cfg)
        elif args.mode == "list":
            from cli import _cmd_list
            _cmd_list(cfg)
        elif args.mode == "history":
            from cli import _cmd_history
            _cmd_history(project_name=args.project, op=args.op,
                         limit=args.limit, json_output=args.json)
        elif args.mode == "sync":
            if not args.project:
                print("错误: --mode sync 需要 --project NAME 参数")
                print("用法: python -m gitgo --mode sync --project MyApp")
                sys.exit(1)
            from cli import _cmd_sync
            _cmd_sync(cfg, args.project, args.message, json_output=args.json,
                      stream=args.stream)
        elif args.mode == "daemon":
            if not args.project:
                print("错误: --mode daemon 需要 --project NAME 参数")
                print("用法: python -m gitgo --mode daemon --project MyApp [--daemon-action start|stop|status|run] [--skip-push] [--force-on-warning]")
                sys.exit(1)
            from cli import _cmd_daemon
            _cmd_daemon(cfg, args.project, args.message,
                        skip_push=args.skip_push,
                        force_on_warning=args.force_on_warning,
                        json_output=args.json,
                        stream=args.stream,
                        daemon_action=args.daemon_action,
                        trial_interval=args.trial_interval,
                        debounce_sec=args.debounce)
        elif args.mode == "status":
            if not args.project:
                print("错误: --mode status 需要 --project NAME 参数")
                sys.exit(1)
            from cli import _cmd_status
            _cmd_status(cfg, args.project, json_output=args.json,
                        raw=args.raw, semantic_only=getattr(args, 'semantic_only', False))
        elif args.mode == "trial":
            if not args.project:
                print("错误: --mode trial 需要 --project NAME 参数")
                sys.exit(1)
            from cli import _cmd_trial
            _cmd_trial(cfg, args.project, args.trial_action,
                        index=args.index, json_output=args.json)
        elif args.mode == "formalize":
            if not args.project:
                print("错误: --mode formalize 需要 --project NAME 参数")
                sys.exit(1)
            from cli import _cmd_formalize
            _cmd_formalize(cfg, args.project, indices=args.indices,
                           message=args.message, template_name=args.template,
                           json_output=args.json)
        elif args.mode == "scan":
            if not args.project:
                print("错误: --mode scan 需要 --project NAME 参数")
                sys.exit(1)
            from cli import _cmd_scan
            _cmd_scan(cfg, args.project, json_output=args.json,
                      stream=args.stream)
        elif args.mode == "push":
            if not args.project:
                print("错误: --mode push 需要 --project NAME 参数")
                sys.exit(1)
            from cli import _cmd_push
            _cmd_push(cfg, args.project, skip_security=args.skip_security,
                       json_output=args.json, stream=args.stream)
        elif args.mode == "session":
            if not args.project:
                print("错误: --mode session 需要 --project NAME 参数")
                sys.exit(1)
            from cli import _cmd_session
            _cmd_session(cfg, args.project, args.session_action,
                          json_output=args.json)
        elif args.mode == "release":
            if not args.project:
                print("错误: --mode release 需要 --project NAME 参数")
                sys.exit(1)
            from cli import _cmd_release
            _cmd_release(cfg, args.project, args.release_action,
                         tag=args.tag, name=args.release_name,
                         body=args.release_body, json_output=args.json)
        elif args.mode == "suggest":
            if not args.project:
                print("错误: --mode suggest 需要 --project NAME 参数")
                sys.exit(1)
            from cli import _cmd_suggest
            _cmd_suggest(cfg, args.project, args.suggest_type,
                         indices=args.indices, json_output=args.json)
        elif args.mode == "governance":
            if not args.project:
                print("错误: --mode governance 需要 --project NAME 参数")
                sys.exit(1)
            from cli import _cmd_governance
            _cmd_governance(cfg, args.project, args.governance_type,
                           message=args.message or "", json_output=args.json)
        elif args.mode == "export":
            if not args.project:
                print("错误: --mode export 需要 --project NAME 参数")
                sys.exit(1)
            from cli import _cmd_export
            _cmd_export(cfg, args.project, args.export_type,
                       minimal=args.minimal, json_output=args.json)
        elif args.mode == "template":
            from cli import _cmd_template
            _cmd_template(cfg, args.template_action,
                         name=args.template_name,
                         description=args.template_desc,
                         header_format=args.template_header,
                         body_format=args.template_body,
                         prefix_override=args.template_prefix,
                         json_output=args.json)
        elif args.mode == "formal":
            if not args.project:
                print("错误: --mode formal 需要 --project NAME 参数")
                sys.exit(1)
            from cli import _cmd_formal
            _cmd_formal(cfg, args.project, args.formal_action,
                       formal_index=args.formal_index,
                       message=args.message,
                       new_number=args.new_number,
                       json_output=args.json)
    except Exception as e:
        msg = f"启动失败:\n{traceback.format_exc()}"
        print(msg, file=sys.stderr)
        import tempfile
        (Path(tempfile.gettempdir()) / "gitgo_crash.log").write_text(msg, encoding="utf-8")
        if getattr(sys, "frozen", False):
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                app = QApplication(sys.argv)
                QMessageBox.critical(None, "gitgo - 错误", msg)
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
