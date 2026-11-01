"""Owned branch retention, archive-before-delete and exact remote recovery."""
import pytest

from backend.core.publish.git import CandidateGit
from backend.core.publish.remote_git import RemoteGit
from backend.core.publish.state import read_ledger
from tests.test_publish_commands import setup, prepare, adopt, call, git


@pytest.fixture
def managed(setup):
    prepared = prepare(setup, target_branch="work/owned")
    assert "item" in prepared, prepared
    item = prepared["item"]
    return setup, adopt(setup, item)["item"]


def plan(setup, item, action, request=None, **kwargs):
    return call(setup, "branch", request or action + str(item["revision"]), item_id=item["id"],
        expected_revision=item["revision"], action=action, owner="maintainer", reason="Keep a recoverable review record", **kwargs)


def apply(setup, item, request="apply-branch"):
    return call(setup, "branch.apply", request, item_id=item["id"], expected_revision=item["revision"])


def test_source_and_work_retention_are_explicit_and_do_not_change_git(managed):
    setup, item = managed
    before = git(setup[2], "show-ref")
    source = plan(setup, item, "retain_source")["item"]
    retained = plan(setup, source, "retain_work")["item"]
    assert [row["kind"] for row in retained["branches"]["retention"]] == ["source", "work"]
    assert retained["branches"]["retention"][0]["ref"] == git(setup[2], "symbolic-ref", "HEAD")
    assert all(row["owner"] == "maintainer" and row["reason"] for row in retained["branches"]["retention"])
    assert git(setup[2], "show-ref") == before


def test_local_delete_previews_then_fixes_archive_before_conditional_delete(managed):
    setup, item = managed
    oid = git(setup[2], "rev-parse", "work/owned")
    head = git(setup[2], "rev-parse", "HEAD")
    prepared = plan(setup, item, "prepare_delete")["item"]
    branch_plan = prepared["branches"]["plan"]
    assert branch_plan["expected_oid"] == oid
    assert CandidateGit(setup[2]).target(branch_plan["archive_ref"]) is None
    assert CandidateGit(setup[2]).target("refs/heads/work/owned")["oid"] == oid
    result = apply(setup, prepared)
    assert result["item"]["branches"]["plan"]["state"] == "deleted", result
    assert CandidateGit(setup[2]).target("refs/heads/work/owned") is None
    assert git(setup[2], "rev-parse", branch_plan["archive_ref"]) == oid
    assert git(setup[2], "rev-parse", "HEAD") == head
    assert apply(setup, prepared) == result


def test_archive_operation_retains_branch_then_delete_reuses_fixed_archive(managed):
    setup, item = managed
    prepared = plan(setup, item, "prepare_archive")["item"]
    archived = apply(setup, prepared, "archive-only")["item"]
    assert archived["branches"]["plan"]["state"] == "archived"
    archive = archived["branches"]["plan"]["archive_ref"]
    assert git(setup[2], "rev-parse", "work/owned") == git(setup[2], "rev-parse", archive)
    deletion = plan(setup, archived, "prepare_delete")["item"]
    assert deletion["branches"]["plan"]["archive_ref"] == archive
    assert apply(setup, deletion, "delete-after-archive")["item"]["branches"]["plan"]["state"] == "deleted"


def test_reused_branch_name_with_unrelated_history_loses_ownership(managed):
    setup, item = managed
    unrelated = git(setup[2], "commit-tree", git(setup[2], "rev-parse", "HEAD^{tree}"), "-m", "unrelated reused name")
    git(setup[2], "update-ref", "refs/heads/work/owned", unrelated, item["candidate_oid"])
    result = plan(setup, item, "prepare_delete")
    assert result["error"] == "PUBLISH_BRANCH_OWNERSHIP_CHANGED"
    assert git(setup[2], "rev-parse", "work/owned") == unrelated


