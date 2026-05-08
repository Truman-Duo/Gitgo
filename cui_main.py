"""CUI 终端界面 - 基于 rich（功能等效于 GUI）"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from config import Config, ConfigManager, ProjectConfig
from core import (
    CommitInfo,
    FileEntry,
    _find_next_number,
    build_commit_template,
    compare_files,
    get_exclude_patterns,
    get_git_log,
    push_to_backup,
    scan_workspace,
    sync_to_backup,
    validate_commit_message,
)

console = Console()


# ── 数据模型（等效 GUI 的 FormalCommit）────────────────────


@dataclass
class FormalCommit:
    message: str
    number: int
    prefix: str
    synced: bool = False
    pushed: bool = False


# ── 项目管理 ──────────────────────────────────────────────


def _project_list(config: Config):
    """展示项目列表，让用户选择或添加"""
    while True:
        console.clear()
        console.print(Panel.fit("[bold]sync_tool — 项目列表[/]", border_style="blue"))
        console.print()

        if not config.projects:
            console.print("[yellow]暂无项目，请先添加[/]\n")
        else:
            table = Table(box=None)
            table.add_column("#", width=4)
            table.add_column("项目名", width=20)
            table.add_column("工作区", width=40)
            table.add_column("备份库", width=40)
            for i, p in enumerate(config.projects, 1):
                ws = p.workspace_path or "(当前目录)"
                bk = p.backup_path or "未设置"
                table.add_row(str(i), p.name, ws, bk)
            console.print(table)

        console.print()
        console.print("[dim]操作: [1-9]选择项目  [a]添加项目  [d]删除项目  [q]退出[/]")
        cmd = Prompt.ask("选择", default="").strip().lower()

        if cmd == "q":
            return None
        elif cmd == "a":
            _add_project(config)
        elif cmd == "d":
            _delete_project(config)
        elif cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(config.projects):
                return config.projects[idx]
            console.print("[red]无效序号[/]")
            Prompt.ask("[dim]按回车继续[/]", default="")
        else:
            console.print("[red]无效输入[/]")
            Prompt.ask("[dim]按回车继续[/]", default="")


def _add_project(config: Config):
    """添加新项目"""
    console.print(Panel.fit("[bold]添加项目[/]", border_style="green"))
    name = Prompt.ask("项目名", default="")
    if not name:
        console.print("[red]项目名不能为空[/]")
        return

    ws = Prompt.ask("工作区路径（留空使用当前目录）", default="")
    bk = Prompt.ask("备份仓库路径", default="")
    if not bk:
        console.print("[red]备份路径不能为空[/]")
        return

    if bk:
        bp = Path(bk)
        if bp.exists() and not (bp / ".git").exists():
            if not Confirm.ask("[yellow]备份路径不是 git 仓库，是否继续？[/]"):
                return

    pc = ProjectConfig(name=name, workspace_path=ws.strip(), backup_path=bk.strip())
    config.projects.append(pc)
    ConfigManager.save(config)
    console.print(f"[green]项目「{name}」已添加[/]")
    Prompt.ask("[dim]按回车继续[/]", default="")


def _delete_project(config: Config):
    """删除项目"""
    num = Prompt.ask("输入要删除的项目编号", default="")
    if num.isdigit():
        idx = int(num) - 1
        if 0 <= idx < len(config.projects):
            p = config.projects[idx]
            if Confirm.ask(f"[yellow]删除项目「{p.name}」？[/]"):
                config.projects.pop(idx)
                ConfigManager.save(config)
                console.print(f"[green]已删除[/]")
                Prompt.ask("[dim]按回车继续[/]", default="")


# ── 文件列表交互 ──────────────────────────────────────────


def _status_style(status: str) -> str:
    return {"new": "green", "modified": "yellow", "same": "white", "renamed": "cyan"}.get(status, "white")


def _file_table(entries: list[FileEntry]) -> Table:
    table = Table(title="文件对比结果", box=None)
    table.add_column("选择", width=6)
    table.add_column("状态", width=10)
    table.add_column("文件路径", width=60)
    table.add_column("备注", width=30)
    for e in entries:
        note = ""
        if e.status == "renamed" and e.old_path:
            note = f"← {e.old_path}"
        elif e.status == "same":
            note = "内容相同，跳过"
        table.add_row("[x]" if e.selected else "[ ]",
                       Text(e.status.upper(), style=_status_style(e.status)),
                       e.rel_path, note)
    return table


def _interactive_file_select(entries: list[FileEntry]):
    """让用户选择哪些文件要同步"""
    while True:
        console.clear()
        console.print(_file_table(entries))
        console.print("\n[dim]操作: [a]全选 [n]取消全选 [1-9]切换序号 关键字切换 [enter]确认[/]")
        cmd = Prompt.ask("选择", default="")
        if cmd == "":
            break
        elif cmd == "a":
            for e in entries:
                if e.status != "same":
                    e.selected = True
        elif cmd == "n":
            for e in entries:
                e.selected = False
        elif cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(entries):
                entries[idx].selected = not entries[idx].selected
        else:
            matched = [e for e in entries if cmd.lower() in e.rel_path.lower()]
            if not matched:
                console.print("[red]未匹配到文件[/]")
            elif len(matched) == 1:
                matched[0].selected = not matched[0].selected
            else:
                all_sel = all(m.selected for m in matched)
                for m in matched:
                    m.selected = not all_sel
    return entries


# ── Commit 交互 ──────────────────────────────────────────


def _commit_table(commits: list[CommitInfo], selected: set[int]) -> Table:
    table = Table(title="工作区 Commit 列表", box=None)
    table.add_column("#", width=4)
    table.add_column("选择", width=6)
    table.add_column("Type", width=10)
    table.add_column("Scope", width=12)
    table.add_column("Subject", width=60)
    for i, c in enumerate(commits, 1):
        sel = "[green]✓[/]" if (i - 1) in selected else "[ ]"
        scope = c.scope or ""
        table.add_row(str(i), sel, c.type, scope, c.subject[:57])
    return table


def _edit_commit_message(template: str) -> str:
    """打开系统编辑器编辑 commit message"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(template)
        tmppath = f.name
    editor = os.environ.get("EDITOR", "notepad.exe")
    try:
        subprocess.run([editor, tmppath], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        subprocess.run(["notepad.exe", tmppath])
    result = Path(tmppath).read_text(encoding="utf-8")
    os.unlink(tmppath)
    return result


# ── CUI FormalCommit 管理 ──────────────────────────────────


def _formal_commit_list(formal_commits: list[FormalCommit]) -> Table:
    table = Table(title="正式 Commits", box=None)
    table.add_column("#", width=4)
    table.add_column("Message", width=60)
    table.add_column("状态", width=20)
    for i, fc in enumerate(formal_commits, 1):
        header = fc.message.split("\n")[0][:57]
        status = ""
        if fc.pushed:
            status = "[green]已推送[/]"
        elif fc.synced:
            status = "[green]已同步[/]"
        else:
            status = "[yellow]未同步[/]"
        table.add_row(str(i), header, status)
    return table


# ── 工作区主流程 ──────────────────────────────────────────


def _main(project: ProjectConfig, config: Config):
    """项目的操作工作流（等效 GUI 的 WorkspacePanel）"""
    workspace = Path(project.workspace_path or Path.cwd()).resolve()
    backup_path = project.backup_path
    formal_commits: list[FormalCommit] = []

    while True:
        console.clear()
        console.print(Panel.fit(f"[bold]{project.name}[/] — 操作菜单", border_style="blue"))
        console.print()

        # 显示当前状态
        console.print(f"  工作区: [cyan]{workspace}[/]")
        console.print(f"  备份库: [cyan]{backup_path or '未配置'}[/]")
        console.print(f"  正式 Commits: {len(formal_commits)} 个")
        console.print()

        # 菜单
        console.print("  [1] 扫描与对比文件")
        console.print("  [2] 选择要同步的文件")
        console.print("  [3] Commit 整合（合并 → 编辑 → 生成正式 Commit）")
        console.print("  [4] Sync 到备份仓库")
        console.print("  [5] Push 到 GitHub")
        if formal_commits:
            console.print("  [6] 查看正式 Commits")
        console.print("  [b] 返回项目列表")
        console.print("  [q] 退出")
        console.print()

        cmd = Prompt.ask("选择", default="").strip().lower()

        if cmd == "q":
            break
        elif cmd == "b":
            return
        elif cmd == "1":
            _scan_files(workspace, backup_path, config, project)
        elif cmd == "2":
            _select_files(workspace, backup_path, config)
        elif cmd == "3":
            _commit_workflow(workspace, config, project, formal_commits)
        elif cmd == "4":
            _do_sync(workspace, backup_path, config, project, formal_commits)
        elif cmd == "5":
            _do_push(backup_path, formal_commits)
        elif cmd == "6":
            _show_formal_commits(formal_commits)
        else:
            console.print("[red]无效选择[/]")

    # 退出前保存配置
    ConfigManager.save(config)


# ── 子流程 ────────────────────────────────────────────────


def _scan_files(workspace: Path, backup_path: str, config: Config, project: ProjectConfig):
    """扫描与对比文件"""
    console.print(Panel("[bold]扫描与对比[/]", border_style="green"))

    exclude = get_exclude_patterns(config, workspace)
    files = scan_workspace(workspace, exclude)
    console.print(f"  找到 [cyan]{len(files)}[/] 个文件")

    if not backup_path:
        console.print("[red]未配置备份路径[/]")
        Prompt.ask("[dim]按回车返回[/]", default="")
        return

    def _progress(c, t, msg=""):
        if msg:
            console.print(f"  [{c}/{t}] {msg}")

    entries = compare_files(workspace, Path(backup_path), files, _progress)
    console.print(f"  [green]对比完成:[/] {len(entries)} 个文件变更")

    if not entries:
        console.print("[green]完全一致，无需同步[/]")
        Prompt.ask("[dim]按回车返回[/]", default="")
        return

    # 保存在全局供后续使用
    _scan_files._entries = entries  # type: ignore
    console.print("[green]文件列表已缓存，可使用「选择要同步的文件」进一步操作[/]")
    Prompt.ask("[dim]按回车返回[/]", default="")


def _select_files(workspace: Path, backup_path: str, config: Config):
    """选择要同步的文件"""
    entries = getattr(_scan_files, "_entries", None)
    if entries is None:
        console.print("[red]请先执行「扫描与对比」[/]")
        Prompt.ask("[dim]按回车返回[/]", default="")
        return

    console.print(Panel("[bold]选择要同步的文件[/]", border_style="green"))
    _interactive_file_select(entries)
    selected = sum(1 for e in entries if e.selected)
    console.print(f"  已选择 [cyan]{selected}[/] 个文件")
    Prompt.ask("[dim]按回车返回[/]", default="")


def _commit_workflow(workspace: Path, config: Config, project: ProjectConfig, formal_commits: list[FormalCommit]):
    """Commit 整合：选择 → 合并 → 编辑 → 生成正式 Commit"""
    console.print(Panel("[bold]Commit 整合[/]", border_style="green"))

    commits = get_git_log(workspace, project.sync_base or None)
    if not commits:
        console.print("[yellow]未检测到新 commit[/]")
        Prompt.ask("[dim]按回车返回[/]", default="")
        return

    # 选择 commit（多选）
    selected_set: set[int] = set()
    while True:
        console.clear()
        console.print(_commit_table(commits, selected_set))
        console.print()
        console.print("[dim]输入序号选择/取消（如 1 或 1,3,5 或 1-5），[enter]完成选择，[a]全选[/]")
        cmd = Prompt.ask("选择", default="").strip()

        if cmd == "":
            break
        elif cmd == "a":
            selected_set = set(range(len(commits)))
        else:
            # 解析逗号分隔或范围
            parts = cmd.replace(",", " ").split()
            for part in parts:
                if "-" in part:
                    try:
                        s, e = part.split("-")
                        for i in range(int(s) - 1, int(e)):
                            if 0 <= i < len(commits):
                                selected_set.add(i)
                    except ValueError:
                        pass
                elif part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(commits):
                        if idx in selected_set:
                            selected_set.discard(idx)
                        else:
                            selected_set.add(idx)

    if len(selected_set) < 1:
        console.print("[yellow]至少选择 1 个 commit[/]")
        Prompt.ask("[dim]按回车返回[/]", default="")
        return

    selected_commits = [commits[i] for i in sorted(selected_set)]

    if len(selected_commits) >= 2:
        # 合并多个 → 编辑 message
        console.print(f"\n[green]已选择 {len(selected_commits)} 个 commit，生成合并模板...[/]")
        template = build_commit_template(selected_commits, config)
        msg = _edit_commit_message(template)
    else:
        # 单个 commit → 直接用其 message
        c = selected_commits[0]
        prefix = project.commit_format.get("prefix", "PROJ")
        template = build_commit_template([c], config)
        msg = _edit_commit_message(template)

    # 验证
    err = validate_commit_message(msg)
    if err:
        console.print(f"[red]格式错误: {err}[/]")
        if not Confirm.ask("重新编辑？"):
            return
        msg = _edit_commit_message(msg)
        err2 = validate_commit_message(msg)
        if err2:
            console.print(f"[red]仍错误: {err2}[/]")
            return

    # 分配编号
    prefix = project.commit_format.get("prefix", "PROJ")
    number_start = project.commit_format.get("number_start", 0)
    max_n = number_start
    for fc in formal_commits:
        if fc.number > max_n:
            max_n = fc.number
    repo_max = _find_next_number(backup_path := project.backup_path, prefix)
    next_n = max(max_n, repo_max)

    fc = FormalCommit(message=msg, number=next_n, prefix=prefix)
    formal_commits.append(fc)
    console.print(f"[green]正式 Commit 已创建: [{prefix}-{fc.number}][/]")
    Prompt.ask("[dim]按回车返回[/]", default="")


def _do_sync(workspace: Path, backup_path: str, config: Config, project: ProjectConfig, formal_commits: list[FormalCommit]):
    """Sync 到备份仓库"""
    entries = getattr(_scan_files, "_entries", None)
    if entries is None:
        console.print("[red]请先执行「扫描与对比」[/]")
        Prompt.ask("[dim]按回车返回[/]", default="")
        return

    # 找第一个未 synced 的 formal commit
    target = None
    for fc in formal_commits:
        if not fc.synced:
            target = fc
            break
    if target is None:
        console.print("[yellow]没有待同步的正式 Commit，请先执行 Commit 整合[/]")
        Prompt.ask("[dim]按回车返回[/]", default="")
        return

    selected = [e for e in entries if e.selected]
    if not selected:
        console.print("[yellow]未选择任何文件[/]")
        Prompt.ask("[dim]按回车返回[/]", default="")
        return

    console.print(Panel("[bold]Sync 到备份仓库[/]", border_style="green"))
    console.print(f"  Commit: {target.message.split(chr(10))[0]}")
    console.print(f"  文件数: {len(selected)}")
    if not Confirm.ask("确认同步？"):
        return

    def _progress(c, t, msg=""):
        if msg:
            console.print(f"  [{c}/{t}] {msg}")

    success = sync_to_backup(selected, target.message, workspace, Path(backup_path), _progress)

    if success:
        target.synced = True
        # 更新 sync_base
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "HEAD"],
                capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
            if result.returncode == 0:
                project.sync_base = result.stdout.strip()
                ConfigManager.save(config)
        except (subprocess.TimeoutExpired, OSError):
            pass
        console.print(f"\n[green bold][OK] 同步成功![/] 现在可以执行 Push")
    else:
        console.print(f"\n[red bold][FAIL] 同步失败[/]")
    Prompt.ask("[dim]按回车返回[/]", default="")


