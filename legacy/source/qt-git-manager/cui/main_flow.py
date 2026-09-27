"""CUI 主流程 — 项目操作菜单 + 入口"""
from rich.panel import Panel
from rich.prompt import Prompt
from backend.core.config import Config, ConfigManager
from backend.core import SyncSession
from backend.core.i18n import _tr


def main_menu(project, config: Config, console, scan_fn, select_fn, commit_fn, sync_fn, push_fn, trial_fn):
    """项目的操作工作流（等效 GUI 的 WorkspacePanel）"""
    from .display import show_history
    session = SyncSession(project, config)

    def _progress(c, t, msg=""):
        if msg:
            console.print(f"  [{c}/{t}] {msg}")
    session.on_progress = _progress
    session.on_log = lambda msg: console.print(f"  {msg}")

    while True:
        console.clear()
        console.print(Panel.fit(f"[bold]{_tr('cui.menu_title', '{n} — 操作菜单').format(n=project.name)}[/]", border_style="blue"))
        console.print()
        console.print(f"  {_tr('cui.workspace_label', '工作区')}: [cyan]{session.workspace_path}[/]")
        console.print(f"  {_tr('cui.backup_label', '备份库')}: [cyan]{session.backup_path or _tr('cui.not_configured', '未配置')}[/]")
        console.print(f"  {_tr('cui.formal_commits_label', '正式 Commits')}: {len(session.formal_commits)} {_tr('cui.count_unit', '个')}")
        console.print()

        console.print(f"  {_tr('cui.menu_scan', '[1] 扫描与对比文件')}")
        console.print(f"  {_tr('cui.menu_select', '[2] 选择要同步的文件')}")
        console.print(f"  {_tr('cui.menu_commit', '[3] Commit 整合（合并 → 编辑 → 生成正式 Commit）')}")
        console.print(f"  {_tr('cui.menu_sync', '[4] Sync 到备份仓库')}")
        console.print(f"  {_tr('cui.menu_push', '[5] Push 到 GitHub')}")
        if session.formal_commits:
            console.print(f"  {_tr('cui.menu_view_commits', '[6] 查看正式 Commits')}")
        console.print(f"  {_tr('cui.menu_history', '[7] 查看同步历史')}")
        console.print(f"  {_tr('cui.menu_back', '[b] 返回项目列表')}")
        console.print(f"  {_tr('cui.menu_trial_check', '[t] Trial 检查与三叉处理')}")
        console.print(f"  {_tr('cui.menu_quit', '[q] 退出')}")
        console.print()
        cmd = Prompt.ask(_tr('cui.select_prompt', '选择'), default="").strip().lower()
        if cmd == "q":
            break
        elif cmd == "b":
            return
        elif cmd == "1":
            scan_fn(session)
        elif cmd == "2":
            select_fn(session)
        elif cmd == "3":
            commit_fn(session)
        elif cmd == "4":
            sync_fn(session)
        elif cmd == "5":
            push_fn(session)
        elif cmd == "6":
            from .display import formal_commit_list
            console.clear()
            if not session.formal_commits:
                console.print(f"[yellow]{_tr('commit.no_formal_commits', '暂无正式 Commit')}[/]")
            else:
                console.print(formal_commit_list(session.formal_commits))
            Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
        elif cmd == "7":
            show_history()
            Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
        elif cmd == "t":
            trial_fn(session)
        else:
            console.print(f"[red]{_tr('cui.invalid_choice', '无效选择')}[/]")

    ConfigManager.save(config)


def entry(console, config_class=Config, config_manager_class=ConfigManager):
    """CUI 入口"""
    from .projects import project_list
    from .workflow import scan_files, select_files, commit_workflow, do_sync, do_push, trial_workflow
    while True:
        config = config_manager_class.load()
        if config.language:
            from backend.core.i18n import load_language
            load_language(config.language)
        project = project_list(config)
        if project is None:
            break
        main_menu(project, config, console, scan_files, select_files, commit_workflow, do_sync, do_push, trial_workflow)
    console.print(f"[dim]{_tr('cui.goodbye', '再见')}[/]")
