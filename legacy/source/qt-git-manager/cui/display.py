"""CUI 显示 — Rich 表格渲染"""
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from backend.core import CommitInfo, FileEntry, FormalCommit
from backend.core.i18n import _tr

console = None  # set by caller


def status_style(status: str) -> str:
    return {"new": "green", "modified": "yellow", "same": "white", "renamed": "cyan"}.get(status, "white")


def file_table(entries: list[FileEntry]) -> Table:
    table = Table(title=_tr('file.compare_title', '文件对比结果'), box=None)
    table.add_column(_tr('file.select_header', '选择'), width=6)
    table.add_column(_tr('file.status_header', '状态'), width=10)
    table.add_column(_tr('file.path_header', '文件路径'), width=60)
    table.add_column(_tr('file.note_header', '备注'), width=30)
    for e in entries:
        note = ""
        if e.status == "renamed" and e.old_path:
            note = _tr('file.renamed_from', '← {p}').format(p=e.old_path)
        elif e.status == "same":
            note = _tr('file.same_skip', '内容相同，跳过')
        table.add_row("[x]" if e.selected else "[ ]",
                       Text(e.status.upper(), style=status_style(e.status)),
                       e.rel_path, note)
    return table


def commit_table(commits: list[CommitInfo], selected: set[int]) -> Table:
    table = Table(title=_tr('commit.list_title', '工作区 Commit 列表'), box=None)
    table.add_column("#", width=4)
    table.add_column(_tr('commit.select_header', '选择'), width=6)
    table.add_column(_tr('commit.type_header', 'Type'), width=10)
    table.add_column(_tr('commit.scope_header', 'Scope'), width=12)
    table.add_column(_tr('commit.subject_header', 'Subject'), width=60)
    for i, c in enumerate(commits, 1):
        sel = "[green]✓[/]" if (i - 1) in selected else "[ ]"
        scope = c.scope or ""
        table.add_row(str(i), sel, c.type, scope, c.subject[:57])
    return table


def formal_commit_list(formal_commits: list[FormalCommit]) -> Table:
    table = Table(title=_tr('commit.formal_title', '正式 Commits'), box=None)
    table.add_column("#", width=4)
    table.add_column(_tr('commit.message_header', 'Message'), width=60)
    table.add_column(_tr('commit.status_header', '状态'), width=20)
    for i, fc in enumerate(formal_commits, 1):
        header = fc.message.split("\n")[0][:57]
        status = ""
        if fc.pushed:
            status = f"[green]{_tr('commit.pushed', '已推送')}[/]"
        elif fc.synced:
            status = f"[green]{_tr('commit.synced', '已同步')}[/]"
        else:
            status = f"[yellow]{_tr('commit.not_synced', '未同步')}[/]"
        table.add_row(str(i), header, status)
    return table


def show_history():
    """查看同步历史"""
    from backend.core.history import HistoryManager
    entries = HistoryManager.load()
    console.clear()
    if not entries:
        console.print(f"[yellow]{_tr('cui.no_history', '暂无同步记录')}[/]")
    else:
        console.print(Panel(f"[bold]{_tr('cui.history_title', '同步历史')}[/]", border_style="blue"))
        table = Table(box=None)
        table.add_column(_tr('cui.history_time', '时间'), width=20)
        table.add_column(_tr('cui.history_project', '项目'), width=15)
        table.add_column(_tr('cui.history_files', '文件'), width=6)
        table.add_column(_tr('cui.history_commit', '提交'), width=14)
        table.add_column(_tr('cui.history_message', '信息'), width=40)
        for e in reversed(entries[-20:]):
            table.add_row(e.timestamp[:19], e.project_name, str(e.file_count), e.commit_hash[:12], e.commit_message[:38])
        console.print(table)
