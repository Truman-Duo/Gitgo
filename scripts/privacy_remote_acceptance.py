"""Disposable real-remote acceptance for Gitgo's outbound privacy boundary.

The script creates a unique branch, proves that an innocent filename carrying
a fake provider key is blocked, proves that filename- and content-identified
private artifacts are purged before publish, verifies the remote commit, then
deletes only the unique test branch.  It never prints matched content.
"""

from __future__ import annotations

import argparse
import atexit
import json
import subprocess
import tempfile
import time
from pathlib import Path

from backend.adapters import LocalFileAdapter, LocalGitRunner
from backend.core.operations.models import FileEntry
from backend.core.operations.security import _security_scan
from backend.core.operations.sync import push_to_backup, sync_to_backup


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check,
        capture_output=True, text=True, encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote", required=True)
    args = parser.parse_args()
    branch = f"gitgo-privacy-e2e-{int(time.time())}"
    pushed = False
    report = {"branch": branch, "secret_blocked": False,
              "private_artifacts_purged": False, "remote_verified": False,
              "cleanup": "not-needed"}

    with tempfile.TemporaryDirectory(prefix="gitgo-privacy-remote-") as raw:
        root = Path(raw).resolve()
        workspace = root / "workspace"
        release = root / "release"
        workspace.mkdir()
        clone = subprocess.run(
            ["git", "clone", "--quiet", args.remote, str(release)],
            check=False, capture_output=True, text=True, encoding="utf-8",
        )
        if clone.returncode != 0:
            detail = (clone.stderr or clone.stdout or "unknown git error").strip()[-800:]
            raise RuntimeError(f"remote clone failed: {detail}")
        def emergency_cleanup() -> None:
            if pushed and release.exists():
                git(release, "push", "origin", "--delete", branch, check=False)

        atexit.register(emergency_cleanup)
        git(release, "config", "user.email", "privacy-e2e@gitgo.invalid")
        git(release, "config", "user.name", "Gitgo Privacy E2E")
        git(release, "checkout", "-b", branch)

        seeded = release / ".gitgo-privacy-e2e"
        seeded.mkdir()
        (seeded / "CLAUDE.md").write_text("# private harness instructions\n", encoding="utf-8")
        (seeded / "renamed-note.txt").write_text(
            "See the real CLAUDE.md for private instructions.\n",
            encoding="utf-8",
        )
        git(release, "add", "-A")
        git(release, "commit", "-m", "seed disposable private artifacts")

        (workspace / "safe.txt").write_text("safe outbound content\n", encoding="utf-8")
        (workspace / "CLAUDE.md").write_text("# private local instructions\n", encoding="utf-8")
        (workspace / "renamed-note.txt").write_text(
            "See the real CLAUDE.md for private instructions.\n",
            encoding="utf-8",
        )
        (workspace / "innocent.json").write_text(
            json.dumps({"api_key": "sk-" + "q" * 32}), encoding="utf-8",
        )

        adapters = {
            "workspace": LocalFileAdapter(workspace),
            "release": LocalFileAdapter(release),
            "git": LocalGitRunner(release),
        }
        blocked = sync_to_backup(
            [FileEntry("innocent.json", "new")], "must be blocked",
            workspace, release, ws_adapter=adapters["workspace"],
            bk_adapter=adapters["release"], git_runner=adapters["git"],
        )
        report["secret_blocked"] = blocked is False and not (release / "innocent.json").exists()
        if not report["secret_blocked"]:
            raise RuntimeError("fake secret was not blocked")

        published = sync_to_backup(
            [FileEntry("safe.txt", "new"), FileEntry("CLAUDE.md", "new"),
             FileEntry("renamed-note.txt", "new")],
            "publish sanitized disposable tree", workspace, release,
            ws_adapter=adapters["workspace"], bk_adapter=adapters["release"],
            git_runner=adapters["git"],
        )
        if not published:
            raise RuntimeError("sanitized sync failed")
        warnings = _security_scan(git_runner=adapters["git"])
        tracked = set(git(release, "ls-files").stdout.splitlines())
        report["private_artifacts_purged"] = (
            not warnings
            and "safe.txt" in tracked
            and not any(path.casefold().endswith("claude.md") for path in tracked)
            and ".gitgo-privacy-e2e/renamed-note.txt" not in tracked
            and "renamed-note.txt" not in tracked
        )
        if not report["private_artifacts_purged"]:
            diagnostic = {
                "warning_rule_ids": sorted({str(item.get("rule_id") or "") for item in warnings}),
                "safe_tracked": "safe.txt" in tracked,
                "claude_path_count": sum(path.casefold().endswith("claude.md") for path in tracked),
                "seeded_rename_tracked": ".gitgo-privacy-e2e/renamed-note.txt" in tracked,
                "workspace_rename_tracked": "renamed-note.txt" in tracked,
            }
            raise RuntimeError(
                "private artifacts remained in the release tree: "
                + json.dumps(diagnostic, sort_keys=True)
            )

        git(release, "config", "push.default", "current")
        push_progress: list[str] = []
        success, push_warnings = push_to_backup(
            release, git_runner=adapters["git"],
            progress_callback=lambda _done, _total, message: push_progress.append(str(message)),
        )
        if not success or push_warnings:
            raise RuntimeError(
                "sanitized push failed: " + json.dumps({
                    "warning_rule_ids": sorted({str(item.get("rule_id") or "") for item in push_warnings}),
                    "last_progress": push_progress[-1][-500:] if push_progress else "",
                }, ensure_ascii=False, sort_keys=True)
            )
        pushed = True
        local_head = git(release, "rev-parse", "HEAD").stdout.strip()
        remote_line = subprocess.run(
            ["git", "ls-remote", args.remote, f"refs/heads/{branch}"],
            check=True, capture_output=True, text=True, encoding="utf-8",
        ).stdout.strip()
        report["remote_verified"] = remote_line.startswith(local_head + "\t")
        if not report["remote_verified"]:
            raise RuntimeError("remote branch did not match the sanitized commit")

        try:
            if pushed:
                deleted = git(release, "push", "origin", "--delete", branch, check=False)
                report["cleanup"] = "deleted" if deleted.returncode == 0 else "failed"
                if deleted.returncode == 0:
                    pushed = False
        finally:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["cleanup"] in {"not-needed", "deleted"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
