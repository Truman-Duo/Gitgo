"""Create disposable Issue 40 repositories under an explicitly chosen folder.

This creates real Git repositories and two local Git identities. It does not
claim that local identities are authenticated GitHub/GitLab accounts.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone


def create(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "targets.json"
    if manifest.exists():
        raise ValueError("Acceptance targets already exist; choose a new directory")
    names = ("seed", "shared.git", "workspace", "trial", "formal", "collaborator-alice", "collaborator-bob")
    if any((root / name).exists() for name in names):
        raise ValueError("Refusing to replace an existing repository")
    hooks = root / "empty-hooks"
    hooks.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("GIT_", "GCM_"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")

    def git(repo, *args):
        return subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=" + str(hooks),
            "-c", "commit.gpgsign=false", *args], env=env, capture_output=True, check=True,
            text=True, encoding="utf8", **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})).stdout.strip()

    seed = root / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    git(seed, "config", "user.name", "Publish Fixture Maintainer")
    git(seed, "config", "user.email", "maintainer@local.gitgo.invalid")
    (seed / "README.md").write_text("# Issue 40 acceptance fixture\nDisposable local repository.\n", encoding="utf8")
    (seed / "calculator.py").write_text("def total(values):\n    return sum(values)\n", encoding="utf8")
    (seed / "test_calculator.py").write_text("from calculator import total\n\ndef test_total():\n    assert total([1, 2, 3]) == 6\n", encoding="utf8")
    git(seed, "add", "."); git(seed, "commit", "-m", "[TEST-1] feat: acceptance baseline")
    base = git(seed, "rev-parse", "HEAD")
    git(seed, "clone", "--bare", "--no-hardlinks", str(seed), str(root / "shared.git"))
    for name in names[2:]:
        git(root, "clone", "--no-hardlinks", str(root / "shared.git"), str(root / name))
        actor = "alice" if name.endswith("alice") else "bob" if name.endswith("bob") else "maintainer"
        git(root / name, "config", "user.name", "Publish Fixture " + actor.title())
        git(root / name, "config", "user.email", actor + "@local.gitgo.invalid")
    git(root / "formal", "branch", "formal", base)
    (root / "workspace" / "feature.txt").write_text("Local output for Formal integration.\n", encoding="utf8")
    git(root / "workspace", "add", "feature.txt")
    git(root / "workspace", "commit", "-m", "[TEST-2] feat: local output")
    (root / "trial" / "contribution.txt").write_text("Contribution requiring whitespace repair.   \n", encoding="utf8")
    git(root / "trial", "add", "contribution.txt")
    git(root / "trial", "commit", "-m", "feat: incoming contribution")
    data = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root), "repositories": {name: str(root / name) for name in names}, "base_oid": base,
        "workspace_oid": git(root / "workspace", "rev-parse", "HEAD"),
        "trial_oid": git(root / "trial", "rev-parse", "HEAD"),
        "local_participants": ["alice", "bob"], "platform_accounts_verified": False,
        "github": {"state": "not_provisioned"}, "gitlab": {"state": "not_provisioned"}}
    manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
    (root / "README.md").write_text("# Gitgo Issue 40 验收环境\n\n"
        "此目录由用户授权创建。Workspace、Trial、Formal 为独立真实 Git 仓库，shared.git 为 bare 远端。\n\n"
        "collaborator-alice 与 collaborator-bob 使用两个本地 Git 身份，不能代替平台双账户验收。"
        "平台环境状态记录在 targets.json；运行证据应保存在本目录。\n\n"
        "不包含用户项目内容或凭据；不会修改原产品工作树的 Git refs。\n", encoding="utf8")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    print(json.dumps(create(args.root), ensure_ascii=True))
