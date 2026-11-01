"""Workspace consolidation uses the real API, Git trees and existing authority chain."""
import pytest
from tests.test_publish_commands import setup, call, git
from tests.test_publish_formal import protocol, command
from backend.core.publish.state import PublishError
from backend.core.publish.history import workspace_page, history_page


def commit(repo, filename, content):
    (repo / filename).write_text(content, encoding="utf8")
    git(repo, "add", filename)
    git(repo, "commit", "-m", "fix: " + filename)
    return git(repo, "rev-parse", "HEAD")


def candidate(setup, request="local", **changes):
    _, project, repo = setup
    base = git(repo, "rev-parse", "HEAD")
    first = commit(repo, "test_local.py", "def test_ok():\n    assert True\n")
    omitted = commit(repo, "unselected.txt", "must stay out\n")
    last = commit(repo, "feature.txt", "semantic feature\n")
    result = call(setup, "prepare", request, **{
        "source_role": "workspace", "source_ref": last, "target_role": "workspace", "base_oid": base,
        "destination": "formal", "target_branch": "publish/formal", "method": "squash", "owner": "maintainer",
        "selected_commits": [last, first], "title": "feat: coherent feature", "test_target": "test_local.py", **changes})
    assert "item" in result, result
    return result["item"], (base, first, omitted, last)


def test_selection_is_canonical_local_and_not_a_checkout_or_remote_write(setup, monkeypatch):
    services, project, repo = setup
    assert not project.backup_path
    item, (base, first, omitted, last) = candidate(setup)
    assert item["plan"]["selected_commits"] == [first, last]
    assert item["plan"]["target_absent"] is True
    assert "refs/heads/publish/formal" not in git(repo, "show-ref", "--heads")
    assert git(repo, "rev-parse", "HEAD") == last
    assert git(repo, "ls-tree", "--name-only", item["plan"]["tree_oid"]).splitlines() == ["README.md", "feature.txt", "test_local.py"]
    assert "feat: coherent feature" in item["plan"]["commit_message"]
    assert command(setup, "execute", item)["error"] == "PUBLISH_CHECKS_REQUIRED"
    protocol(monkeypatch)
    verified = command(setup, "verify", item)
    assert "item" in verified, verified
    checked = verified["item"]
    result = command(setup, "execute", checked)["item"]
    oid = git(repo, "rev-parse", "publish/formal")
    assert result["status"] == "integrated" and oid != last
    assert git(repo, "rev-parse", "HEAD") == last
    assert git(repo, "show", "-s", "--format=%P", oid) == base
    page = services.invoke("publish.workspace", {"project": project.name})
    formal = page["formal_commits"][0]
    assert formal["oid"] == oid and formal["parents"] == [base]
    assert formal["sources"] == [first, last] and omitted not in formal["sources"]
    assert history_page(project, item_id=item["id"], head="formal")["head_oid"] == oid
    patch = services.invoke("publish.preview_diff", {"project": project.name, "item_id": item["id"], "binding": item["binding"]})
    assert "+semantic feature" in patch["text"] and "unselected.txt" not in patch["text"]
    # Revising the current plan must not erase a previous real commit or retarget
    # the history node to the new candidate/baseline.
    operation_id = formal["operation_id"]
    new_source = commit(repo, "next.txt", "next revision\n")
    revised = call(setup, "prepare", "next-local", source_role="workspace", source_ref=new_source,
        destination="formal", target_role="workspace", target_branch="publish/formal", owner="maintainer",
        item_id=item["id"], expected_revision=result["revision"], method="squash", selected_commits=[new_source], test_target="test_local.py")
    assert "item" in revised, revised
    historical = workspace_page(project)["formal_commits"][0]
    assert historical["oid"] == oid and historical["sources"] == [first, last]
    assert history_page(project, item_id=item["id"], head="formal_operation:" + operation_id)["head_oid"] == oid


def test_a_competing_first_branch_creation_invalidates_checks(setup, monkeypatch):
    _, _, repo = setup
    item, (_, _, _, last) = candidate(setup)
    git(repo, "branch", "publish/formal", last)
    result = command(setup, "verify", item)
    assert result["error"] == "PUBLISH_TARGET_EXISTS"
    assert git(repo, "rev-parse", "publish/formal") == last


def test_reading_process_history_does_not_create_publish_records_and_rejects_stale_pages(setup):
    services, project, repo = setup
    first = services.invoke("publish.workspace", {"project": project.name, "limit": 1})
    assert len(first["commits"]) == 1 and first["formal_commits"] == []
    commit(repo, "second.txt", "second\n")
    page = workspace_page(project, limit=1)
    assert page["next_cursor"]
    commit(repo, "third.txt", "third\n")
    with pytest.raises(PublishError, match="SNAPSHOT_CHANGED"):
        workspace_page(project, limit=1, cursor=page["next_cursor"])
    items = services.invoke("publish.items", {"project": project.name})
    assert items["items"] == []


def test_privacy_and_authorship_are_saved_as_one_validated_group(setup):
    services, project, _ = setup
    current = services.invoke("publish.get", {"project": project.name})
    value = {"privacy": current["privacy"], "authorship": {"mode": "clean", "strip_commit_coauthors": True, "strip_code_comments": False}}
    saved = services.invoke("publish.set", {"project": project.name, "section": "privacy_authorship", "value": value})
    assert saved["ok"]
    after = services.invoke("publish.get", {"project": project.name})
    assert after["authorship"]["mode"] == "clean"
    assert after["privacy"]["version"] == current["privacy"]["version"] + 1
    value["authorship"]["mode"] = "invalid"
    from backend.core.application.services import OperationError
    with pytest.raises(OperationError, match="Invalid authorship"):
        services.invoke("publish.set", {"project": project.name, "section": "privacy_authorship", "value": value})
    assert services.invoke("publish.get", {"project": project.name}) == after
    with pytest.raises(OperationError, match="Settings changed"):
        services.invoke("publish.set", {"project": project.name, "section": "privacy_authorship", "value": value,
            "expected_value": {"privacy": current["privacy"], "authorship": current["authorship"]}})
    assert services.invoke("publish.get", {"project": project.name}) == after


def test_repository_pr_browsing_needs_no_local_item_and_never_sends(setup, monkeypatch):
    from tests.test_publish_collaboration import Server
    from backend.remote import publish_platform, publish_accounts
    services, project, _ = setup
    server = Server()
    monkeypatch.setattr(publish_platform, "PlatformClient", lambda target, token: server.client(target))
    monkeypatch.setattr(publish_accounts, "resolve_secret", lambda target: "fixture-secret")
    result = services.invoke("publish.platform_read", {"project": project.name, "target": server.target.descriptor(),
        "resource": {"kind": "pull", "number": 9}})
    assert result["read_only"] is True and result["resource"]["number"] == 9
    assert result["document"]["number"] == 9 and result["files"][0]["path"] == "calc.py"
    assert all(request.method == "GET" for request in server.requests)
    assert "fixture-secret" not in str(result)
    assert services.invoke("publish.items", {"project": project.name})["items"] == []