@pytest.mark.parametrize("name", ["main", "master", "trunk", "gitgo/publish-metadata-v1", "foreign"])
def test_lifecycle_cannot_touch_protected_or_foreign_branches(managed, name):
    setup, item = managed
    before = git(setup[2], "show-ref")
    result = plan(setup, item, "prepare_delete", branch=name)
    assert result["error"] in {"PUBLISH_BRANCH_PROTECTED", "PUBLISH_BRANCH_NOT_OWNED"}
    assert git(setup[2], "show-ref") == before


def test_checked_out_linked_worktree_branch_cannot_be_deleted(managed, tmp_path_factory):
    setup, item = managed
    linked = tmp_path_factory / "linked-worktree"
    git(setup[2], "worktree", "add", str(linked), "work/owned")
    result = plan(setup, item, "prepare_delete")
    assert result["error"] == "PUBLISH_TARGET_CHECKED_OUT"
    assert git(linked, "rev-parse", "HEAD") == item["candidate_oid"]


def test_changed_local_tip_does_not_archive_or_delete_another_revision(managed):
    setup, item = managed
    prepared = plan(setup, item, "prepare_delete")["item"]
    old = item["candidate_oid"]
    newer = git(setup[2], "commit-tree", git(setup[2], "rev-parse", old + "^{tree}"), "-p", old, "-m", "next revision")
    git(setup[2], "update-ref", "refs/heads/work/owned", newer, old)
    result = apply(setup, prepared)
    assert result["error"] == "PUBLISH_TARGET_CHANGED"
    assert git(setup[2], "rev-parse", "work/owned") == newer
    assert CandidateGit(setup[2]).target(prepared["branches"]["plan"]["archive_ref"]) is None


def test_lost_local_delete_ack_recovers_without_replaying_any_write(managed, monkeypatch):
    setup, item = managed
    prepared = plan(setup, item, "prepare_delete")["item"]
    original = CandidateGit.run
    def lose(self, args):
        result = original(self, args)
        if args[0] == "update-ref" and "-d" in args:
            assert result.returncode == 0
            raise OSError("lost deletion acknowledgement")
        return result
    monkeypatch.setattr(CandidateGit, "run", lose)
    pending = apply(setup, prepared)
    assert pending["effect_state"] == "ambiguous" and pending["item"]["branches"]["plan"]["state"] == "pending"
    def read_only(self, args):
        assert args[0] != "update-ref", "reconciliation cannot replay a ref update"
        return original(self, args)
    monkeypatch.setattr(CandidateGit, "run", read_only)
    restored = plan(setup, pending["item"], "reconcile")["item"]
    assert restored["branches"]["plan"]["state"] == "deleted"
    assert restored["branches"]["plan"]["archive_retained"]


def test_local_delete_cas_refuses_tip_race_after_preflight(managed, monkeypatch):
    setup, item = managed
    prepared = plan(setup, item, "prepare_delete")["item"]
    newer = git(setup[2], "commit-tree", git(setup[2], "rev-parse", "HEAD^{tree}"), "-p", item["candidate_oid"], "-m", "race advance")
    original = CandidateGit.run
    def race(self, args):
        if args[0] == "update-ref" and "-d" in args:
            git(setup[2], "update-ref", "refs/heads/work/owned", newer, item["candidate_oid"])
        return original(self, args)
    monkeypatch.setattr(CandidateGit, "run", race)
    result = apply(setup, prepared)
    assert result["error"] == "PUBLISH_BRANCH_RESULT_CONFLICT"
    assert result["item"]["branches"]["plan"]["archive_retained"]
    assert git(setup[2], "rev-parse", "work/owned") == newer


@pytest.fixture
def remote(managed, tmp_path_factory):
    setup, item = managed
    bare = tmp_path_factory / "branches.git"
    git(setup[2], "init", "--bare", str(bare))
    git(setup[2], "remote", "add", "review", str(bare))
    return setup, item, bare


