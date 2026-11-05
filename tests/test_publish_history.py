"""Real commit parents, bounded patches, snapshot and repository scope."""
import base64
import json
import subprocess

import pytest

from backend.core.publish.history import history_page, diff_view, history_scope
from backend.core.publish.state import PublishError, Repository
from backend.core.storage import get_storage
from tests.test_publish_commands import setup, git, prepare
from tests.test_publish_formal import formal_setup, prepare as prepare_formal


def _commit(repo, name, content):
    (repo / name).write_text(content, encoding="utf8")
    git(repo, "add", name)
    git(repo, "commit", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def test_real_merge_parents_pagination_and_fixed_head(setup):
    services, project, repo = setup
    root = git(repo, "rev-parse", "HEAD")
    first = _commit(repo, "first", "one\n")
    git(repo, "checkout", "-b", "side", root)
    side = _commit(repo, "side", "two\n")
    git(repo, "checkout", "-b", "combined", first)
    git(repo, "merge", "--no-ff", "-m", "actual merge", side)
    merge = git(repo, "rev-parse", "HEAD")
    item = prepare(setup)["item"]
    saved = history_page(project, item_id=item["id"], limit=1)
    assert saved["relation"] == "git_parent"
    assert saved["commits"][0]["parents"] == [first, side]
    assert saved["commits"][0]["author"]["name"] == "Tester"
    assert saved["head"] == "candidate" and saved["role"] == "workspace"
    git(repo, "commit", "--allow-empty", "-m", "later unprepared source")
    following = history_page(project, item_id=item["id"], limit=2, cursor=saved["next_cursor"])
    assert following["head_oid"] == merge
    assert {row["oid"] for row in following["commits"]} == {first, side}
    last = history_page(project, item_id=item["id"], limit=2, cursor=following["next_cursor"])
    assert [row["oid"] for row in last["commits"]] == [root] and last["next_cursor"] is None
    for parent in [first, side]:
        patch = diff_view(project, item_id=item["id"], base_oid=parent, head_oid=merge,
                          snapshot_id=saved["snapshot_id"])
        assert "diff --git" in patch["text"] and not patch["truncated"]
    root_patch = services.invoke("publish.diff", {"project": project.name, "item_id": item["id"],
                                                "base_oid": "", "head_oid": root})
    assert "+# Test" in root_patch["text"]
    with pytest.raises(PublishError, match="BASE_REQUIRED"):
        diff_view(project, item_id=item["id"], base_oid="", head_oid=merge)


def test_snapshot_change_and_repository_role_drift_fail_before_git(setup, monkeypatch):
    _, project, repo = setup
    _commit(repo, "second", "two")
    item = prepare(setup)["item"]
    saved = history_page(project, item_id=item["id"], limit=1)
    store = Repository(str(repo), get_storage(repo))
    ledger, ref = store.load()
    ledger["items"][item["id"]]["binding"] = "different"
    store.save(ledger, ref)
    with pytest.raises(PublishError, match="SNAPSHOT_CHANGED"):
        history_page(project, item_id=item["id"], cursor=saved["next_cursor"])
    with pytest.raises(PublishError, match="SNAPSHOT_CHANGED"):
        diff_view(project, item_id=item["id"], base_oid="", snapshot_id=saved["snapshot_id"])
    monkeypatch.setattr("backend.core.publish.history._run", lambda *args, **kw: pytest.fail("Git started"))
    project.workspace_path = str(repo.parent)
    with pytest.raises(PublishError, match="ITEM_NOT_FOUND"):
        history_scope(project, item["id"])


def test_unreachable_objects_and_grafts_never_expand_diff_scope(setup):
    _, project, repo = setup
    root = git(repo, "rev-parse", "HEAD")
    public = _commit(repo, "public", "allowed")
    item = prepare(setup)["item"]
    git(repo, "checkout", "--orphan", "private")
    private = _commit(repo, "secret-name", "secret private content")
    # A forged graft must neither invent parent edges nor authorize private data.
    (repo / ".git/info/grafts").write_text(public + " " + private + "\n", encoding="ascii")
    git(repo, "replace", public, private)
    page = history_page(project, item_id=item["id"])
    assert [row["oid"] for row in page["commits"]] == [public, root]
    assert page["commits"][0]["parents"] == [root]
    for bad in [private, "f" * 40, git(repo, "rev-parse", private + "^{tree}")]:
        with pytest.raises(PublishError, match="HISTORY_OUTSIDE_SCOPE"):
            diff_view(project, item_id=item["id"], base_oid=bad, head_oid=public)


def test_patch_output_has_hard_bound_and_queries_do_not_open_writer(setup, monkeypatch):
    _, project, repo = setup
    root = git(repo, "rev-parse", "HEAD")
    _commit(repo, "large", "a" * (3 * 1024 * 1024) + "\n")
    item = prepare(setup)["item"]
    monkeypatch.setattr("backend.core.storage.get_storage", lambda *args, **kw: pytest.fail("writer opened"))
    patch = diff_view(project, item_id=item["id"], base_oid=root, limit_bytes=4096)
    assert patch["truncated"] and len(patch["text"].encode()) <= 4096
    assert len(history_page(project, item_id=item["id"])["commits"]) == 2
    assert git(repo, "status", "--porcelain") == ""


def test_oversized_commit_message_is_rejected_at_bounded_read(setup):
    _, project, repo = setup
    git(repo, "commit", "--allow-empty", "-m", "small")
    item = prepare(setup)["item"]
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    huge = subprocess.run(["git", "-C", str(repo), "commit-tree", tree], input="x" * 70000,
                          capture_output=True, text=True, check=True).stdout.strip()
    store = Repository(str(repo), get_storage(repo))
    ledger, ref = store.load()
    ledger["items"][item["id"]]["candidate"]["oid"] = huge
    store.save(ledger, ref)
    with pytest.raises(PublishError, match="OUTPUT_LIMIT"):
        history_page(project, item_id=item["id"])


def test_formal_and_contribution_scopes_are_saved_roles(formal_setup):
    _, project, repo = formal_setup
    item = prepare_formal(formal_setup)
    formal = history_scope(project, item["id"], "formal")
    contribution = history_scope(project, item["id"], "contribution:0")
    assert formal["repository"] == str(repo.resolve())
    assert formal["role"] == "formal" and formal["head_oid"] == item["plan"]["expected_target_oid"]
    assert contribution["head_oid"] == item["candidate"]["oid"]
    assert history_page(project, item_id=item["id"], head="formal")["commits"]
    project.backup_path = str(repo.parent)
    with pytest.raises(PublishError, match="SCOPE_CHANGED"):
        history_scope(project, item["id"], "formal")


@pytest.mark.parametrize("cursor", ["!", base64.urlsafe_b64encode(json.dumps({"offset": True, "snapshot": "x"}).encode()).decode()])
def test_malformed_cursor_rejected(setup, cursor):
    _, project, _ = setup
    item = prepare(setup)["item"]
    with pytest.raises(PublishError, match="INVALID_CURSOR"):
        history_page(project, item_id=item["id"], cursor=cursor)


def test_read_session_does_not_allow_writes_transport_output_files_or_expired_builder(git_repo):
    from backend.core.loop.git_execution import protected_git_read_session, GitBoundaryError
    with protected_git_read_session(git_repo) as (command_for, environment):
        command = command_for(["git", "log", "--format=%H", "-1"])
        assert "--no-ext-diff" in command and "--no-textconv" in command
        assert environment["GIT_NO_LAZY_FETCH"] == "1"
        for args in [["update-ref", "refs/heads/unsafe", "0" * 40], ["push", "origin"],
                     ["log", "--output=unsafe"], ["diff", "--ext-diff"], ["cat-file", "--textconv", "HEAD"]]:
            with pytest.raises(GitBoundaryError, match="GIT_COMMAND_UNSUPPORTED"):
                command_for(["git", *args])
    with pytest.raises(GitBoundaryError, match="GIT_COMMAND_UNSUPPORTED"):
        command_for(["git", "log", "-1"])


def test_snapshot_cache_revalidates_binding_head_role_and_cannot_be_poisoned(setup):
    _, project, repo = setup
    root = git(repo, "rev-parse", "HEAD")
    _commit(repo, "later", "later")
    item = prepare(setup)["item"]
    first = history_page(project, item_id=item["id"])
    assert first["cache"] == "miss"
    first["commits"][0]["subject"] = "poisoned"
    cached = history_page(project, item_id=item["id"])
    assert cached["cache"] == "verified_snapshot" and cached["commits"][0]["subject"] == "later"
    store = Repository(str(repo), get_storage(repo))
    for field, value in [("binding", "new-binding"), ("oid", root), ("role", "trial")]:
        ledger, ref = store.load()
        current = ledger["items"][item["id"]]
        if field == "binding":
            current[field] = value
        else:
            current["candidate"][field] = value
        if field == "role":
            project.trial_path = str(repo)
        store.save(ledger, ref)
        result = history_page(project, item_id=item["id"])
        assert result["cache"] == "miss"
    project.trial_path = str(repo.parent)
    with pytest.raises(PublishError, match="SCOPE_CHANGED"):
        history_page(project, item_id=item["id"])


def test_snapshot_cache_ttl_and_capacity_evict_complete_entries(monkeypatch):
    from collections import OrderedDict
    from types import SimpleNamespace
    from backend.core.publish import history_cache as cache
    clock = [0]
    monkeypatch.setattr(cache, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(cache, "_entries", OrderedDict())
    monkeypatch.setattr(cache, "_bytes", 0)
    monkeypatch.setattr(cache, "MAX_ENTRIES", 2)
    monkeypatch.setattr(cache, "MAX_BYTES", 128)
    cache.put("first", {"text": "first"})
    cache.put("second", {"text": "second"})
    cache.put("third", {"text": "third"})
    assert cache.get("first") is None
    cache.put("large", {"text": "x" * 120})  # larger than the entire encoded budget
    assert cache.get("large") is None and cache.get("second") is not None
    cache.put("bounded", {"text": "x" * 110})
    assert cache.get("second") is None and cache._bytes <= 128
    clock[0] += cache.TTL_SECONDS + 1
    assert cache.get("bounded") is None
