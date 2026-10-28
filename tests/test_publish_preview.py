"""Actual divergent Git graphs, selection semantics and fixed combined results."""
import pytest

from backend.core.config import Config, ConfigManager
from backend.core.publish import formal
from backend.core.publish.git import CandidateGit
from backend.core.loop.git_execution import _arguments, GitBoundaryError
from tests.test_publish_commands import setup, call, git
from tests.test_publish_formal import formal_setup, prepare, command, protocol
from tests.test_native_sandbox import isolated_python
from tests.native_source_runtime import workspace_native_launcher
from tests.test_registered_test_service import pytest_native_runtime


def commit(repo, parent, changes, message="[ANBM-20] feat: isolated change"):
    runner = CandidateGit(repo)
    files = {row[0]: row for row in formal.tree_files(runner, parent)}
    for name, value in changes.items():
        if value is None:
            files.pop(name)
        else:
            raw = value.encode()
            blob = runner.binary(["hash-object", "-w", "--stdin"], raw).decode().strip()
            files[name] = (name, "100644", blob, raw)
    tree = formal.write_tree(runner, list(files.values()))
    return git(repo, "commit-tree", tree, "-p", parent, "-m", message)


def advance_target(repo):
    base = git(repo, "rev-parse", "formal")
    target = commit(repo, base, {"target.txt": "retain target\n"}, "[ANBM-10] fix: target change")
    git(repo, "update-ref", "refs/heads/formal", target, base)
    return target


def integrate(setup, item, monkeypatch, action=None):
    protocol(monkeypatch, action=action)
    checked = command(setup, "verify", item)["item"]
    assert checked["formal_eligible"], [(e["kind"], e["outcome"], e["summary"]) for e in checked["evidence"]]
    result = command(setup, "execute", checked)["item"]
    assert result["status"] == "integrated"
    return result


@pytest.mark.parametrize("method", ["squash", "merge"])
def test_diverged_target_uses_combined_tree_and_preserves_checkout(formal_setup, monkeypatch, method):
    _, _, repo = formal_setup
    target = advance_target(repo)
    head = git(repo, "rev-parse", "HEAD")
    (repo / "dirty.txt").write_text("untouched")
    before = git(repo, "status", "--porcelain")
    item = prepare(formal_setup, method=method)
    assert item["plan"]["tree_oid"] not in {git(repo, "rev-parse", head + "^{tree}"), git(repo, "rev-parse", target + "^{tree}")}
    def check(stage):
        assert (stage / "target.txt").read_text() == "retain target\n"
        assert (stage / "test_release.py").is_file()
    result = integrate(formal_setup, item, monkeypatch, check)
    parents = git(repo, "show", "-s", "--format=%P", "formal").split()
    assert parents == ([target, head] if method == "merge" else [target])
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == before
    assert result["operations"][-1]["plan"]["contributions"][0]["oid"] == head


def test_multiple_repositories_pin_all_candidates_and_test_combination(formal_setup, tmp_path_factory, monkeypatch):
    _, project, repo = formal_setup
    target = advance_target(repo)
    trial = tmp_path_factory / "trial"
    git(repo, "clone", "--no-hardlinks", str(repo), str(trial))
    git(trial, "config", "user.name", "Trial")
    git(trial, "config", "user.email", "trial@example.test")
    base = git(repo, "merge-base", "HEAD", target)
    trial_oid = commit(trial, base, {"trial.txt": "second contribution\n"})
    git(trial, "branch", "incoming", trial_oid)
    project.trial_path = str(trial)
    ConfigManager.save(Config(projects=[project]))
    item = prepare(formal_setup, contributions=[{"source_role": "trial", "source_ref": "refs/heads/incoming"}])
    assert [row["role"] for row in item["plan"]["contributions"]] == ["workspace", "trial"]
    assert item["plan"]["contributions"][1]["oid"] == trial_oid
    later = commit(trial, trial_oid, {"late.txt": "must not leak\n"})
    git(trial, "update-ref", "refs/heads/incoming", later, trial_oid)
    def check(stage):
        assert (stage / "trial.txt").is_file() and (stage / "target.txt").is_file()
        assert (stage / "test_release.py").is_file() and not (stage / "late.txt").exists()
    integrate(formal_setup, item, monkeypatch, check)


