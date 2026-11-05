import json
import pytest

from backend.core.publish.remote_git import RemoteGit
from backend.core.publish.sharing import REF, read_document
from backend.core.publish.state import read_ledger
from backend.core.loop.git_execution import GitBoundaryError
from tests.test_publish_commands import setup, call, prepare, git


@pytest.fixture
def shared(setup, tmp_path_factory):
    _, _, repo = setup
    bare = tmp_path_factory / "shared.git"
    git(repo, "init", "--bare", str(bare))
    git(repo, "remote", "add", "collaboration", str(bare))
    return setup, bare


def test_exact_ref_creation_refuses_remote_drift_and_does_not_push_other_refs(shared, tmp_path_factory):
    setup, bare = shared
    repo = setup[2]
    remote = RemoteGit(repo, "collaboration")
    observed = remote.observe("refs/heads/accepted")
    assert observed["oid"] is None
    head = git(repo, "rev-parse", "HEAD")
    assert remote.update(observed, head).returncode == 0
    assert remote.observe("refs/heads/accepted")["oid"] == head
    assert git(bare, "for-each-ref", "--format=%(refname)") == "refs/heads/accepted"
    another = tmp_path_factory / "another.git"
    git(repo, "init", "--bare", str(another))
    git(repo, "remote", "set-url", "collaboration", str(another))
    with pytest.raises(GitBoundaryError, match="changed"):
        remote.update(observed, head)


def test_shared_metadata_preview_and_explicit_publish_use_separate_ref(shared):
    setup, bare = shared
    item = prepare(setup, rework="Fix whitespace")["item"]
    prepared = call(setup, "share", "share-prepare", item_id=item["id"], expected_revision=item["revision"], action="prepare", remote="collaboration")["item"]
    assert not git(bare, "for-each-ref", "--format=%(refname)")
    raw = json.dumps(prepared["sharing"]["preview"])
    assert "repository" not in raw and "summary" not in raw and "runtime_owner" not in raw
    assert str(setup[2]) not in raw
    result = call(setup, "share.publish", "share-publish", item_id=item["id"], expected_revision=prepared["revision"])
    assert result.get("item", {}).get("sharing", {}).get("state") == "published", result
    assert git(bare, "for-each-ref", "--format=%(refname)") == REF
    reader = RemoteGit(setup[2], "collaboration")
    observed = reader.observe(REF)
    reader.fetch(observed)
    document = read_document(reader.git, observed["oid"])
    assert document["items"][item["id"]]["owner"] == "maintainer"
    assert not (setup[2] / ".git/FETCH_HEAD").exists()


def test_two_writers_cannot_overwrite_shared_revision(shared):
    setup, bare = shared
    first = prepare(setup)["item"]
    second = prepare(setup, "second", target_branch="publish/second")["item"]
    plans = [call(setup, "share", "prepare-" + item["id"], item_id=item["id"], expected_revision=item["revision"],
        action="prepare", remote="collaboration")["item"] for item in (first, second)]
    result = call(setup, "share.publish", "first-publish", item_id=first["id"], expected_revision=plans[0]["revision"])
    assert result["item"]["sharing"]["state"] == "published"
    loser = call(setup, "share.publish", "second-publish", item_id=second["id"], expected_revision=plans[1]["revision"])
    assert loser["error"] == "PUBLISH_SHARED_CONFLICT"
    assert git(bare, "rev-parse", REF) == result["item"]["sharing"]["result_oid"]
    retried = call(setup, "share", "second-reprepare", item_id=second["id"], expected_revision=plans[1]["revision"],
        action="prepare", remote="collaboration")["item"]
    assert call(setup, "share.publish", "second-accepted", item_id=second["id"], expected_revision=retried["revision"])["item"]["sharing"]["state"] == "published"
    reader = RemoteGit(setup[2], "collaboration")
    observed = reader.observe(REF)
    assert len(read_document(reader.git, reader.fetch(observed))["items"]) == 2