def published(remote):
    setup, item, bare = remote
    prepared = plan(setup, item, "prepare_publish", remote="review")["item"]
    result = apply(setup, prepared)
    assert result.get("item", {}).get("branches", {}).get("plan", {}).get("state") == "published", result
    return result["item"]


def test_remote_publication_and_delete_are_fixed_separate_explicit_actions(remote):
    setup, item, bare = remote
    item = published(remote)
    assert git(bare, "for-each-ref", "--format=%(refname)") == "refs/heads/work/owned"
    prepared = plan(setup, item, "prepare_remote_delete", remote="review")["item"]
    assert RemoteGit(setup[2], "review").observe("refs/heads/work/owned")["oid"] == item["candidate_oid"]
    deleted = apply(setup, prepared, "remote-delete")["item"]
    assert deleted["branches"]["plan"]["state"] == "remote_deleted"
    assert RemoteGit(setup[2], "review").observe("refs/heads/work/owned")["oid"] is None
    assert git(setup[2], "rev-parse", deleted["branches"]["plan"]["archive_ref"]) == item["candidate_oid"]
    assert deleted["branches"]["publications"][-1]["state"] == "deleted"
    assert git(setup[2], "rev-parse", "work/owned") == item["candidate_oid"]


def test_remote_delete_refuses_unowned_publication_and_changed_tip(remote):
    setup, item, bare = remote
    transport = RemoteGit(setup[2], "review")
    transport.update(transport.observe("refs/heads/work/owned"), item["candidate_oid"])
    refused = plan(setup, item, "prepare_remote_delete", remote="review")
    assert refused["error"] == "PUBLISH_BRANCH_NOT_PUBLISHED"
    item = published(remote)
    prepared = plan(setup, item, "prepare_remote_delete", remote="review")["item"]
    newer = git(setup[2], "commit-tree", git(setup[2], "rev-parse", "HEAD^{tree}"), "-p", item["candidate_oid"], "-m", "remote advance")
    transport.update(transport.observe("refs/heads/work/owned"), newer)
    result = apply(setup, prepared, "refuse-remote-drift")
    assert result["error"] == "PUBLISH_BRANCH_CHANGED"
    assert git(bare, "rev-parse", "work/owned") == newer


def test_remote_delete_ack_loss_observes_absence_without_second_push(remote, monkeypatch):
    setup, _, _ = remote
    item = published(remote)
    prepared = plan(setup, item, "prepare_remote_delete", remote="review")["item"]
    original = RemoteGit.update
    def lost(self, *args, **kwargs):
        assert original(self, *args, **kwargs).returncode == 0
        raise OSError("lost remote acknowledgement")
    monkeypatch.setattr(RemoteGit, "update", lost)
    pending = apply(setup, prepared, "lost-remote-delete")
    assert pending["effect_state"] == "ambiguous"
    monkeypatch.setattr(RemoteGit, "update", lambda *a, **k: pytest.fail("must not push during recovery"))
    restored = plan(setup, pending["item"], "reconcile")["item"]
    assert restored["branches"]["plan"]["state"] == "remote_deleted"


def test_remote_delete_lease_refuses_tip_race_after_preflight(remote, monkeypatch):
    setup, _, bare = remote
    item = published(remote)
    prepared = plan(setup, item, "prepare_remote_delete", remote="review")["item"]
    newer = git(setup[2], "commit-tree", git(setup[2], "rev-parse", "HEAD^{tree}"), "-p", item["candidate_oid"], "-m", "remote race")
    original = RemoteGit.update
    def race(self, observation, result_oid, **kwargs):
        if kwargs.get("delete"):
            assert original(self, self.observe(observation["ref"]), newer).returncode == 0
        return original(self, observation, result_oid, **kwargs)
    monkeypatch.setattr(RemoteGit, "update", race)
    result = apply(setup, prepared, "remote-race-delete")
    assert result["error"] == "PUBLISH_BRANCH_RESULT_CONFLICT"
    assert result["item"]["branches"]["plan"]["archive_retained"]
    assert git(bare, "rev-parse", "work/owned") == newer


