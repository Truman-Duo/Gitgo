"""核心逻辑 - 文件扫描/哈希对比/git操作/commit生成"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from config import Config

# ── 数据结构 ─────────────────────────────────────────────────


@dataclass
class FileEntry:
    rel_path: str  # 相对工作区根目录的路径，使用 /
    status: str  # new | modified | same | renamed
    old_path: Optional[str] = None  # renamed 时记录旧路径
    workspace_hash: str = ""
    backup_hash: str = ""
    selected: bool = True


@dataclass
class CommitInfo:
    hash: str
    subject: str
    type: str  # feat/fix/docs/...
    scope: Optional[str]
    body: str = ""


# ── 工具函数 ─────────────────────────────────────────────────


def _hash_file(filepath: str | Path) -> str:
    """计算文件 SHA256 哈希，流式读取支持大文件"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(8192 * 1024)  # 8MB chunks
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_binary(filepath: str | Path) -> bool:
    """快速检测二进制文件"""
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(1024)
            return b"\0" in chunk
    except OSError:
        return True


def _normalize_path(path: str) -> str:
    """将路径转换为 / 分隔的 Unix 风格"""
    return path.replace(os.sep, "/")


def _read_gitignore(workspace: Path) -> list[str]:
    """读取 .gitignore 返回规则列表"""
    gi = workspace / ".gitignore"
    if gi.exists():
        return [
            line.strip()
            for line in gi.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    return []


def _match_glob(pattern: str, path: str) -> bool:
    """简易 glob 匹配（支持 ** 前缀/后缀，其余 fnmatch）"""
    import fnmatch

    # 目录标记规范
    if pattern.endswith("/"):
        pattern = pattern.rstrip("/")

    # 以 / 开头 → 匹配相对路径前缀
    if pattern.startswith("/"):
        pattern = pattern[1:]
        return fnmatch.fnmatch(path, pattern)

    # **/xxx → 任意位置匹配
    if pattern.startswith("**/"):
        inner = pattern[3:]
        parts = path.split("/")
        return any(fnmatch.fnmatch(p, inner) for p in parts)

    # xxx/** → 前缀匹配
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path.startswith(prefix) or fnmatch.fnmatch(path, pattern)

    # 普通 glob → 任意层级匹配
    parts = path.split("/")
    return any(fnmatch.fnmatch(p, pattern) for p in parts) or fnmatch.fnmatch(path, pattern)


def _is_excluded(rel_path: str, patterns: list[str]) -> bool:
    """检查路径是否匹配任意排除规则"""
    return any(_match_glob(p, rel_path) for p in patterns)


# ── 扫描与对比 ───────────────────────────────────────────────


def scan_workspace(
    workspace: str | Path, exclude_patterns: list[str]
) -> list[str]:
    """扫描工作区，返回所有未排除文件的相对路径列表"""
    ws = Path(workspace).resolve()
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ws):
        # 跳过 .git 目录
        if ".git" in dirnames:
            dirnames.remove(".git")

        for fn in filenames:
            full = Path(dirpath) / fn
            try:
                if full.is_symlink():
                    continue
                rel = _normalize_path(str(full.relative_to(ws)))
                if not _is_excluded(rel, exclude_patterns):
                    results.append(rel)
            except (ValueError, OSError):
                continue
    return sorted(results)


