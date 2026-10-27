"""Real local Git effects and persistence/recovery contracts for Publish."""
import subprocess

import pytest

from backend.core.application import ApplicationServices
from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.core.publish.git import CandidateGit
from backend.core.publish.state import read_ledger
from backend.core.storage import close_owned_storage


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf8").stdout.strip()


@pytest.fixture
def setup(git_repo, tmp_path_factory):
    project = ProjectConfig(name="publish-command-test")
    project.workspace_path = str(git_repo)
    ConfigManager.save(Config(projects=[project]))
    return ApplicationServices(), project, git_repo


def call(setup, operation, request, **args):
    services, project, _ = setup
    return services.invoke("publish." + operation, {"project": project.name, "request_id": request, **args})


def prepare(setup, request="prepare1", **args):
    return call(setup, "prepare", request, **{
        "source_role": "workspace", "source_ref": "HEAD", "target_branch": "publish/first",
        "owner": "maintainer", "title": "固定候选 🚀", **args})


def adopt(setup, item, request="adopt1"):
    return call(setup, "adopt", request, item_id=item["id"], expected_revision=item["revision"])


def test_prepare_read_and_adopt_pins_oid_without_changing_checkout(setup):
    services, project, repo = setup
    head = git(repo, "rev-parse", "HEAD")
    current = git(repo, "symbolic-ref", "HEAD")
    item = prepare(setup)["item"]
    assert item["candidate_oid"] == head and item["formal_eligible"] is False
    assert item["evidence"][0]["producer"] == "protected_git"
    (repo / "uncommitted.txt").write_text("untouched", encoding="utf8")
    result = adopt(setup, item)
    assert result["item"]["status"] == "adopted"
    assert git(repo, "rev-parse", "publish/first") == head
    assert git(repo, "symbolic-ref", "HEAD") == current
    assert (repo / "uncommitted.txt").read_text() == "untouched"
    close_owned_storage()
    saved = services.invoke("publish.item", {"project": project.name, "item_id": item["id"]})["item"]
    assert saved["operations"][0]["result_oid"] == head
    assert saved["formal_eligible"] is False


def test_failed_check_requires_rework_and_survives_adoption(setup):
    _, _, repo = setup
    (repo / "failure.txt").write_text("trailing space   \n", encoding="utf8")
    git(repo, "add", "failure.txt"); git(repo, "commit", "-m", "known whitespace failure")
    item = prepare(setup)["item"]
    assert item["evidence"][0]["outcome"] == "failed"
    assert adopt(setup, item)["error"] == "PUBLISH_REWORK_REQUIRED"
    updated = call(setup, "plan.update", "plan1", item_id=item["id"], expected_revision=1,
        target_branch="publish/first", owner="repair-owner", rework="修复空白错误，并运行完整测试")["item"]
    result = adopt(setup, updated, "adopt2")["item"]
    assert result["status"] == "adopted" and result["owner"] == "repair-owner"
    assert result["rework"] and result["evidence"][0]["outcome"] == "failed"
    assert result["formal_eligible"] is False


def test_drift_does_not_move_fixed_candidate_and_existing_target_is_preserved(setup):
    _, _, repo = setup
    item = prepare(setup)["item"]
    git(repo, "commit", "--allow-empty", "-m", "later source")
    later = git(repo, "rev-parse", "HEAD")
    result = adopt(setup, item)["item"]
    assert git(repo, "rev-parse", "publish/first") == item["candidate_oid"] != later
    second = prepare(setup, "prepare2", target_branch="publish/raced")["item"]
    git(repo, "branch", "publish/raced", item["candidate_oid"])
    assert adopt(setup, second, "adopt2")["error"] == "PUBLISH_TARGET_EXISTS"
    assert git(repo, "rev-parse", "publish/raced") == item["candidate_oid"]


def test_request_replay_and_conflicting_identity(setup, monkeypatch):
    item = prepare(setup)["item"]
    result = adopt(setup, item)
    monkeypatch.setattr(CandidateGit, "adopt", lambda *args: pytest.fail("replay executed Git"))
    assert adopt(setup, item) == result
    assert prepare(setup)["item"]["revision"] == 1
    assert prepare(setup, owner="different")["error"] == "PUBLISH_REQUEST_CONFLICT"


