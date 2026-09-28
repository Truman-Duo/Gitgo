"""CUI 工作流 — 扫描/选择/Commit/Sync/Push/Trial"""
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.syntax import Syntax
from backend.core.config import Config
from backend.core import (CommitInfo, FileEntry, SyncSession, build_commit_template,
                  get_file_diff, validate_commit_message)
from backend.models import TrialAction
from backend.core.i18n import _tr

console = None  # set by caller


def edit_commit_message(template: str) -> str:
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


def interactive_file_select(entries: list[FileEntry], workspace: Optional[Path] = None,
                            backup_path: Optional[str] = None):
    """让用户选择哪些文件要同步，支持 d+数字 查看差异"""
    while True:
        from .display import file_table
        console.clear()
        console.print(file_table(entries))
        console.print(f"\n[dim]{_tr('file.menu_help', '操作: [a]全选 [n]取消全选 [1-9]切换序号  d+序号查看差异 [enter]确认')}[/]")
        cmd = Prompt.ask(_tr('file.select', '选择'), default="")
        if cmd == "":
            break
        elif cmd == "a":
            for e in entries:
                if e.status != "same":
                    e.selected = True
        elif cmd == "n":
            for e in entries:
                e.selected = False
        elif cmd.startswith("d") or cmd.startswith("D"):
            num_part = cmd[1:].strip()
            if num_part.isdigit():
                idx = int(num_part) - 1
                if 0 <= idx < len(entries) and workspace and backup_path:
                    diff_text = get_file_diff(workspace, Path(backup_path), entries[idx])
                    if diff_text:
                        console.clear()
                        console.print(Panel(f"[bold]{_tr('file.diff_title', '差异: {p}').format(p=entries[idx].rel_path)}[/]", border_style="blue"))
                        console.print(Syntax(diff_text, "diff", theme="monokai", word_wrap=True))
                    else:
                        console.print(f"[yellow]{_tr('file.no_diff', '（无差异）')}[/]")
                    Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
                else:
                    console.print(f"[red]{_tr('file.invalid_index_or_no_backup', '无效序号或未配置备份路径')}[/]")
            else:
                console.print(f"[red]{_tr('file.diff_format_hint', '格式: d+序号，如 d1')}[/]")
        elif cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(entries):
                entries[idx].selected = not entries[idx].selected
        else:
            matched = [e for e in entries if cmd.lower() in e.rel_path.lower()]
            if not matched:
                console.print(f"[red]{_tr('file.no_match', '未匹配到文件')}[/]")
            elif len(matched) == 1:
                matched[0].selected = not matched[0].selected
            else:
                all_sel = all(m.selected for m in matched)
                for m in matched:
                    m.selected = not all_sel
    return entries


def scan_files(session: SyncSession):
    console.print(Panel(f"[bold]{_tr('scan.title', '扫描与对比')}[/]", border_style="green"))
    entries = session.step_scan()
    if not entries:
        console.print(f"[green]{_tr('scan.all_same', '完全一致，无需同步')}[/]")
    else:
        changed = sum(1 for e in entries if e.selected)
        console.print(f"  [green]{_tr('scan.compare_done', '对比完成')}:[/] {len(entries)} {_tr('scan.file_changes', '个文件变更')}，选中 {changed} 个")
    console.print(f"[green]{_tr('scan.cached_hint', '文件列表已缓存，可使用「选择要同步的文件」进一步操作')}[/]")
    Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")


def select_files(session: SyncSession):
    if not session.entries:
        console.print(f"[red]{_tr('scan.please_scan_first', '请先执行「扫描与对比」')}[/]")
        Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
        return
    console.print(Panel(f"[bold]{_tr('file.select_title', '选择要同步的文件')}[/]", border_style="green"))
    interactive_file_select(session.entries, session.workspace_path,
                            str(session.backup_path) if session.backup_path else None)
    selected = sum(1 for e in session.entries if e.selected)
    console.print(f"  {_tr('file.selected_count', '已选择')} [cyan]{selected}[/] {_tr('file.file_count', '个文件')}")
    Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")


