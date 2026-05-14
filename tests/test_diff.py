"""测试 get_diff_summary — 变更文件轻量统计摘要"""
from __future__ import annotations

import subprocess
from pathlib import Path

from backend.core.operations.diff import get_diff_summary
from backend.adapters import LocalGitRunner


def make_commit(repo: Path, filename: str, content: str, msg: str) -> str:
    """在 repo 中创建/修改文件并 git commit，返回 commit hash。"""
    f = repo / filename
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, capture_output=True)
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                      capture_output=True, text=True)
    return r.stdout.strip()


def test_diff_summary_new_file(git_repo: Path):
    """新文件的 diff_summary：status=new，无 removed，至少一个顶层符号。"""
    runner = LocalGitRunner(git_repo)
    h = make_commit(git_repo, "src/main.py",
                   "class App:\n    def run(self):\n        pass\n",
                   "feat: add App class")
    result = get_diff_summary(h, runner)
    assert len(result) >= 1
    main_file = [f for f in result if f["path"] == "src/main.py"]
    assert len(main_file) == 1
    f = main_file[0]
    assert f["status"] == "new"
    assert f["added"] > 0
    assert f["removed"] == 0
    assert "App" in f["top_level_symbols"]
    assert "run" in f["top_level_symbols"]


def test_diff_summary_modified_file(git_repo: Path):
    """修改已有文件：status=modified，有 added 和/或 removed。"""
    runner = LocalGitRunner(git_repo)
    # 先创建一个文件
    make_commit(git_repo, "src/utils.py",
               "def foo():\n    return 1\n",
               "feat: add utils")
    # 再修改它
    h = make_commit(git_repo, "src/utils.py",
                   "def foo():\n    return 2\n\ndef bar():\n    return 3\n",
                   "feat: extend utils")
    result = get_diff_summary(h, runner)
    utils = [f for f in result if f["path"] == "src/utils.py"]
    assert len(utils) == 1
    f = utils[0]
    assert f["status"] == "modified"
    assert f["added"] > 0
    assert "bar" in f["top_level_symbols"]


def test_diff_summary_multiple_files(git_repo: Path):
    """一个 commit 含多个文件变更。"""
    runner = LocalGitRunner(git_repo)
    # 创建含多个文件的 commit
    (git_repo / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
    (git_repo / "b.py").write_text("def b_func():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: multiple files"],
                   cwd=git_repo, capture_output=True)
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo,
                      capture_output=True, text=True)
    h = r.stdout.strip()

    result = get_diff_summary(h, runner)
    paths = {f["path"] for f in result}
    assert "a.py" in paths
    assert "b.py" in paths


def test_diff_summary_empty_repo(git_repo: Path):
    """只有 initial commit 时，get_diff_summary 能正常工作。"""
    runner = LocalGitRunner(git_repo)
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo,
                      capture_output=True, text=True)
    h = r.stdout.strip()
    # initial commit 可能没有 parent，get_diff_summary 会尝试用 ^
    # 应优雅降级
    result = get_diff_summary(h, runner)
    # 至少不抛异常
    assert isinstance(result, list)


def test_diff_summary_symbol_limit(git_repo: Path):
    """顶层符号超过 10 个时截断。"""
    runner = LocalGitRunner(git_repo)
    lines = []
    for i in range(15):
        lines.append(f"def func_{i:02d}():\n    pass\n")
    content = "\n".join(lines)
    h = make_commit(git_repo, "src/many_funcs.py", content, "feat: many funcs")
    result = get_diff_summary(h, runner)
    f = [x for x in result if x["path"] == "src/many_funcs.py"][0]
    assert len(f["top_level_symbols"]) <= 10
