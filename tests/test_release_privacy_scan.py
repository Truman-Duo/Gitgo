from pathlib import Path
import subprocess

from scripts.verify_release_privacy import scan_repository


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, text=True)


def test_release_privacy_scan_reports_paths_without_secret_values(tmp_path_factory):
    root = tmp_path_factory / "release"
    root.mkdir()
    _git(root, "init")
    (root / "safe.txt").write_text("public\n", encoding="utf-8")
    (root / "renamed.txt").write_text("sk-" + "q" * 32, encoding="utf-8")
    (root / "state.sqlite3").write_bytes(b"not a real database")
    _git(root, "add", "safe.txt", "renamed.txt", "state.sqlite3")

    findings = scan_repository(root)
    assert ("renamed.txt", "provider_key") in findings
    assert ("state.sqlite3", "forbidden_release_path") in findings
    assert all("sk-" not in rule and "q" * 8 not in path for path, rule in findings)


def test_release_privacy_scan_accepts_safe_tracked_tree(tmp_path_factory):
    root = tmp_path_factory / "safe-release"
    root.mkdir()
    _git(root, "init")
    (root / "README.md").write_text("public release notes\n", encoding="utf-8")
    _git(root, "add", "README.md")
    assert scan_repository(root) == []