def commit_workflow(session: SyncSession):
    console.print(Panel(f"[bold]{_tr('commit.integration_title', 'Commit 整合')}[/]", border_style="green"))
    from .display import commit_table
    commits = session.step_load_commits()
    if not commits:
        console.print(f"[yellow]{_tr('commit.no_new_commits', '未检测到新 commit')}[/]")
        Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
        return
    selected_set: set[int] = set()
    while True:
        console.clear()
        console.print(commit_table(commits, selected_set))
        console.print()
        console.print(f"[dim]{_tr('commit.selection_help', '输入序号选择/取消（如 1 或 1,3,5 或 1-5），[enter]完成选择，[a]全选')}[/]")
        cmd = Prompt.ask(_tr('commit.select_prompt', '选择'), default="").strip()
        if cmd == "":
            break
        elif cmd == "a":
            selected_set = set(range(len(commits)))
        else:
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
        console.print(f"[yellow]{_tr('commit.min_select', '至少选择 1 个 commit')}[/]")
        Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
        return
    selected_commits = [commits[i] for i in sorted(selected_set)]
    project = session.project
    if len(selected_commits) >= 2:
        console.print(f"\n[green]{_tr('commit.selected_count', '已选择 {n} 个 commit，生成合并模板...').format(n=len(selected_commits))}[/]")
        template = build_commit_template(selected_commits, project)
        msg = edit_commit_message(template)
    else:
        c = selected_commits[0]
        template = build_commit_template([c], project)
        msg = edit_commit_message(template)
    err = validate_commit_message(msg)
    if err:
        console.print(f"[red]{_tr('commit.format_error', '格式错误: {e}').format(e=err)}[/]")
        if not Confirm.ask(_tr('commit.re_edit_prompt', '重新编辑？')):
            return
        msg = edit_commit_message(msg)
        err2 = validate_commit_message(msg)
        if err2:
            console.print(f"[red]{_tr('commit.still_error', '仍错误: {e}').format(e=err2)}[/]")
            return
    fc = session.step_create_formal_commit(selected_indices=selected_set, message=msg)
    if fc:
        console.print(f"[green]{_tr('commit.created', '正式 Commit 已创建: [{p}-{n}]').format(p=fc.prefix, n=fc.number)}[/]")
    Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")


def do_sync(session: SyncSession):
    if not session.entries:
        console.print(f"[red]{_tr('scan.please_scan_first', '请先执行「扫描与对比」')}[/]")
        Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
        return
    target = None
    for fc in session.formal_commits:
        if not fc.synced:
            target = fc
            break
    if target is None:
        console.print(f"[yellow]{_tr('exec.no_sync_pending', '没有待同步的正式 Commit，请先执行 Commit 整合')}[/]")
        Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
        return
    selected = [e for e in session.entries if e.selected]
    if not selected:
        console.print(f"[yellow]{_tr('exec.no_files_selected', '未选择任何文件')}[/]")
        Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
        return
    console.print(Panel(f"[bold]{_tr('exec.sync_title', 'Sync 到备份仓库')}[/]", border_style="green"))
    console.print(f"  {_tr('exec.commit_label', 'Commit')}: {target.message.split(chr(10))[0]}")
    console.print(f"  {_tr('exec.file_count_label', '文件数')}: {len(selected)}")
    if not Confirm.ask(_tr('exec.confirm_sync', '确认同步？')):
        return
    success = session.step_sync()
    if success:
        console.print(f"\n[green bold]{_tr('exec.sync_success', '[OK] 同步成功!')}[/] {_tr('exec.push_hint', '现在可以执行 Push')}")
    else:
        console.print(f"\n[red bold]{_tr('exec.sync_failed', '[FAIL] 同步失败')}[/]")
    Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")