def test_stale_plan_and_new_candidate_preserve_invalidated_evidence(setup):
    item = prepare(setup)["item"]
    changed = call(setup, "plan.update", "plan1", item_id=item["id"], expected_revision=1,
        target_branch="publish/new", owner="owner", rework="follow up")["item"]
    assert "adopt" not in changed["allowed_actions"] and changed["nodes"][1]["status"] == "stale"
    assert adopt(setup, item)["error"] == "PUBLISH_REVISION_CONFLICT"
    assert adopt(setup, changed, "adopt2")["error"] == "PUBLISH_CHECKS_STALE"
    revision = prepare(setup, "prepare2", item_id=item["id"], expected_revision=2,
                       target_branch="publish/new")["item"]
    assert revision["id"] == item["id"] and revision["revision"] == 3
    assert len(revision["evidence"]) == 2
    assert revision["evidence"][0]["binding"] != revision["binding"]
    assert adopt(setup, revision, "adopt3")["item"]["status"] == "adopted"


def test_lost_acknowledgement_is_reconciled_without_repeating_git(setup, monkeypatch):
    item = prepare(setup)["item"]
    original = CandidateGit.adopt
    def lose_ack(*args):
        original(*args)
        raise OSError("simulated transport loss after ref commit")
    monkeypatch.setattr(CandidateGit, "adopt", lose_ack)
    pending = adopt(setup, item)
    assert pending["effect_state"] == "ambiguous"
    assert adopt(setup, item) == pending
    monkeypatch.setattr(CandidateGit, "adopt", lambda *args: pytest.fail("reconciliation repeated effect"))
    result = call(setup, "reconcile", "reconcile1", item_id=item["id"], expected_revision=2)["item"]
    assert result["status"] == "adopted"
    assert result["operations"][0]["attribution"] == "matching_ref_observed"


def test_missing_result_remains_pending_and_blocks_revision(setup, monkeypatch):
    item = prepare(setup)["item"]
    def unavailable(*args):
        raise OSError("no acknowledgement")
    monkeypatch.setattr(CandidateGit, "adopt", unavailable)
    adopt(setup, item)
    result = call(setup, "reconcile", "reconcile1", item_id=item["id"], expected_revision=2)
    assert result["status"] == "needs_reconciliation"
    assert prepare(setup, "prepare2", item_id=item["id"], expected_revision=2)["error"] == "PUBLISH_OPERATION_PENDING"


def test_trial_import_uses_fixed_oid_without_config_or_fetch_head_changes(setup, tmp_path_factory, monkeypatch):
    _, project, repo = setup
    trial = tmp_path_factory / "trial"
    git(repo, "clone", "--no-hardlinks", str(repo), str(trial))
    git(trial, "config", "user.email", "test@local.gitgo.invalid")
    git(trial, "config", "user.name", "Tester")
    (trial / "incoming.txt").write_text("incoming\n", encoding="utf8")
    git(trial, "add", "incoming.txt"); git(trial, "commit", "-m", "incoming")
    project.trial_path = str(trial)
    ConfigManager.save(Config(projects=[project]))
    config_before = (repo / ".git" / "config").read_bytes()
    item = prepare(setup, source_role="trial")["item"]
    git(trial, "commit", "--allow-empty", "-m", "later incoming")
    original = CandidateGit.run
    def checked_run(self, args):
        result = original(self, args)
        if args[0] == "fetch":
            assert result.returncode == 0, result.stderr
        return result
    monkeypatch.setattr(CandidateGit, "run", checked_run)
    result = adopt(setup, item)
    assert result.get("item", {}).get("status") == "adopted", result
    assert git(repo, "rev-parse", "publish/first") == item["candidate_oid"]
    assert (repo / ".git" / "config").read_bytes() == config_before
    assert not (repo / ".git" / "FETCH_HEAD").exists()


def test_symbolic_target_and_unborn_checked_out_branch_are_not_overwritten(setup):
    _, _, repo = setup
    git(repo, "symbolic-ref", "refs/heads/publish/first", "refs/heads/missing")
    assert prepare(setup)["error"] == "PUBLISH_TARGET_EXISTS"
    assert git(repo, "symbolic-ref", "refs/heads/publish/first") == "refs/heads/missing"


