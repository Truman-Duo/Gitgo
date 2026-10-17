"""Real temporary Git history tests; these are not OS sandbox evidence."""
import json
from pathlib import Path
import subprocess

import pytest

from scripts.audit_sandbox_candidate import audit, main


STAMP = "2026-10-10T00:23:21+08:00"


def run(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   text=True, encoding="utf-8").strip()


def commit(repo, title):
    run(repo, "add", "-A")
    run(repo, "commit", "-m", title)
    return run(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path_factory):
    tmp_path = tmp_path_factory
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "init", "-b", "master")
    run(repo, "config", "user.name", "Fixture")
    run(repo, "config", "user.email", "fixture@example.invalid")
    run(repo, "config", "commit.gpgsign", "false")
    run(repo, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (repo / "commit-config.json").write_text(json.dumps({
        "validTypes": ["fix", "docs", "test"],
        "validScopes": ["security", "docs", "tests"],
        "maxSubjectLength": 60,
    }), encoding="utf-8")
    base = commit(repo, "docs(docs): establish fixture baseline")
    (repo / "source.py").write_text("x = 1", encoding="utf-8")
    head = commit(repo, "fix(security): establish candidate")
    return repo, base, head


def test_aligned_clean_candidate_does_not_claim_tests(history):
    repo, base, head = history
    report = audit(repo, base, STAMP)
    assert report["candidate_head"] == head
    assert report["merge_base"] == base
    assert report["integration_candidate_tree"] == run(repo, "rev-parse", "HEAD^{tree}")
    assert report["alignment_format_blockers"] == []
    assert report["tested_integration_tree"] is None
    assert report["test_evidence"] == []
    assert report["sandbox_acceptance"] == "not_asserted"


def test_dirty_content_cannot_use_committed_tree_as_validated_worktree(history):
    repo, base, _ = history
    (repo / "source.py").write_text("x = 2", encoding="utf-8")
    (repo / "private-user-file.txt").write_text("private", encoding="utf-8")
    report = audit(repo, base, STAMP)
    assert "WORKTREE_NOT_CLEAN" in report["alignment_format_blockers"]
    assert "private-user-file" not in json.dumps(report)


def test_new_upstream_requires_alignment(history):
    repo, base, head = history
    run(repo, "checkout", "-b", "upstream", base)
    (repo / "upstream.py").write_text("x = 3", encoding="utf-8")
    upstream = commit(repo, "fix(security): change upstream contract")
    run(repo, "checkout", "master")
    report = audit(repo, upstream, STAMP)
    assert report["candidate_head"] == head
    assert report["merge_base"] == base
    assert report["integration_candidate_tree"] is None
    assert "UPSTREAM_NOT_INCORPORATED" in report["alignment_format_blockers"]


def test_squash_discloses_historical_debt_without_rewriting(history):
    repo, base, _ = history
    (repo / "source.py").write_text("x = 4", encoding="utf-8")
    bad = commit(repo, "[GITGO-80] fix(tools): old scope")
    (repo / "source.py").write_text("x = 5", encoding="utf-8")
    head = commit(repo, "fix(security): valid contribution title")
    report = audit(repo, base, STAMP)
    assert report["historical_format_errors"] == [{"sha": bad, "errors": ["scope"]}]
    assert report["numbered_contributions_require_reallocation"] == [{"sha": bad, "number": 80}]
    assert not report["alignment_format_blockers"]
    assert run(repo, "rev-parse", "HEAD") == head
    merge_report = audit(repo, base, STAMP, integration_mode="merge")
    assert "HISTORY_REQUIRES_MAINTAINER_SQUASH" in merge_report["alignment_format_blockers"]


@pytest.mark.parametrize("title,error", [
    ("fix(tools): bad scope", "scope"),
    ("feat(security): invalid fixture type", "type"),
    ("fix(security): " + "a" * 61, "subject_length"),
    ("fix(security): trailing period.", "trailing_period"),
    ("missing scope", "format"),
])
def test_bad_head_blocks_even_squash(history, title, error):
    repo, base, _ = history
    (repo / "source.py").write_text("x = 6", encoding="utf-8")
    commit(repo, title)
    report = audit(repo, base, STAMP)
    assert error in report["head_format_errors"]
    assert "CANDIDATE_COMMIT_FORMAT_INVALID" in report["alignment_format_blockers"]


def test_selected_candidate_must_match_checkout(history):
    repo, base, head = history
    report = audit(repo, base, STAMP, candidate=base)
    assert "CHECKOUT_DOES_NOT_MATCH_CANDIDATE" in report["alignment_format_blockers"]
    assert run(repo, "rev-parse", "HEAD") == head


def test_format_rules_come_from_candidate_not_dirty_config(history):
    repo, base, _ = history
    (repo / "source.py").write_text("x = 7", encoding="utf-8")
    commit(repo, "fix(tools): prohibited scope")
    config = json.loads((repo / "commit-config.json").read_text(encoding="utf-8"))
    config["validScopes"].append("tools")
    (repo / "commit-config.json").write_text(json.dumps(config), encoding="utf-8")
    report = audit(repo, base, STAMP)
    assert report["head_format_errors"] == ["scope"]
    assert "WORKTREE_NOT_CLEAN" in report["alignment_format_blockers"]


@pytest.mark.parametrize("upstream,stamp", [("HEAD", STAMP), ("a" * 40, "2026-10-10T00:23:21")])
def test_rejects_unidentified_upstream_or_naive_observation(history, upstream, stamp):
    repo, _, _ = history
    with pytest.raises(ValueError):
        audit(repo, upstream, stamp)


def test_cli_failure_is_nonzero_and_does_not_expose_private_paths(history, capsys):
    repo, base, _ = history
    (repo / "secret.txt").write_text("secret", encoding="utf-8")
    assert main(["--repo", str(repo), "--observed-upstream", base,
                 "--observed-at", STAMP]) == 2
    report = json.loads(capsys.readouterr().out)
    assert "WORKTREE_NOT_CLEAN" in report["alignment_format_blockers"]
    assert "secret.txt" not in json.dumps(report)
