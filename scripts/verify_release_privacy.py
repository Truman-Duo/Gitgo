"""Fail-closed privacy check for a Git release candidate.

Only paths and rule identifiers are reported. Matched content is never printed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


FORBIDDEN_NAMES = {
    "claude.md",
    "agents.md",
    "handoff.md",
    ".env",
    "llm_config.json",
    "provider_secrets.json",
}
FORBIDDEN_SUFFIXES = {
    ".sqlite",
    ".sqlite3",
    ".db",
    ".db-wal",
    ".db-shm",
    "-wal",
    "-shm",
}
SECRET_PATTERNS = {
    "provider_key": re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "slack_token": re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "windows_user_path": re.compile(
        rb"[A-Za-z]:\\Users\\(?!Public\\|Default\\|USERNAME\\)[^\\/\s]+",
        re.IGNORECASE,
    ),
}


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def tracked_paths(root: Path) -> list[str]:
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in _git(root, "ls-files", "-z").split(b"\0")
        if item
    ]


def scan_repository(root: Path) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for relative in tracked_paths(root):
        normalized = relative.replace("\\", "/")
        leaf = normalized.rsplit("/", 1)[-1].casefold()
        lower = normalized.casefold()
        if leaf in FORBIDDEN_NAMES or any(lower.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            findings.append((normalized, "forbidden_release_path"))
            continue
        path = root / relative
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size > 64 * 1024 * 1024:
                findings.append((normalized, "unscanned_large_file"))
                continue
            data = path.read_bytes()
        except OSError:
            findings.append((normalized, "unreadable_tracked_file"))
            continue
        if b"\0" in data[:8192]:
            continue
        for rule, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                findings.append((normalized, rule))
    return sorted(set(findings))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    findings = scan_repository(root)
    if findings:
        print("Release privacy check failed. Matched values are intentionally hidden.")
        for path, rule in findings:
            print(f"- {path}: {rule}")
        return 2
    print(f"Release privacy check passed ({len(tracked_paths(root))} tracked files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