def test_private_history_is_never_pushed_as_a_work_branch(setup, tmp_path_factory):
    (setup[2] / "CLAUDE.md").write_text("private local instructions\n", encoding="utf8")
    git(setup[2], "add", "CLAUDE.md")
    git(setup[2], "commit", "-m", "local private instructions")
    item = adopt(setup, prepare(setup, target_branch="work/owned")["item"])["item"]
    bare = tmp_path_factory / "private-history.git"
    git(setup[2], "init", "--bare", str(bare))
    git(setup[2], "remote", "add", "review", str(bare))
    result = plan(setup, item, "prepare_publish", remote="review")
    assert result["error"] == "PUBLISH_FF_REQUIRES_FILTERING"
    assert not git(bare, "for-each-ref", "--format=%(refname)")


def dependent(setup, *, request="dependent"):
    return prepare(setup, request, source_ref="refs/heads/work/owned", target_branch="work/dependent")["item"]


def test_active_candidate_dependency_blocks_delete_but_allows_archive(managed):
    setup, item = managed
    dependent(setup)
    result = plan(setup, item, "prepare_delete")
    assert result["error"] == "PUBLISH_BRANCH_HAS_DEPENDENCIES"
    archived = plan(setup, item, "prepare_archive")["item"]
    assert apply(setup, archived)["item"]["branches"]["plan"]["state"] == "archived"
    assert git(setup[2], "rev-parse", "work/owned") == item["candidate_oid"]


def test_dependency_created_after_preview_is_checked_again_before_delete(managed):
    setup, item = managed
    prepared = plan(setup, item, "prepare_delete")["item"]
    dependent(setup)
    result = apply(setup, prepared)
    assert result["error"] == "PUBLISH_BRANCH_HAS_DEPENDENCIES"
    assert CandidateGit(setup[2]).target(prepared["branches"]["plan"]["archive_ref"]) is None


def test_resolved_head_dependency_survives_later_checkout(managed):
    setup, item = managed
    original = git(setup[2], "symbolic-ref", "--short", "HEAD")
    git(setup[2], "checkout", "work/owned")
    following = prepare(setup, "head-dependent", target_branch="work/following")["item"]
    assert following["candidate"]["source_ref"] == "HEAD"
    assert following["candidate"]["resolved_source_ref"] == "refs/heads/work/owned"
    git(setup[2], "checkout", original)
    assert plan(setup, item, "prepare_delete")["error"] == "PUBLISH_BRANCH_HAS_DEPENDENCIES"


def save_pull(setup, item, *, state="open", stamp="2026-10-26T00:00:00+00:00", head_oid=None):
    from backend.core.publish.state import Repository
    from backend.core.storage import get_storage
    repo = Repository(str(setup[2]), get_storage(setup[2]))
    ledger, ref = repo.load()
    current = ledger["items"][item["id"]]
    current.setdefault("collaboration", {"snapshots": [], "drafts": []})["snapshots"].append({
        "id": "pull-" + state, "binding": current["binding"], "observed_at": stamp,
        "target": {"kind": "github", "instance": "https://github.com", "repository": "fixture/repo", "account": "alice"},
        "resource": {"kind": "pull", "number": 3},
        "document": {"number": 3, "is_pull": True, "state": state,
            "refs": {"head": "work/owned", "head_sha": head_oid or item["candidate_oid"]}}})
    repo.save(ledger, ref)