def test_conflict_is_saved_and_can_be_handed_to_owned_work_branch(formal_setup):
    _, _, repo = formal_setup
    head, target = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "formal")
    diverged = commit(repo, target, {"test_release.py": "conflicting file\n"})
    git(repo, "update-ref", "refs/heads/formal", diverged, target)
    item = prepare(formal_setup, rework="Resolve conflict, then check the combined tree")
    assert item["status"] == "conflicted" and not item["formal_eligible"]
    assert item["plan"]["conflict_paths"] == ["test_release.py"]
    assert not {"verify", "execute", "run"} & set(item["allowed_actions"])
    assert command(formal_setup, "execute", item)["error"] == "PUBLISH_ACTION_UNAVAILABLE"
    assert git(repo, "rev-parse", "formal") == diverged
    work = call(formal_setup, "prepare", "take-for-repair", destination="work", item_id=item["id"],
        expected_revision=item["revision"], source_role="workspace", source_ref=head,
        target_branch="repair/conflict", owner="repair-owner", rework="Resolve the recorded merge conflict")["item"]
    adopted = command(formal_setup, "adopt", work)["item"]
    assert adopted["status"] == "adopted" and adopted["owner"] == "repair-owner"
    assert adopted["handoffs"][0]["plan"]["conflict_paths"] == ["test_release.py"]
    assert any(e["kind"] == "merge_preview" and e["outcome"] == "failed" and not e["current"] for e in adopted["evidence"])
    assert git(repo, "rev-parse", "repair/conflict") == head
    assert git(repo, "rev-parse", "formal") == diverged


@pytest.mark.parametrize("method", ["rebase", "cherry_pick"])
def test_replayed_commits_have_fixed_trees_and_numbered_messages(formal_setup, monkeypatch, method):
    _, _, repo = formal_setup
    first = git(repo, "rev-parse", "HEAD")
    second = commit(repo, first, {"second.txt": "second\n"})
    target = advance_target(repo)
    item = prepare(formal_setup, source_ref=second, method=method,
        **({"selected_commits": [first, second]} if method == "cherry_pick" else {}))
    assert [step["source_oid"] for step in item["plan"]["commit_series"]] == [first, second]
    assert item["plan"]["commit_series"][0]["number"] < item["plan"]["commit_series"][1]["number"]
    integrate(formal_setup, item, monkeypatch)
    result = git(repo, "rev-parse", "formal")
    assert git(repo, "rev-parse", result + "~2") == target
    assert git(repo, "show", result + ":target.txt") == "retain target"
    assert git(repo, "show", result + ":second.txt") == "second"
    assert git(repo, "show", "-s", "--format=%B", result + "~1") == item["plan"]["commit_series"][0]["message"]


def test_cherry_pick_excludes_unselected_commit(formal_setup, monkeypatch):
    _, _, repo = formal_setup
    first = git(repo, "rev-parse", "HEAD")
    second = commit(repo, first, {"unselected.txt": "leave out\n"})
    third = commit(repo, second, {"selected.txt": "include\n"})
    advance_target(repo)
    item = prepare(formal_setup, source_ref=third, method="cherry_pick", selected_commits=[first, third])
    def check(stage):
        assert (stage / "selected.txt").is_file() and not (stage / "unselected.txt").exists()
    integrate(formal_setup, item, monkeypatch, check)
    assert [step["source_oid"] for step in item["plan"]["commit_series"]] == [first, third]


def test_partial_path_changes_preserve_other_target_content_and_deletions(formal_setup, monkeypatch):
    _, _, repo = formal_setup
    base = git(repo, "rev-parse", "formal")
    # An existing baseline file makes deletion observable, not an omitted add.
    existing = formal.tree_files(CandidateGit(repo), base)[0][0]
    source = commit(repo, git(repo, "rev-parse", "HEAD"), {existing: None, "unselected.txt": "skip\n", "selected.txt": "yes\n"})
    target = advance_target(repo)
    paths = [existing, "selected.txt", "test_release.py"]
    item = prepare(formal_setup, source_ref=source, method="partial", paths=paths)
    def check(stage):
        assert not (stage / existing).exists() and not (stage / "unselected.txt").exists()
        assert (stage / "selected.txt").read_text() == "yes\n"
        assert (stage / "target.txt").is_file()
    integrate(formal_setup, item, monkeypatch, check)
    assert git(repo, "rev-parse", "formal^1") == target


def test_rebase_rejects_merge_commits_and_cherry_pick_rejects_unknown_objects(formal_setup):
    _, _, repo = formal_setup
    head = git(repo, "rev-parse", "HEAD")
    side = commit(repo, git(repo, "rev-parse", "formal"), {"side.txt": "side\n"})
    merged = git(repo, "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"), "-p", head, "-p", side, "-m", "merge")
    rejected = call(formal_setup, "prepare", "nonlinear", destination="formal", method="rebase", source_role="workspace",
        source_ref=merged, target_branch="formal", owner="owner", test_target="test_release.py")
    assert rejected["error"] == "PUBLISH_NONLINEAR_SELECTION"
    rejected = call(formal_setup, "prepare", "unknown-pick", destination="formal", method="cherry_pick", source_role="workspace",
        source_ref=head, target_branch="formal", owner="owner", test_target="test_release.py", selected_commits=[side])
    assert rejected["error"] == "PUBLISH_INVALID_SELECTION"


