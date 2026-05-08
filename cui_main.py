"""CUI 终端界面 - 基于 rich"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from config import Config, ConfigManager
from core import (
    CommitInfo,
    FileEntry,
    build_commit_template,
    compare_files,
    get_exclude_patterns,
    get_git_log,
    scan_workspace,
    sync_to_backup,
    validate_commit_message,
)

console = Console()


# ── Wizard ────────────────────────────────────────────────


def setup_wizard() -> Optional[Config]:
    """首次配置向导"""
    console.print(Panel.fit("[bold]首次运行 - 配置向导[/]", border_style="blue"))

    backup_path = Prompt.ask("请输入备份仓库的完整路径", default="")
    if not backup_path:
        console.print("[red]备份路径不能为空[/]")
        return None

    # 验证是否是 git 仓库
    bp = Path(backup_path)
    git_dir = bp / ".git"
    if not git_dir.exists():
        proceed = Confirm.ask(
            f"[yellow]'{backup_path}' 不是 git 仓库，是否继续？[/]"
        )
        if not proceed:
            return None

    cfg = Config(backup_path=str(bp.resolve()))
    ConfigManager.save(cfg)
    console.print(f"[green][OK] 配置已保存到 {ConfigManager.default_path()}[/]")
    return cfg


# ── 文件列表展示 ──────────────────────────────────────────


def _status_style(status: str) -> str:
    return {
        "new": "green",
        "modified": "yellow",
        "same": "white",
        "renamed": "cyan",
    }.get(status, "white")


def _file_table(entries: list[FileEntry]) -> Table:
    table = Table(title="文件对比结果", box=None)
    table.add_column("选择", width=6)
    table.add_column("状态", width=10)
    table.add_column("文件路径", width=60)
    table.add_column("备注", width=30)

    for e in entries:
        status_text = Text(e.status.upper(), style=_status_style(e.status))
        note = ""
        if e.status == "renamed" and e.old_path:
            note = f"← {e.old_path}"
        elif e.status == "same":
            note = "内容相同，跳过"

        table.add_row(
            "[x]" if e.selected else "[ ]",
            status_text,
            e.rel_path,
            note,
        )
    return table


def _interactive_file_select(
    entries: list[FileEntry],
) -> list[FileEntry]:
    """让用户选择哪些文件要同步"""
    while True:
        console.clear()
        console.print(_file_table(entries))
        console.print("\n[dim]操作: [a]全选 [n]取消全选 [1-9]切换序号 [/]输入路径关键字切换 [enter]确认[/]")
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
            # 关键字匹配：切换匹配文件的选择状态
            matched = [e for e in entries if cmd.lower() in e.rel_path.lower()]
            if len(matched) == 1:
                matched[0].selected = not matched[0].selected
            elif matched:
                all_selected = all(m.selected for m in matched)
                for m in matched:
                    m.selected = not all_selected
            else:
                console.print("[red]未匹配到文件[/]")

    return entries


# ── 相同文件确认 ──────────────────────────────────────────


def _confirm_same_files(entries: list[FileEntry]) -> list[FileEntry]:
    """对 status=same 的文件，逐个询问用户"""
    for e in entries:
        if e.status == "same":
            console.print(f"  [white]文件 '{e.rel_path}' 内容与备份一致[/]")
            choice = Prompt.ask(
                "  操作", choices=["s", "o", "a"], default="s",
                show_choices=False
            )
            # s=跳过, o=覆盖, a=全部跳过
            if choice == "o":
                e.selected = True
            elif choice in ("a", "s"):
                e.selected = False
            if choice == "a":
                break  # 剩余全部跳过
    return entries


# ── Commit 选择 ──────────────────────────────────────────


def _commit_table(commits: list[CommitInfo]) -> Table:
    table = Table(title="工作区 Commit 列表", box=None)
    table.add_column("#", width=4)
    table.add_column("选择", width=6)
    table.add_column("Type", width=10)
    table.add_column("Scope", width=12)
    table.add_column("Subject", width=60)

    for i, c in enumerate(commits, 1):
        scope = c.scope or ""
        table.add_row(
            str(i),
            "[x]" if True else "[ ]",
            c.type,
            scope,
            c.subject[:57] + ("..." if len(c.subject) > 57 else ""),
        )
    return table


def _edit_commit_message(template: str) -> str:
    """打开系统编辑器让用户编辑 commit message"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(template)
        tmppath = f.name

    editor = os.environ.get("EDITOR", "notepad.exe")
    try:
        subprocess.run([editor, tmppath], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        # notepad 一定能用
        subprocess.run(["notepad.exe", tmppath])

    result = Path(tmppath).read_text(encoding="utf-8")
    os.unlink(tmppath)
    return result


# ── 主流程 ────────────────────────────────────────────────


def main(config: Config):
    workspace = Path.cwd().resolve()

    # 步骤 0: 合并排除规则
    exclude_patterns = get_exclude_patterns(config, workspace)
    console.print(Panel("[bold]步骤 1/4[/] - 扫描工作区文件", border_style="green"))

    # 步骤 1: 扫描
    files = scan_workspace(workspace, exclude_patterns)
    console.print(f"  找到 [cyan]{len(files)}[/] 个文件")

    # 对比
    if not config.backup_path:
        console.print("[red]错误: 未配置备份路径，请先运行配置向导[/]")
        return

    console.print("\n[bold]正在对比文件...[/]")

    def _progress(current, total, msg=""):
        if msg:
            console.print(f"  [{current}/{total}] {msg}")

    entries = compare_files(
        workspace, Path(config.backup_path), files, _progress
    )
    console.print(f"  [green]对比完成:[/] {len(entries)} 个文件变更")

    if not entries:
        console.print("[green][OK] 工作区和备份仓库完全一致，无需同步[/]")
        return

    # 步骤 2: 相同文件确认
    same_files = [e for e in entries if e.status == "same"]
    if same_files:
        console.print(
            Panel(
                f"[yellow]{len(same_files)}[/] 个文件内容与备份完全一致",
                border_style="yellow",
            )
        )
        _confirm_same_files(entries)

    # 步骤 3: 用户选择文件
    console.print(Panel("[bold]步骤 2/4[/] - 选择要同步的文件", border_style="green"))
    _interactive_file_select(entries)

    selected_count = sum(1 for e in entries if e.selected)
    if selected_count == 0:
        console.print("[yellow]未选择任何文件，退出[/]")
        return

    console.print(f"  已选择 [cyan]{selected_count}[/] 个文件")

    # 步骤 4: Commit 整合
    console.print(Panel("[bold]步骤 3/4[/] - Commit 整合", border_style="green"))
    commits = get_git_log(workspace, config.sync_base or None)

    if commits:
        console.print(_commit_table(commits))
        console.print("\n[dim]按回车确认所有 commit，或输入范围如 1-3[/]")
        cmd = Prompt.ask("选择 commit", default="all")

        selected_commits = list(commits)  # 默认全选
        if cmd != "all" and cmd != "":
            parts = cmd.split("-")
            try:
                start = int(parts[0]) - 1
                end = int(parts[1]) - 1 if len(parts) > 1 else start
                selected_commits = commits[start : end + 1]
            except (ValueError, IndexError):
                console.print("[red]无效范围，使用全部[/]")
                selected_commits = list(commits)
    else:
        console.print("[yellow]未检测到新 commit，使用简单 commit message[/]")
        selected_commits = []

    # 生成并编辑 commit message
    template = build_commit_template(selected_commits or [CommitInfo(hash="", subject="sync update", type="chore", scope=None)], config)
    console.print("[bold]正在编辑 commit message...[/]")

    msg = _edit_commit_message(template)

    # 验证
    err = validate_commit_message(msg)
    if err:
        console.print(f"[red]Commit message 格式错误: {err}[/]")
        retry = Confirm.ask("重新编辑？")
        if retry:
            msg = _edit_commit_message(msg)
        else:
            console.print("[red]取消同步[/]")
            return

    # 步骤 5: 执行同步
    console.print(Panel("[bold]步骤 4/4[/] - 同步到备份仓库", border_style="green"))

    Confirm.ask(f"确认将 [cyan]{selected_count}[/] 个文件同步到备份仓库？")

    success = sync_to_backup(
        entries, msg, workspace, Path(config.backup_path), _progress
    )

    if success:
        # 更新 sync_base
        try:
            import subprocess

            result = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )
            if result.returncode == 0:
                config.sync_base = result.stdout.strip()
                ConfigManager.save(config)
        except (subprocess.TimeoutExpired, OSError):
            pass
        console.print("\n[green bold][OK] 同步成功![/] 请手动 push 备份仓库的 commit")
    else:
        console.print("\n[red bold][FAIL] 同步失败[/]，请检查错误信息")


def entry():
    """CUI 入口"""
    console.clear()
    console.print(Panel.fit("[bold]工作区 <-> 备份仓库同步工具[/]", border_style="blue"))

    config = ConfigManager.load()
    if not config.backup_path:
        console.print("[yellow]首次使用，请先配置备份路径[/]")
        cfg = setup_wizard()
        if not cfg:
            return
        config = cfg

    main(config)