def test_queries_do_not_create_writer_or_execute_git_after_preparation(setup, monkeypatch):
    services, project, _ = setup
    item = prepare(setup)["item"]
    close_owned_storage()
    monkeypatch.setattr("backend.core.storage.StorageRuntime.__init__", lambda *args, **kwargs: pytest.fail("query opened writer"))
    monkeypatch.setattr(CandidateGit, "run", lambda *args: pytest.fail("query ran Git"))
    assert services.invoke("publish.items", {"project": project.name})["items"][0]["id"] == item["id"]
    assert services.invoke("publish.item", {"project": project.name, "item_id": item["id"]})["item"]["revision"] == 1


def test_agent_and_ui_share_service_without_accepting_caller_identity(setup):
    from backend.core.tools.publish_service import build_publish_tools
    from backend.core.loop.tool_pipeline import ToolPipeline
    from tests.test_file_feedback_contract import context
    services, project, repo = setup
    tool = build_publish_tools(project)["publish_prepare"]
    ctx = context(repo, [tool.name])
    args = {"request_id": "agent1", "source_role": "workspace", "source_ref": "HEAD",
            "target_branch": "publish/agent", "owner": "reviewer"}
    spoof = ToolPipeline().execute({"name": tool.name, "args": {**args, "_publish_actor": "local_operator"}}, tool, ctx, "spoof")
    assert spoof.is_error and spoof.receipt["effect_state"] == "not_committed"
    result = ToolPipeline().execute({"name": tool.name, "args": args}, tool, ctx, "agent")
    assert not result.is_error, result
    item = result.data["item"]
    assert item["last_actor"] == ctx.process.process_id and item["task_id"] == "task"
    assert services.invoke("publish.item", {"project": project.name, "item_id": item["id"]})["item"]["binding"] == item["binding"]


def test_agent_external_trial_scope_uses_existing_broker_and_reuses_grant(setup, tmp_path_factory):
    from backend.core.tools.publish_service import build_publish_tools
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
    from tests.test_file_feedback_contract import context
    _, project, repo = setup
    trial = tmp_path_factory / "trial"
    git(repo, "clone", str(repo), str(trial))
    project.trial_path = str(trial)
    ConfigManager.save(Config(projects=[project]))
    tool = build_publish_tools(project)["publish_prepare"]
    ctx = context(repo, [tool.name])
    args = {"request_id": "agent1", "source_role": "trial", "source_ref": "HEAD",
            "target_branch": "publish/agent", "owner": "reviewer"}
    first = ToolPipeline().execute({"name": tool.name, "args": args}, tool, ctx, "denied")
    assert first.is_error and first.diagnostics["code"] == "RESOURCE_SCOPE_APPROVAL_REQUIRED"
    assert read_ledger(repo)[0] is None
    create_permission_request(ctx.process, {"tool_name": tool.name, "arguments": args,
        "resource": str(trial), "purpose": "prepare this configured trial candidate"}, {tool.name: tool}, str(repo))
    assert grant_from_decision(ctx.process, ctx.process.pending_decision, "allow_task")
    for index in range(2):
        result = ToolPipeline().execute({"name": tool.name, "args": {**args, "request_id": f"agent{index + 1}"}}, tool, ctx, f"allowed{index}")
        assert not result.is_error, result
    assert len(ctx.process.approval_grants) == 1