def test_retained_merge_history_cannot_hide_private_ancestor(formal_setup):
    _, _, repo = formal_setup
    head = git(repo, "rev-parse", "HEAD")
    private = commit(repo, head, {"CLAUDE.md": "private tool configuration\n"})
    clean = commit(repo, private, {"CLAUDE.md": None}, "[ANBM-21] chore: remove file")
    rejected = call(formal_setup, "prepare", "private-history", destination="formal", method="merge", source_role="workspace",
        source_ref=clean, target_branch="formal", owner="owner", test_target="test_release.py")
    assert rejected["error"] == "PUBLISH_FF_REQUIRES_FILTERING"


def test_custom_merge_driver_is_not_run(formal_setup, tmp_path_factory):
    _, _, repo = formal_setup
    marker = tmp_path_factory / "unexpected.txt"
    git(repo, "config", "merge.untrusted.driver", "echo invoked > " + str(marker))
    rejected = call(formal_setup, "prepare", "driver", destination="formal", method="squash", source_role="workspace",
        source_ref="HEAD", target_branch="formal", owner="owner", test_target="test_release.py")
    assert rejected["error"] == "GIT_REPOSITORY_EXTENSION_UNSUPPORTED"
    assert not marker.exists()


@pytest.mark.parametrize("arguments", [
    ["merge-tree", "--write-tree", "HEAD", "main"],
    ["merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", "-Xours", "a" * 40, "b" * 40],
    ["commit-tree", "a" * 40, "-p", "HEAD", "-m", "message"],
])
def test_merge_boundary_accepts_only_fixed_closed_commands(arguments):
    with pytest.raises(GitBoundaryError):
        _arguments(["git", *arguments])


def test_additional_trial_repository_is_in_agent_permission_scope(formal_setup, tmp_path_factory):
    from backend.core.tools.publish_service import build_publish_tools
    from backend.core.loop.permission_broker import out_of_scope_resources
    _, project, repo = formal_setup
    project.trial_path = str(tmp_path_factory / "separate-trial")
    ConfigManager.save(Config(projects=[project]))
    prepared = build_publish_tools(project)["publish_prepare"].prepare_args({"destination": "formal", "source_role": "workspace",
        "contributions": [{"source_role": "trial", "source_ref": "HEAD"}]})
    assert str(tmp_path_factory / "separate-trial") in out_of_scope_resources(prepared, str(repo))


def test_native_runtime_tests_actual_multi_candidate_combination(formal_setup, pytest_native_runtime):
    _, _, repo = formal_setup
    target = advance_target(repo)
    primary = commit(repo, git(repo, "rev-parse", "HEAD"), {"test_release.py": """from pathlib import Path
def test_combined():
    assert Path('target.txt').read_text() == 'retain target\\n'
    assert Path('peer.txt').read_text() == 'from peer\\n'
    assert not Path('dirty.txt').exists()
"""})
    peer = commit(repo, git(repo, "merge-base", primary, target), {"peer.txt": "from peer\n"})
    item = prepare(formal_setup, source_ref=primary, contributions=[{"source_role": "workspace", "source_ref": peer}])
    (repo / "dirty.txt").write_text("private")
    checked = command(formal_setup, "verify", item)["item"]
    assert checked["formal_eligible"], [(e["kind"], e["summary"]) for e in checked["evidence"]]
    assert "1 passed" in checked["evidence"][-1]["summary"]
    integrated = command(formal_setup, "execute", checked)["item"]
    assert integrated["status"] == "integrated"
    assert git(repo, "rev-parse", "formal^1") == target
    assert (repo / "dirty.txt").read_text() == "private"


def test_multiple_candidate_change_invalidates_combination_evidence(formal_setup, monkeypatch):
    _, _, repo = formal_setup
    peer = commit(repo, git(repo, "rev-parse", "formal"), {"peer.txt": "first\n"})
    item = prepare(formal_setup, contributions=[{"source_role": "workspace", "source_ref": peer}])
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    assert checked["formal_eligible"]
    next_peer = commit(repo, peer, {"peer.txt": "second\n"})
    changed = prepare(formal_setup, request="new-combination", item_id=checked["id"], expected_revision=checked["revision"],
        contributions=[{"source_role": "workspace", "source_ref": next_peer}])
    assert changed["binding"] != checked["binding"] and not changed["formal_eligible"]
    assert any(e["kind"] == "project_test" and not e["current"] for e in changed["evidence"])
    assert command(formal_setup, "execute", changed)["error"] == "PUBLISH_CHECKS_REQUIRED"
