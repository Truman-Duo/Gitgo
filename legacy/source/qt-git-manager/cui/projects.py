"""CUI 项目管理 — 列表/添加/删除"""
from pathlib import Path
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.models import FileAccess, RepoNode
from backend.core.i18n import _tr

console = None  # set by caller


def project_list(config: Config):
    """展示项目列表，让用户选择或添加"""
    while True:
        console.clear()
        console.print(Panel.fit(f"[bold]{_tr('project.list_title', 'gitgo — 项目列表')}[/]", border_style="blue"))
        console.print()

        if not config.projects:
            console.print(f"[yellow]{_tr('project.no_projects', '暂无项目，请先添加')}[/]\n")
        else:
            table = Table(box=None)
            table.add_column("#", width=4)
            table.add_column(_tr('project.name_header', '项目名'), width=20)
            table.add_column(_tr('project.workspace_header', '工作区'), width=40)
            table.add_column(_tr('project.backup_header', '备份库'), width=40)
            for i, p in enumerate(config.projects, 1):
                ws = p.workspace_path or _tr('project.current_dir', '(当前目录)')
                bk = p.backup_path or _tr('project.not_set', '未设置')
                table.add_row(str(i), p.name, ws, bk)
            console.print(table)

        console.print()
        console.print(f"[dim]{_tr('project.menu_help', '操作: [1-9]选择项目  [a]添加项目  [d]删除项目  [q]退出')}[/]")
        cmd = Prompt.ask(_tr('project.select_prompt', '选择'), default="").strip().lower()

        if cmd == "q":
            return None
        elif cmd == "a":
            add_project(config)
        elif cmd == "d":
            delete_project(config)
        elif cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(config.projects):
                return config.projects[idx]
            console.print(f"[red]{_tr('project.invalid_index', '无效序号')}[/]")
            Prompt.ask(f"[dim]{_tr('common.press_enter', '按回车继续')}[/]", default="")
        else:
            console.print(f"[red]{_tr('common.invalid_input', '无效输入')}[/]")
            Prompt.ask(f"[dim]{_tr('common.press_enter', '按回车继续')}[/]", default="")


def add_project(config: Config):
    """添加新项目"""
    console.print(Panel.fit(f"[bold]{_tr('project.add_title', '添加项目')}[/]", border_style="green"))
    name = Prompt.ask(_tr('project.name_prompt', '项目名'), default="").strip()
    if not name:
        console.print(f"[red]{_tr('project.name_required', '项目名不能为空')}[/]")
        return

    if name in [p.name for p in config.projects]:
        console.print(f"[red]{_tr('project.name_exists', '该项目名已存在')}[/]")
        Prompt.ask(f"[dim]{_tr('common.press_enter', '按回车继续')}[/]", default="")
        return

    ws = Prompt.ask(_tr('project.workspace_prompt', '工作区路径（留空使用当前目录）'), default="").strip()
    bk = Prompt.ask(_tr('project.backup_prompt', '备份仓库路径'), default="").strip()
    if not bk:
        console.print(f"[red]{_tr('project.backup_required', '备份路径不能为空')}[/]")
        return

    if ws and bk and ws == bk:
        console.print(f"[red]{_tr('project.same_path', '工作区路径与备份路径不能相同')}[/]")
        Prompt.ask(f"[dim]{_tr('common.press_enter', '按回车继续')}[/]", default="")
        return

    tl = Prompt.ask(_tr('project.trial_prompt', 'Trial 仓库路径（可选，三叉工作流用）'), default="").strip()

    if bk:
        bp = Path(bk)
        if bp.exists() and not (bp / ".git").exists():
            if not Confirm.ask(f"[yellow]{_tr('project.not_git_confirm', '备份路径不是 git 仓库，是否继续？')}[/]"):
                return

    pc = ProjectConfig(
        name=name,
        workspace=RepoNode(file_access=FileAccess(path=ws.strip())),
        release=RepoNode(file_access=FileAccess(path=bk.strip())),
        trial=RepoNode(file_access=FileAccess(path=tl)) if tl else None,
    )
    config.projects.append(pc)
    ConfigManager.save(config)
    console.print(f"[green]{_tr('project.added', '项目「{n}」已添加').format(n=name)}[/]")
    Prompt.ask(f"[dim]{_tr('common.press_enter', '按回车继续')}[/]", default="")


def delete_project(config: Config):
    """删除项目"""
    num = Prompt.ask(_tr('project.delete_prompt', '输入要删除的项目编号'), default="")
    if num.isdigit():
        idx = int(num) - 1
        if 0 <= idx < len(config.projects):
            p = config.projects[idx]
            if Confirm.ask(f"[yellow]{_tr('project.confirm_delete', '删除项目「{n}」？').format(n=p.name)}[/]"):
                config.projects.pop(idx)
                ConfigManager.save(config)
                console.print(f"[green]{_tr('common.deleted', '已删除')}[/]")
                Prompt.ask(f"[dim]{_tr('common.press_enter', '按回车继续')}[/]", default="")