def compare_files(
    workspace: str | Path,
    backup: str | Path,
    file_list: list[str],
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> list[FileEntry]:
    """对比工作区和备份仓库的文件，返回带状态的文件列表"""
    ws = Path(workspace).resolve()
    bk = Path(backup).resolve()
    total = len(file_list)
    entries: list[FileEntry] = []
    # hash → [rel_path] 映射（用于检测重命名）
    backup_by_hash: dict[str, list[str]] = {}
    ws_by_hash: dict[str, list[str]] = {}

    # 1. 预先计算备份仓库所有文件的 hash
    if total > 0 and progress_callback:
        progress_callback(0, total, "正在扫描备份仓库...")
    backup_files: list[str] = []
    if bk.exists():
        for dirpath, dirnames, filenames in os.walk(bk):
            if ".git" in dirnames:
                dirnames.remove(".git")
            for fn in filenames:
                full = Path(dirpath) / fn
                try:
                    if full.is_symlink():
                        continue
                    rel = _normalize_path(str(full.relative_to(bk)))
                    # 不包含 .git 下的文件
                    if not rel.startswith(".git/"):
                        backup_files.append(str(bk / rel))
                        backup_by_hash.setdefault(
                            _hash_file(str(full)), []
                        ).append(rel)
                except (ValueError, OSError):
                    continue

    # 2. 逐个对比工作区文件
    for idx, rel in enumerate(file_list):
        if progress_callback:
            progress_callback(idx + 1, total, rel)

        ws_path = ws / rel
        if not ws_path.exists():
            continue
        if _is_binary(ws_path):
            continue

        ws_hash = _hash_file(ws_path)
        bk_path = bk / rel
        bk_exists = bk_path.exists()

        if not bk_exists:
            entries.append(
                FileEntry(
                    rel_path=rel,
                    status="new",
                    workspace_hash=ws_hash,
                    selected=True,
                )
            )
        else:
            bk_hash = _hash_file(bk_path)
            if ws_hash == bk_hash:
                entries.append(
                    FileEntry(
                        rel_path=rel,
                        status="same",
                        workspace_hash=ws_hash,
                        backup_hash=bk_hash,
                        selected=False,  # 默认不选中相同文件
                    )
                )
            else:
                entries.append(
                    FileEntry(
                        rel_path=rel,
                        status="modified",
                        workspace_hash=ws_hash,
                        backup_hash=bk_hash,
                        selected=True,
                    )
                )

        ws_by_hash.setdefault(ws_hash, []).append(rel)

    # 3. 检测重命名：内容相同但路径不同
    path_to_entry = {e.rel_path: e for e in entries}
    for ws_hash, ws_paths in ws_by_hash.items():
        bk_paths = backup_by_hash.get(ws_hash, [])
        if not bk_paths:
            continue
        for wp in ws_paths:
            entry = path_to_entry.get(wp)
            if not entry or entry.status != "new":
                continue
            # 这个"新"文件在备份仓库有相同内容 → 可能是重命名
            for bp in bk_paths:
                if bp != wp and not bk_paths:
                    entry.status = "renamed"
                    entry.old_path = bp
                    entry.selected = True
                    break
                elif bp != wp:
                    entry.status = "renamed"
                    entry.old_path = bp
                    entry.selected = True
                    bk_paths.remove(bp)
                    break

    return entries


# ── Git 操作 ─────────────────────────────────────────────────


def get_git_log(
    repo_path: str | Path,
    since_hash: Optional[str] = None,
) -> list[CommitInfo]:
    """读取工作区的 git 日志，可指定起始 hash"""
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        return []

    args = [
        "git",
        "-C",
        str(repo),
        "log",
        "--format=%H|||%s|||%b",
        "--reverse",
    ]
    if since_hash:
        args.append(f"{since_hash}..HEAD")

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, OSError):
        return []

    commits: list[CommitInfo] = []
    # 先剥离 [PREFIX-N] 或 [PREFIX-NNN] 前缀
    prefix_pattern = re.compile(r"^\[[A-Z]+-\d+\]\s*")
    type_pattern = re.compile(
        r"^(feat|fix|docs|style|refactor|perf|test|chore)"
        r"(?:\(([^)]*)\))?:\s*(.*)"
    )

    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|||", 2)
        if len(parts) < 2:
            continue
        h = parts[0]
        s = parts[1]
        body = parts[2] if len(parts) > 2 else ""

        # 剥离项目前缀再解析
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


