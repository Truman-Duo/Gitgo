"""Git 操作 — get_git_log / get_trial_log / build_commit_template / validate_commit_message"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from backend.adapters import GitRunner, LocalGitRunner
from backend.core.config import ProjectConfig
from backend.models import IncomingChange

from .models import CommitInfo


def get_git_log(
    repo_path: str | Path = "",
    since_hash: Optional[str] = None,
    *,
    git_runner: GitRunner | None = None,
) -> list[CommitInfo]:
    """读取工作区的 git 日志，可指定起始 hash"""
    if git_runner is None:
        git_runner = LocalGitRunner(Path(repo_path).resolve())
    if not git_runner.is_git_repo():
        return []

    lines = git_runner.log(
        fmt="%H|||%s|||%b",
        since_hash=since_hash,
        reverse=True,
    )

    commits: list[CommitInfo] = []
    prefix_pattern = re.compile(r"^\[[A-Z]+-\d+\]\s*")
    type_pattern = re.compile(
        r"^(feat|fix|docs|style|refactor|perf|test|chore)"
        r"(?:\(([^)]*)\))?:\s*(.*)"
    )

    for line in lines:
        if not line:
            continue
        parts = line.split("|||", 2)
        if len(parts) < 2:
            continue
        h = parts[0]
        s = parts[1]
        body = parts[2] if len(parts) > 2 else ""

        s_clean = prefix_pattern.sub("", s)
        m = type_pattern.match(s_clean)
        if m:
            ctype = m.group(1)
            cscope = m.group(2)
            csubject = m.group(3)
        else:
            ctype = "chore"
            cscope = None
            csubject = s_clean

        commits.append(
            CommitInfo(
                hash=h,
                subject=csubject if m else s_clean,
                type=ctype,
                scope=cscope,
                body=body.strip(),
            )
        )

    return commits


def get_trial_log(
    trial_path: str | Path = "",
    since_hash: Optional[str] = None,
    *,
    git_runner: GitRunner | None = None,
) -> list[IncomingChange]:
    """读取 trial 仓库自 since_hash 以来的新 commit 列表。"""
    if git_runner is None:
        git_runner = LocalGitRunner(Path(trial_path).resolve())
    if not git_runner.is_git_repo():
        return []

    lines = git_runner.log(
        fmt="%H|||%s|||%an|||%ai|||%b",
        since_hash=since_hash,
        reverse=True,
    )

    changes: list[IncomingChange] = []
    for line in lines:
        if not line:
            continue
        parts = line.split("|||", 4)
        if len(parts) < 4:
            continue
        h, msg, author, ts = parts[0], parts[1], parts[2], parts[3]
        body = parts[4] if len(parts) > 4 else ""
        changes.append(IncomingChange(
            hash=h, message=msg, author=author, timestamp=ts, body=body,
        ))

    return changes


def _find_next_number(
    backup_path: str = "",
    prefix: str = "ANBM",
    *,
    git_runner: GitRunner | None = None,
) -> int:
    """从备份仓库的 commit 历史中找到下一个可用的编号"""
    if git_runner is None:
        if not backup_path:
            return 0
        git_runner = LocalGitRunner(Path(backup_path).resolve())
    if not git_runner.is_git_repo():
        return 0
    lines = git_runner.log(grep=f"^{prefix}-\\d+", fmt="%s", max_count=50)
    max_n = -1
    pat = re.compile(rf"\[{prefix}-(\d+)\]")
    for line in lines:
        m = pat.search(line)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return max_n + 1 if max_n >= 0 else 0


def build_commit_template(
    commits: list[CommitInfo],
    project: ProjectConfig,
    *,
    git_runner: GitRunner | None = None,
) -> str:
    """根据选中的 commit 生成正式 commit message 模板"""
    prefix = project.commit_format.get("prefix", "PROJ")
    number_start = project.commit_format.get("number_start", 0)

    types = set(c.type for c in commits)
    scopes = set(c.scope for c in commits if c.scope)
    type_str = "/".join(sorted(types)) if len(types) > 1 else next(iter(types))
    scope_str = f"({','.join(sorted(scopes))})" if scopes else ""

    subjects = [c.subject for c in commits]
    if not subjects:
        agg_subject = "update"
    elif len(subjects) == 1:
        agg_subject = subjects[0]
    else:
        agg_subject = f"{subjects[0][:30]} +{len(subjects)-1} more"
        if len(agg_subject) > 50:
            agg_subject = agg_subject[:47] + "..."

    max_n = _find_next_number(project.backup_path, prefix, git_runner=git_runner)
    n = max(max_n, number_start)

    header = f"[{prefix}-{n}] {type_str}{scope_str}: {agg_subject}"

    lines = [header, ""]
    lines.append(f"Project: {project.name}")
    lines.append("")
    lines.append(f"Synced from {len(commits)} workspace commit(s):")
    for i, c in enumerate(commits, 1):
        scope_part = f"({c.scope})" if c.scope else ""
        subj = c.subject[:60] + ("..." if len(c.subject) > 60 else "")
        lines.append(f"  {i}. {c.type}{scope_part}: {subj}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("# 请编辑正式 commit message（以上为模板，删除此说明行）")
    lines.append("")

    return "\n".join(lines)


def validate_commit_message(msg: str) -> Optional[str]:
    """验证 commit message，返回 None 或错误信息"""
    lines = [l for l in msg.split("\n") if l and not l.startswith("#")]
    if not lines:
        return "Commit message 不能为空"
    first = lines[0]
    pattern = re.compile(r"^\[[A-Z]+-\d+\]\s+\w+")
    if not pattern.match(first):
        return "首行格式必须为 [PREFIX-N] type: subject"
    return None