def _do_push(backup_path: str, formal_commits: list[FormalCommit]):
    """Push 到 GitHub"""
    # 找 synced 但未 pushed 的
    target = None
    for fc in formal_commits:
        if fc.synced and not fc.pushed:
            target = fc
            break
    if target is None:
        console.print("[yellow]没有待 push 的正式 Commit[/]")
        Prompt.ask("[dim]按回车返回[/]", default="")
        return

    console.print(Panel("[bold]Push 到 GitHub[/]", border_style="green"))
    console.print(f"  Commit: {target.message.split(chr(10))[0]}")

    if not Confirm.ask("确认 push？"):
        return

    def _progress(c, t, msg=""):
        if msg:
            console.print(f"  {msg}")

    success = push_to_backup(backup_path, progress_callback=_progress)

    if success:
        for fc in formal_commits:
            if fc.synced and not fc.pushed:
                fc.pushed = True
        console.print(f"\n[green bold][OK] Push 成功![/]")
    else:
        console.print(f"\n[red bold][FAIL] Push 失败[/]")
    Prompt.ask("[dim]按回车返回[/]", default="")


def _show_formal_commits(formal_commits: list[FormalCommit]):
    """查看正式 Commits 列表"""
    console.clear()
    if not formal_commits:
        console.print("[yellow]暂无正式 Commit[/]")
    else:
        console.print(_formal_commit_list(formal_commits))
    Prompt.ask("[dim]按回车返回[/]", default="")


# ── 入口 ────────────────────────────────────────────────


def entry():
    """CUI 入口"""
    while True:
        config = ConfigManager.load()
        project = _project_list(config)
        if project is None:
            break
        _main(project, config)

    console.print("[dim]再见[/]")
