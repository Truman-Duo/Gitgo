"""Release-boundary privacy regression tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.adapters import LocalFileAdapter, LocalGitRunner
from backend.core.authorship import (
    is_ai_config_file,
    looks_like_private_tool_content,
    scan_files_privacy,
)
from backend.core.llm_config import LLMConfigManager
from backend.core.operations.models import FileEntry
from backend.core.operations.security import _security_scan
from backend.core.operations.sync import push_to_backup, sync_to_backup
from backend.core.policy.gates import load_gates
from backend.core.sync_session.syncpush import SyncPushMixin


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "privacy-tests@example.invalid")
    _git(path, "config", "user.name", "Privacy Tests")


def _commit_all(path: Path, message: str) -> None:
    _git(path, "add", "-A")
    _git(path, "commit", "-m", message)


def test_nested_claude_and_renamed_instruction_content_are_private():
    assert is_ai_config_file("docs/Claude.md")
    assert is_ai_config_file("nested/LLM_CONFIG.JSON")
    renamed_stub = "Project note\n\n详情请见 docs 中真正的 " + "CLAUDE.md"
    assert looks_like_private_tool_content(renamed_stub)
    assert not looks_like_private_tool_content(
        "# Provider protocol report\nThis document compares several APIs."
    )


def test_content_scan_does_not_skip_large_file_or_return_secret(tmp_path_factory):
    fake_key = "sk-" + ("z" * 32)
    target = tmp_path_factory / "large.txt"
    target.write_text(("safe\n" * 300_000) + fake_key, encoding="utf-8")
    alerts = scan_files_privacy(str(tmp_path_factory), ["large.txt"])
    assert any(item["rule"] == "privacy_apikey" for item in alerts)
    assert fake_key not in json.dumps(alerts)
    assert alerts[0].get("matches") is None


def test_content_scan_keeps_real_secrets_but_ignores_reserved_code_literals(
    tmp_path_factory,
):
    source = tmp_path_factory / "source.py"
    source.write_text(
        "api_key = provider.api_key\n"
        "email = 'agent@local.gitgo.invalid'\n"
        "host = '127.0.0.1'\n",
        encoding="utf-8",
    )
    assert scan_files_privacy(str(tmp_path_factory), ["source.py"]) == []

    secret = tmp_path_factory / "secret.txt"
    secret.write_text("sk-" + ("q" * 32), encoding="utf-8")
    alerts = scan_files_privacy(str(tmp_path_factory), ["secret.txt"])
    assert any(item["rule"] == "privacy_apikey" for item in alerts)


def test_content_privacy_exception_is_bound_to_one_redacted_fingerprint(tmp_path_factory):
    first = tmp_path_factory / "first.txt"
    second = tmp_path_factory / "second.txt"
    first.write_text("sk-" + ("a" * 32), encoding="utf-8")
    second.write_text("sk-" + ("b" * 32), encoding="utf-8")
    alerts = scan_files_privacy(str(tmp_path_factory), ["first.txt", "second.txt"])
    approved = alerts[0]["evidence"][0]["sha256"]
    remaining = scan_files_privacy(
        str(tmp_path_factory), ["first.txt", "second.txt"],
        approved_fingerprints=[approved],
    )
    assert len(remaining) == 1
    assert remaining[0]["evidence"][0]["sha256"] != approved


def test_outbound_diff_ignores_source_references_but_not_secret_literals():
    from backend.core.operations.security import scan_diff_for_secrets

    source_reference = (
        "+++ b/tests/example.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+secret = tmp_path_factory / 'secret.txt'\n"
        "+api_key = provider_key\n"
    )
    assert scan_diff_for_secrets(source_reference) == []

    source_literal = (
        "+++ b/src/settings.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+secret = 'this-is-real-secret-material'\n"  # gitgo-ignore-sensitive
    )
    assert any(
        item["rule_id"] == "generic_secret"
        for item in scan_diff_for_secrets(source_literal)
    )

    env_literal = (
        "+++ b/.env\n"
        "@@ -0,0 +1,1 @@\n"
        "+secret=hunter2longvalue\n"  # gitgo-ignore-sensitive
    )
    assert any(
        item["rule_id"] == "generic_secret"
        for item in scan_diff_for_secrets(env_literal)
    )


def test_sync_purges_previously_tracked_and_content_renamed_private_files(
    tmp_path_factory,
):
    workspace = tmp_path_factory / "workspace"
    release = tmp_path_factory / "release"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("print('safe')\n")
    (workspace / "CLAUDE.md").write_text("# CLAUDE.md\nprivate instructions\n")

    _init_repo(release)
    (release / "docs").mkdir()
    (release / "docs" / "CLAUDE.md").write_text("# CLAUDE.md\nold private\n")
    (release / "renamed-guide.md").write_text(
        "详情请见 docs 中真正的 " + "CLAUDE.md\n"
    )
    assert looks_like_private_tool_content(
        (release / "renamed-guide.md").read_bytes()
    )
    _commit_all(release, "seed private files")

    ok = sync_to_backup(
        [FileEntry("src/main.py", "new")], "publish sanitized tree",
        workspace, release,
        ws_adapter=LocalFileAdapter(workspace),
        bk_adapter=LocalFileAdapter(release),
        git_runner=LocalGitRunner(release),
    )
    assert ok is True
    tracked = set(_git(release, "ls-files").splitlines())
    assert "src/main.py" in tracked
    assert "docs/CLAUDE.md" not in tracked
    assert "renamed-guide.md" not in tracked
    assert (workspace / "CLAUDE.md").exists()


def test_sync_hard_blocks_secret_even_when_filename_is_innocent(tmp_path_factory):
    workspace = tmp_path_factory / "workspace-secret"
    release = tmp_path_factory / "release-secret"
    workspace.mkdir()
    _init_repo(release)
    (release / "base.txt").write_text("base\n")
    _commit_all(release, "base")
    fake_key = "sk-" + ("q" * 32)
    (workspace / "innocent.json").write_text(
        json.dumps({"api_key": fake_key}), encoding="utf-8",
    )
    ok = sync_to_backup(
        [FileEntry("innocent.json", "new")], "must block",
        workspace, release,
        ws_adapter=LocalFileAdapter(workspace),
        bk_adapter=LocalFileAdapter(release),
        git_runner=LocalGitRunner(release),
    )
    assert ok is False
    assert not (release / "innocent.json").exists()


def test_push_scans_all_unpushed_commits_and_hard_findings_cannot_be_skipped(
    tmp_path_factory,
):
    remote = tmp_path_factory / "remote.git"
    release = tmp_path_factory / "release-push"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True, text=True)
    _init_repo(release)
    (release / "base.txt").write_text("base\n")
    _commit_all(release, "base")
    _git(release, "branch", "-M", "master")
    _git(release, "remote", "add", "origin", str(remote))
    _git(release, "push", "-u", "origin", "master")

    fake_key = "sk-" + ("r" * 32)
    (release / "first.json").write_text(json.dumps({"api_key": fake_key}))
    _commit_all(release, "first outbound commit contains secret")
    (release / "second.txt").write_text("safe latest commit\n")
    _commit_all(release, "safe latest commit")

    warnings = _security_scan(git_runner=LocalGitRunner(release))
    assert any(item["rule_id"] == "provider_key" for item in warnings)
    assert fake_key not in json.dumps(warnings)
    success, repeated = push_to_backup(
        release, skip_scan=True, git_runner=LocalGitRunner(release),
    )
    assert success is False
    assert repeated
    assert _git(release, "rev-parse", "origin/master") != _git(release, "rev-parse", "HEAD")


def test_publish_remote_configuration_is_materialized_without_logging_url():
    calls = []

    class Runner:
        def run(self, args, timeout=0):
            calls.append((args, timeout))
            if args[:3] == ["remote", "get-url", "origin"]:
                return SimpleNamespace(returncode=0, stdout="https://old.invalid/repo\n")
            return SimpleNamespace(returncode=0, stdout="")

    class Harness(SyncPushMixin):
        project = SimpleNamespace(release=SimpleNamespace(
            remote=SimpleNamespace(url="https://new.invalid/repo"),
        ))
        bk_git_runner = Runner()
        logs = []

        def on_log(self, message):
            self.logs.append(message)

    harness = Harness()
    assert harness._ensure_configured_release_remote()
    assert calls[-1][0] == [
        "remote", "set-url", "origin", "https://new.invalid/repo",
    ]
    assert all("new.invalid" not in message for message in harness.logs)


def test_privacy_gate_is_mandatory_even_when_contract_disables_it(tmp_path_factory):
    contract_dir = tmp_path_factory / ".gitgo"
    contract_dir.mkdir()
    (contract_dir / "contract.yaml").write_text(
        "gates:\n  sync:\n    disabled: [privacy_scan]\n"
        "  push:\n    disabled: [privacy_scan]\n",
        encoding="utf-8",
    )
    for context in ("sync", "push"):
        privacy = [gate for gate in load_gates(context, str(tmp_path_factory))
                   if gate.name == "privacy_scan"]
        assert len(privacy) == 1
        assert privacy[0].fail_action == "block"


def test_legacy_repo_local_provider_config_migrates_to_canonical_user_path(
    tmp_path_factory,
):
    canonical = tmp_path_factory / "user" / "llm_config.json"
    legacy = tmp_path_factory / "repo" / "llm_config.json"
    legacy.parent.mkdir()
    fake_key = "sk-" + ("m" * 32)
    legacy.write_text(json.dumps({
        "providers": [{
            "id": "p", "name": "p", "base_url": "https://example.invalid",
            "api_key": fake_key, "model_id": "m",
        }],
        "active_provider": "p", "failover_enabled": False,
        "failover_order": [],
    }), encoding="utf-8")
    with patch.object(LLMConfigManager, "_config_path", return_value=canonical), \
            patch.object(LLMConfigManager, "_legacy_config_paths", return_value=[legacy]):
        loaded = LLMConfigManager.load()
    assert loaded["providers"][0]["api_key"] == fake_key
    assert canonical.exists()
    assert not legacy.exists()