def do_push(session: SyncSession):
    target = None
    for fc in session.formal_commits:
        if fc.synced and not fc.pushed:
            target = fc
            break
    if target is None:
        console.print(f"[yellow]{_tr('exec.no_push_pending', '没有待 push 的正式 Commit')}[/]")
        Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
        return
    console.print(Panel(f"[bold]{_tr('exec.push_title', 'Push 到 GitHub')}[/]", border_style="green"))
    console.print(f"  {_tr('exec.commit_label', 'Commit')}: {target.message.split(chr(10))[0]}")
    if not Confirm.ask(_tr('exec.confirm_push', '确认 push？')):
        return
    def _on_security_warning(warnings: list[dict]) -> bool:
        console.print(f"\n[bold red]{_tr('security.warnings_found', '⚠ 安全检查发现敏感信息！')}[/]")
        console.print()
        for w in warnings:
            severity_color = {"critical": "red", "high": "yellow", "medium": "cyan"}
            c = severity_color.get(w["severity"], "white")
            console.print(f"  [{c}][{w['severity'].upper()}][/] {w['label']}")
            console.print(f"      {_tr('security.file_label', '文件')}: {w['file']}:{w['line']}")
            console.print(f"      {_tr('security.match_label', '匹配')}: {w['match']}")
        console.print()
        return Confirm.ask(f"[yellow]{_tr('security.force_push_confirm', '仍然推送到远程仓库？')}[/]")
    session.on_security_warning = _on_security_warning
    success, _ = session.step_push()
    if success:
        console.print(f"\n[green bold]{_tr('exec.push_success', '[OK] Push 成功!')}[/]")
    else:
        console.print(f"\n[red bold]{_tr('exec.push_failed', '[FAIL] Push 失败')}[/]")
    Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")


def trial_workflow(session: SyncSession):
    from rich.table import Table
    from backend.models import TrialAction
    from backend.core.config import ConfigManager
    console.print(Panel(f"[bold]{_tr('trial.title', 'Trial 三叉工作流')}[/]", border_style="magenta"))
    changes = session.step_check_trial()
    if not changes:
        console.print(f"[green]{_tr('trial.no_new', '无新 commit')}[/]")
        Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
        return
    pending = [c for c in changes if c.triage == TrialAction.PENDING]
    if not pending:
        console.print(f"[green]{_tr('trial.all_processed', '所有变更已处理')}[/]")
        Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
        return
    while True:
        console.clear()
        console.print(Panel(f"[bold]{_tr('trial.incoming_title', 'Trial 新提交')}[/]", border_style="magenta"))
        console.print()
        table = Table(box=None)
        table.add_column("#", width=4)
        table.add_column(_tr('trial.col_hash', 'Hash'), width=12)
        table.add_column(_tr('trial.col_author', '作者'), width=16)
        table.add_column(_tr('trial.col_date', '日期'), width=12)
        table.add_column(_tr('trial.col_message', 'Message'), width=50)
        for i, c in enumerate(pending):
            if c.triage == TrialAction.PENDING:
                table.add_row(str(i + 1), c.hash[:12], c.author[:15], c.timestamp[:10], c.message[:48])
        console.print(table)
        console.print()
        console.print(f"[dim]{_tr('trial.triage_help', '输入序号+操作: 1a=accept, 1p=promote, 1d=discard, [b]返回')}[/]")
        cmd = Prompt.ask(_tr('trial.select_prompt', '选择'), default="").strip().lower()
        if cmd == "b":
            break
        if len(cmd) >= 2 and cmd[:-1].isdigit():
            idx = int(cmd[:-1]) - 1
            ac = cmd[-1]
            action_map = {"a": "accept", "p": "promote", "d": "discard"}
            action = action_map.get(ac)
            if action and 0 <= idx < len(pending):
                success = session.step_triage_incoming(idx, action)
                labels = {"accept": "Accept", "promote": "Promote", "discard": "Discard"}
                if success:
                    console.print(f"[green]{_tr('trial.action_done', '{a} 完成').format(a=labels[action])}[/]")
                else:
                    console.print(f"[red]{_tr('trial.action_failed', '{a} 失败').format(a=labels[action])}[/]")
                pending = [c for c in session.incoming_changes if c.triage == TrialAction.PENDING]
                if not pending:
                    console.print(f"[green]{_tr('trial.all_processed', '所有变更已处理')}[/]")
                    Prompt.ask(f"[dim]{_tr('common.press_enter_return', '按回车返回')}[/]", default="")
                    return
                ConfigManager.save(session.config)
                Prompt.ask(f"[dim]{_tr('common.press_enter', '按回车继续')}[/]", default="")
            else:
                console.print(f"[red]{_tr('common.invalid_input', '无效输入')}[/]")
                Prompt.ask(f"[dim]{_tr('common.press_enter', '按回车继续')}[/]", default="")
        else:
            console.print(f"[red]{_tr('common.invalid_input', '无效输入')}[/]")
            Prompt.ask(f"[dim]{_tr('common.press_enter', '按回车继续')}[/]", default="")