def build_commit_template(
    commits: list[CommitInfo], config: Config
) -> str:
    """根据选中的 commit 生成正式 commit message 模板"""
    prefix = config.commit_format.get("prefix", "PROJ")
    number_start = config.commit_format.get("number_start", 0)

    # 提取 type 和 scope
    types = set(c.type for c in commits)
    scopes = set(c.scope for c in commits if c.scope)
    type_str = "/".join(sorted(types)) if len(types) > 1 else next(iter(types))
    scope_str = f"({','.join(sorted(scopes))})" if scopes else ""

    # subject 聚合
    subjects = [c.subject for c in commits]
    if not subjects:
        agg_subject = "update"
    elif len(subjects) == 1:
        agg_subject = subjects[0]
    else:
        agg_subject = f"{subjects[0][:30]} +{len(subjects)-1} more"
        if len(agg_subject) > 50:
            agg_subject = agg_subject[:47] + "..."

    # 寻找最大可用编号
    max_n = _find_next_number(config.backup_path, prefix)
    n = max(max_n, number_start)

    header = f"[{prefix}-{n}] {type_str}{scope_str}: {agg_subject}"

    lines = [header, ""]
    lines.append(f"Project: {config.project_name}")
    lines.append("")

    # 列出原始 commit
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


def _find_next_number(backup_path: str, prefix: str) -> int:
    """从备份仓库的 commit 历史中找到下一个可用的编号"""
    if not backup_path:
        return 0
    bk = Path(backup_path)
    if not (bk / ".git").exists():
        return 0
    try:
        result = subprocess.run(
            ["git", "-C", str(bk), "log", f"--grep=^{prefix}-\\d+", "--format=%s", "--max-count=50"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        max_n = -1
        pat = re.compile(rf"\[{prefix}-(\d+)\]")
        for line in result.stdout.split("\n"):
            m = pat.search(line)
            if m:
                n = int(m.group(1))
                if n > max_n:
                    max_n = n
        return max_n + 1 if max_n >= 0 else 0
    except (subprocess.TimeoutExpired, OSError):
        return 0


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


def sync_to_backup(
    entries: list[FileEntry],
    commit_message: str,
    workspace: str | Path,
    backup: str | Path,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> bool:
    """执行同步：拷贝文件 → git add → git commit"""
    ws = Path(workspace).resolve()
    bk = Path(backup).resolve()
    selected = [e for e in entries if e.selected]

    if not selected:
        if progress_callback:
            progress_callback(0, 0, "没有选中任何文件")
        return False

    total = len(selected)

    # 1. 拷贝文件
    for i, entry in enumerate(selected):
        if progress_callback:
            progress_callback(i, total, f"拷贝 {entry.rel_path}")

        src = ws / entry.rel_path
        dst = bk / entry.rel_path

        if not src.exists():
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        # 大文件分块拷贝
        try:
            with open(src, "rb") as sf, open(dst, "wb") as df:
                while True:
                    chunk = sf.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    df.write(chunk)
        except OSError as e:
            if progress_callback:
                progress_callback(i, total, f"拷贝失败 {entry.rel_path}: {e}")
            return False

    # 2. 备份仓库 git add + commit
    if progress_callback:
        progress_callback(total, total, "正在提交到备份仓库...")

    git_args = ["git", "-C", str(bk)]
    try:
        # add
        add_result = subprocess.run(
            git_args + ["add", "-A"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        if add_result.returncode != 0:
            if progress_callback:
                progress_callback(total, total, f"git add 失败: {add_result.stderr}")
            return False

        # commit
        commit_result = subprocess.run(
            git_args + ["commit", "-m", commit_message],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if commit_result.returncode != 0:
            stderr = commit_result.stderr.strip()
            if "nothing to commit" in stderr:
                if progress_callback:
                    progress_callback(total, total, "没有变更需要提交")
                return True
            if progress_callback:
                progress_callback(total, total, f"git commit 失败: {stderr}")
            return False

        if progress_callback:
            progress_callback(total, total, f"[OK] 提交成功: {commit_result.stdout.strip()}")
        return True

    except (subprocess.TimeoutExpired, OSError) as e:
        if progress_callback:
            progress_callback(total, total, f"Git 操作失败: {e}")
        return False


def get_exclude_patterns(config: Config, workspace: Path) -> list[str]:
    """合并 .gitignore 规则 + force_exclude 规则"""
    patterns = _read_gitignore(workspace)
    patterns.extend(config.force_exclude)
    return patterns