def test_lost_share_ack_reconciles_without_replay_and_blocks_revision(shared, monkeypatch):
    setup, _ = shared
    item = prepare(setup)["item"]
    prepared = call(setup, "share", "prepare-share", item_id=item["id"], expected_revision=item["revision"], action="prepare", remote="collaboration")["item"]
    original = RemoteGit.update
    def lost(self, *args, **kwargs):
        assert original(self, *args, **kwargs).returncode == 0
        raise OSError("lost ack")
    monkeypatch.setattr(RemoteGit, "update", lost)
    pending = call(setup, "share.publish", "lost", item_id=item["id"], expected_revision=prepared["revision"])
    assert pending["effect_state"] == "ambiguous" and pending["item"]["sharing"]["state"] == "pending"
    current = pending["item"]
    denied = call(setup, "plan.update", "competing-edit", item_id=item["id"], expected_revision=current["revision"], target_branch="publish/other", owner="owner", rework="")
    assert denied["error"] == "PUBLISH_OPERATION_PENDING"
    monkeypatch.setattr(RemoteGit, "update", lambda *args, **kwargs: pytest.fail("unexpected replay"))
    restored = call(setup, "share", "reconcile-share", item_id=item["id"], expected_revision=current["revision"], action="reconcile")["item"]
    assert restored["sharing"]["state"] == "published"


def test_exact_transfer_refuses_split_fetch_push_and_non_fast_forward(shared, tmp_path_factory):
    setup, _ = shared
    repo = setup[2]
    remote = RemoteGit(repo, "collaboration")
    initial = remote.observe("refs/heads/accepted")
    head = git(repo, "rev-parse", "HEAD")
    assert remote.update(initial, head).returncode == 0
    # A new root with the same tree is still an unrelated history overwrite.
    root = git(repo, "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"), "-m", "unrelated")
    from backend.core.publish.state import PublishError
    with pytest.raises(PublishError, match="PUBLISH_REMOTE_NON_FAST_FORWARD"):
        remote.update(remote.observe("refs/heads/accepted"), root)
    git(repo, "config", "remote.collaboration.pushurl", str(tmp_path_factory / "different.git"))
    with pytest.raises(GitBoundaryError, match="one shared fetch/push"):
        remote.observe(REF)


def test_shared_preview_cannot_publish_changed_owner_or_review(shared):
    setup, bare = shared
    item = prepare(setup)["item"]
    item = call(setup, "share", "prepare-shared-facts", item_id=item["id"], expected_revision=item["revision"], action="prepare", remote="collaboration")["item"]
    changed = call(setup, "decide", "review-new-owner", item_id=item["id"], expected_revision=item["revision"], decision="rework", note="Follow up", owner="next-owner")["item"]
    assert changed["binding"] == item["binding"]
    result = call(setup, "share.publish", "stale-preview", item_id=changed["id"], expected_revision=changed["revision"])
    assert result["error"] == "PUBLISH_PLAN_STALE"
    assert not git(bare, "for-each-ref", "--format=%(refname)")


def test_explicit_shared_refresh_has_paginated_offline_facts(shared, monkeypatch):
    setup, _ = shared
    services, project, repo = setup
    first = prepare(setup)["item"]
    second = prepare(setup, "second-reader", target_branch="publish/second")["item"]
    for index, item in enumerate((first, second)):
        retained = call(setup, "branch", "retain-" + str(index), item_id=item["id"], expected_revision=item["revision"],
            action="retain_source", owner="reviewer", reason="Keep while discussion remains open")["item"]
        plan = call(setup, "share", "share-" + str(index), item_id=item["id"], expected_revision=retained["revision"], action="prepare", remote="collaboration")["item"]
        call(setup, "share.publish", "publish-" + str(index), item_id=item["id"], expected_revision=plan["revision"])
    latest = services.invoke("publish.item", {"project": project.name, "item_id": second["id"]})["item"]
    refreshed = call(setup, "share", "read-shared", item_id=latest["id"], expected_revision=latest["revision"], action="refresh", remote="collaboration")["item"]
    before, head = read_ledger(repo)
    monkeypatch.setattr(RemoteGit, "observe", lambda *a, **k: pytest.fail("offline view opened remote"))
    page = services.invoke("publish.shared.items", {"project": project.name, "item_id": second["id"], "limit": 1})
    assert page["total"] == 2 and page["next_cursor"] and page["read_only"] is True
    assert page["items"][0]["branch_retention"][0]["owner"] == "reviewer"
    assert "repository" not in json.dumps(page["items"]) and "drafts" not in json.dumps(page["items"])
    other = services.invoke("publish.shared.items", {"project": project.name, "item_id": second["id"], "limit": 1, "cursor": page["next_cursor"]})
    assert other["items"][0]["id"] != page["items"][0]["id"] and not other["next_cursor"]
    assert read_ledger(repo) == (before, head)