def test_concurrent_item_revisions_cannot_both_commit(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from backend.core.publish.state import Repository
    item = prepare(setup)["item"]
    entered, release = threading.Event(), threading.Event()
    original = Repository.save
    def competing_save(self, ledger, expected):
        entered.set()
        assert release.wait(timeout=15)
        return original(self, ledger, expected)
    monkeypatch.setattr(Repository, "save", competing_save)
    def edit(index):
        return call(setup, "plan.update", f"plan{index}", item_id=item["id"], expected_revision=1,
            target_branch="publish/first", owner=f"owner{index}", rework="follow up")
    with ThreadPoolExecutor(2) as pool:
        winner = pool.submit(edit, 0)
        try:
            assert entered.wait(timeout=15)
            rejected = pool.submit(edit, 1).result(timeout=10)
            assert rejected["error"] == "PUBLISH_OPERATION_RUNNING"
            assert rejected["effect_state"] == "not_committed"
        finally:
            release.set()
        committed = winner.result(timeout=30)
    assert committed["effect_state"] == "committed"
    assert committed["item"]["owner"] == "owner0" and committed["item"]["revision"] == 2
    assert edit(2)["error"] == "PUBLISH_REVISION_CONFLICT"


def test_lost_state_ack_does_not_start_git_and_receipt_replay_stays_pending(setup, monkeypatch):
    from backend.core.storage import StorageCommitUnknown
    from backend.core.publish.state import Repository
    item = prepare(setup)["item"]
    original = Repository.save
    def lost_ack(self, *args):
        original(self, *args)
        raise StorageCommitUnknown("commit")
    monkeypatch.setattr(Repository, "save", lost_ack)
    monkeypatch.setattr(CandidateGit, "adopt", lambda *args: pytest.fail("Git started after unconfirmed intent"))
    result = adopt(setup, item)
    assert result["error"] == "STORAGE_COMMIT_UNKNOWN" and result["effect_state"] == "ambiguous"
    repeated = adopt(setup, item)
    assert repeated["status"] == "needs_reconciliation"
    assert repeated["operation_id"] == result["operation_id"]


def test_trial_and_workspace_roles_can_share_one_repository(setup, monkeypatch):
    _, project, repo = setup
    project.trial_path = str(repo)
    ConfigManager.save(Config(projects=[project]))
    original = CandidateGit.run
    def no_fetch(self, args):
        assert args[0] != "fetch"
        return original(self, args)
    monkeypatch.setattr(CandidateGit, "run", no_fetch)
    item = prepare(setup, source_role="trial")["item"]
    result = adopt(setup, item)["item"]
    assert result["status"] == "adopted" and result["candidate"]["role"] == "trial"
    assert git(repo, "rev-parse", "publish/first") == item["candidate_oid"]


def test_publish_capabilities_preserve_read_and_write_profile_boundaries(setup):
    from backend.core.loop.capabilities import CapabilityProfiles
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.tools.publish_service import build_publish_tools
    from tests.test_file_feedback_contract import context
    _, project, repo = setup
    observe = set(CapabilityProfiles.resolve_tools("governance.observe"))
    operate = set(CapabilityProfiles.resolve_tools("governance.operate"))
    assert {"publish_items", "publish_item"} <= observe
    assert {"publish_prepare", "publish_plan", "publish_adopt", "publish_reconcile"} <= operate
    assert not {"publish_prepare", "publish_plan", "publish_adopt", "publish_reconcile"} & observe
    tool = build_publish_tools(project)["publish_prepare"]
    ctx = context(repo, list(observe))
    result = ToolPipeline().execute({"name": tool.name, "args": {"request_id": "denied1",
        "source_role": "workspace", "source_ref": "HEAD", "target_branch": "publish/denied", "owner": "owner"}}, tool, ctx, "denied")
    assert result.is_error and read_ledger(repo)[0] is None


def test_child_context_cannot_read_or_mutate_parent_publish_metadata(setup, tmp_path_factory):
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.tools.publish_service import build_publish_tools
    from tests.test_file_feedback_contract import context
    _, project, repo = setup
    item = prepare(setup)["item"]
    child = tmp_path_factory / "child"; child.mkdir()
    for name, args in [("publish_item", {"item_id": item["id"]}),
                       ("publish_plan", {"request_id": "child1", "item_id": item["id"], "expected_revision": 1,
                         "target_branch": "publish/child", "owner": "child", "rework": "work"})]:
        tool = build_publish_tools(project)[name]
        result = ToolPipeline().execute({"name": name, "args": args}, tool, context(child, [name]), "child")
        assert result.is_error and result.data["error"] == "PUBLISH_CONTEXT_UNAVAILABLE"
        assert result.receipt["effect_state"] == "not_committed"
    assert read_ledger(repo)[0]["items"][item["id"]]["revision"] == 1