def test_saved_open_pull_blocks_remote_delete_until_newer_closed_observation(remote):
    setup, _, bare = remote
    item = published(remote)
    save_pull(setup, item)
    denied = plan(setup, item, "prepare_remote_delete", remote="review")
    assert denied["error"] == "PUBLISH_BRANCH_HAS_DEPENDENCIES"
    save_pull(setup, item, state="closed", stamp="2026-10-26T00:01:00+00:00")
    prepared = plan(setup, item, "prepare_remote_delete", request="after-close", remote="review")["item"]
    assert apply(setup, prepared, "delete-after-close")["item"]["branches"]["plan"]["state"] == "remote_deleted"
    assert not git(bare, "for-each-ref", "--format=%(refname)")


def test_older_open_pull_tip_still_blocks_advanced_work_branch(managed):
    setup, item = managed
    save_pull(setup, item)
    old = item["candidate_oid"]
    newer = git(setup[2], "commit-tree", git(setup[2], "rev-parse", "HEAD^{tree}"), "-p", old, "-m", "ongoing pull work")
    git(setup[2], "update-ref", "refs/heads/work/owned", newer, old)
    assert plan(setup, item, "prepare_delete")["error"] == "PUBLISH_BRANCH_HAS_DEPENDENCIES"


def test_pending_delete_intent_blocks_new_candidate_at_ledger_cas(managed, monkeypatch):
    setup, item = managed
    prepared = plan(setup, item, "prepare_delete")["item"]
    original, blocked = CandidateGit.run, []
    def before_archive(self, args):
        if args[0] == "update-ref" and "gitgo publish archive" in args and not blocked:
            pending = read_ledger(setup[2])[0]["items"][item["id"]]
            assert pending["branches"]["plan"]["state"] == "pending"
            blocked.append(prepare(setup, "racing-dependent", source_ref="refs/heads/work/owned", target_branch="work/racing-dependent"))
        return original(self, args)
    monkeypatch.setattr(CandidateGit, "run", before_archive)
    result = apply(setup, prepared)
    assert blocked[0]["error"] == "PUBLISH_BRANCH_HAS_DEPENDENCIES"
    assert result["item"]["branches"]["plan"]["state"] == "deleted"
    assert len(read_ledger(setup[2])[0]["items"]) == 1


def test_pending_delete_intent_blocks_new_pull_draft_at_ledger_cas(managed, monkeypatch):
    from backend.core.publish.state import Repository
    from backend.core.storage import get_storage
    setup, item = managed
    other = prepare(setup, "other-item", target_branch="work/other")["item"]
    repository = Repository(str(setup[2]), get_storage(setup[2]))
    ledger, reference = repository.load()
    ledger["items"][other["id"]]["collaboration"] = {"drafts": [], "snapshots": [{
        "id": "empty-pulls", "binding": other["binding"], "resource": {"kind": "pulls"},
        "observed_at": "2026-10-26T00:00:00+00:00", "documents": [],
        "target": {"kind": "github", "instance": "https://github.com", "repository": "fixture/repo", "account": "alice"}}]}
    repository.save(ledger, reference)
    prepared = plan(setup, item, "prepare_delete")["item"]
    original, blocked = CandidateGit.run, []
    def before_archive(self, args):
        if args[0] == "update-ref" and "gitgo publish archive" in args and not blocked:
            blocked.append(call(setup, "collaborate", "racing-pull-draft", item_id=other["id"],
                expected_revision=other["revision"], action="draft", snapshot_id="empty-pulls",
                draft={"kind": "pull", "title": "Racing branch dependency", "body": "Please review this branch.",
                    "head": "work/owned", "base": "main", "head_oid": item["candidate_oid"], "base_oid": item["candidate_oid"]}))
        return original(self, args)
    monkeypatch.setattr(CandidateGit, "run", before_archive)
    result = apply(setup, prepared)
    assert blocked[0]["error"] == "PUBLISH_BRANCH_HAS_DEPENDENCIES", blocked
    assert result["item"]["branches"]["plan"]["state"] == "deleted"
    assert not read_ledger(setup[2])[0]["items"][other["id"]]["collaboration"]["drafts"]
