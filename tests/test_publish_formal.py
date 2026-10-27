"""Actual Git integration, with protocol fault injection and real native tests."""
from pathlib import Path
import pytest

from tests.test_publish_commands import setup, call, git
from backend.core.config import Config, ConfigManager
from backend.core.publish.git import CandidateGit
from backend.core.publish.state import read_ledger
from backend.core.loop.tool_pipeline import ToolPipeline


@pytest.fixture
def formal_setup(setup):
    _, project, repo = setup
    project.backup_path = str(repo)
    git(repo, "branch", "formal")
    (repo / "test_release.py").write_text("def test_release():\n    assert True\n", encoding="utf8")
    git(repo, "add", "test_release.py")
    git(repo, "commit", "-m", "[ANBM-5] feat: release candidate")
    ConfigManager.save(Config(projects=[project]))
    return setup


def prepare(setup, request="formal1", **args):
    result = call(setup, "prepare", request, **{"destination": "formal", "method": "squash",
        "source_role": "workspace", "source_ref": "HEAD", "target_branch": "formal", "owner": "maintainer",
        "test_target": "test_release.py", **args})
    assert "item" in result, result
    return result["item"]


def command(setup, name, item, request=None):
    return call(setup, name, request or name + str(item["revision"]), item_id=item["id"], expected_revision=item["revision"])


def protocol(monkeypatch, *, passed=True, action=None):
    def invoke(self, name, args, timeout, **kwargs):
        assert name == "run_test"
        stage = Path(args["_workspace"])
        assert not (stage / ".git").exists()
        if action:
            action(stage)
        kwargs["runtime_metadata"].update(launch_state="started", exit_code=0, cleanup={"complete": True, "mode": "protocol-fixture"})
        return {"seed_results": [{"seed": seed, "exit_code": 0 if passed else 1, "duration_ms": 1,
                                  "output_tail": "passed" if passed else "failed"} for seed in args["seeds"]]}
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", invoke)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.prepare", lambda self: self)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.close", lambda self: None)


def test_squash_final_tree_and_numbered_commit_leave_working_tree_unchanged(formal_setup, monkeypatch):
    _, _, repo = formal_setup
    original = git(repo, "rev-parse", "HEAD")
    target = git(repo, "rev-parse", "formal")
    item = prepare(formal_setup)
    assert not item["formal_eligible"] and item["plan"]["expected_target_oid"] == target
    assert command(formal_setup, "execute", item)["error"] == "PUBLISH_CHECKS_REQUIRED"
    # Dirty/source HEAD changes after preparation never enter the final tree.
    (repo / "uncommitted.txt").write_text("retain me", encoding="utf8")
    git(repo, "commit", "--allow-empty", "-m", "later source")
    later = git(repo, "rev-parse", "HEAD")
    protocol(monkeypatch, action=lambda stage: assert_snapshot(stage))
    checked = command(formal_setup, "verify", item)["item"]
    assert checked["formal_eligible"], checked["evidence"][-1]["summary"]
    result = command(formal_setup, "execute", checked, "execute-again")["item"]
    observed = git(repo, "rev-parse", "formal")
    assert result["status"] == "integrated" and observed != original
    assert git(repo, "show", "-s", "--format=%P", observed) == target
    assert git(repo, "rev-parse", "formal^{tree}") == checked["plan"]["tree_oid"]
    assert git(repo, "show", "-s", "--format=%B", observed) == checked["plan"]["commit_message"]
    assert git(repo, "rev-parse", "HEAD") == later
    assert (repo / "uncommitted.txt").read_text() == "retain me"
    assert result["recorded"]["pushed"] is False
    assert command(formal_setup, "execute", checked, "execute-again")["item"]["status"] == "integrated"


def assert_snapshot(stage):
    assert (stage / "test_release.py").read_text() == "def test_release():\n    assert True\n"
    assert not (stage / "uncommitted.txt").exists()


def test_failed_final_test_blocks_formal_and_retains_evidence(formal_setup, monkeypatch):
    _, _, repo = formal_setup
    old = git(repo, "rev-parse", "formal")
    item = prepare(formal_setup)
    protocol(monkeypatch, passed=False)
    checked = command(formal_setup, "verify", item)["item"]
    assert checked["reason"] == "FORMAL_CHECKS_FAILED" and not checked["formal_eligible"]
    assert command(formal_setup, "execute", checked)["error"] == "PUBLISH_CHECKS_REQUIRED"
    assert git(repo, "rev-parse", "formal") == old
    protocol(monkeypatch, passed=True)
    repeated = command(formal_setup, "verify", checked)["item"]
    assert repeated["formal_eligible"] and [e["outcome"] for e in repeated["evidence"] if e["kind"] == "project_test"] == ["failed", "passed"]


def test_test_rewriting_its_private_source_cannot_pass(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    protocol(monkeypatch, action=lambda stage: (stage / "test_release.py").write_text("forged test\n"))
    checked = command(formal_setup, "verify", item)["item"]
    assert not checked["formal_eligible"]
    assert checked["evidence"][-1]["summary"] == "FILE_CHANGED"


def test_unconfirmed_native_cleanup_keeps_recoverable_intent_and_blocks_integration(formal_setup, monkeypatch):
    from backend.core.loop.publish_runtime import PublishCheckRuntime
    item = prepare(formal_setup)
    monkeypatch.setattr(PublishCheckRuntime, "verify_tree", lambda *args: {"cleanup_pending": True})
    pending = command(formal_setup, "verify", item)
    assert pending["effect_state"] == "ambiguous"
    item = pending["item"]
    assert item["reason"] == "CHECK_OPERATION_INCOMPLETE" and item["allowed_actions"] == ["pause", "reconcile"]
    assert item["operations"][-1]["state"] == "cleanup_pending"
    assert item["operations"][-1]["runtime_owner"][0].startswith("publish-local-")
    assert command(formal_setup, "execute", item)["error"] == "PUBLISH_ACTION_UNAVAILABLE"
    assert command(formal_setup, "verify", item, "repeat-check")["error"] == "PUBLISH_ACTION_UNAVAILABLE"


def test_squash_filters_private_files_before_final_tree_testing(formal_setup, monkeypatch):
    _, _, repo = formal_setup
    (repo / "CLAUDE.md").write_text("private instructions\n", encoding="utf8")
    git(repo, "add", "CLAUDE.md"); git(repo, "commit", "-m", "private tool setup")
    item = prepare(formal_setup)
    assert len(item["plan"]["excluded_path_fingerprints"]) == 1
    assert item["plan"]["tree_oid"] != item["candidate"]["tree_oid"]
    def check(stage):
        assert not (stage / "CLAUDE.md").exists()
    protocol(monkeypatch, action=check)
    checked = command(formal_setup, "verify", item)["item"]
    result = command(formal_setup, "execute", checked)["item"]
    assert result["status"] == "integrated"
    assert "CLAUDE.md" not in git(repo, "ls-tree", "-r", "--name-only", "formal")
    assert "private tool setup" not in git(repo, "log", "--format=%s", "formal")


def test_policy_and_target_drift_invalidate_prepared_acceptance(formal_setup, monkeypatch):
    _, project, repo = formal_setup
    item = prepare(formal_setup)
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    project.outbound_policy["version"] += 1
    ConfigManager.save(Config(projects=[project]))
    assert command(formal_setup, "execute", checked, "stale-policy")["error"] == "PUBLISH_PLAN_STALE"
    project.outbound_policy["version"] -= 1
    ConfigManager.save(Config(projects=[project]))
    git(repo, "update-ref", "refs/heads/formal", "HEAD")
    assert command(formal_setup, "execute", checked, "stale-target")["error"] == "PUBLISH_TARGET_CHANGED"


def test_formal_lost_ack_reconciles_exact_planned_oid_without_repeating(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    original = CandidateGit.integrate
    def lost_ack(self, *args):
        result = original(self, *args)
        assert result.returncode == 0, result.stderr
        raise OSError("lost return")
    monkeypatch.setattr(CandidateGit, "integrate", lost_ack)
    pending = command(formal_setup, "execute", checked)["item"]
    assert pending["status"] == "executing"
    monkeypatch.setattr(CandidateGit, "integrate", lambda *args: pytest.fail("repeated write"))
    reconciled = command(formal_setup, "reconcile", pending)["item"]
    assert reconciled["status"] == "integrated"
    assert reconciled["operations"][-1]["attribution"] == "matching_ref_observed"


def test_cross_repo_squash_imports_only_result_and_preserves_checkouts(formal_setup, tmp_path_factory, monkeypatch):
    _, project, repo = formal_setup
    release = tmp_path_factory / "release"
    git(repo, "clone", "--no-hardlinks", str(repo), str(release))
    git(release, "branch", "formal", "origin/formal")
    project.backup_path = str(release)
    ConfigManager.save(Config(projects=[project]))
    before = git(release, "rev-parse", "HEAD")
    configuration = (release / ".git/config").read_bytes()
    item = prepare(formal_setup)
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    result = command(formal_setup, "execute", checked)["item"]
    assert result["status"] == "integrated", result
    assert git(release, "rev-parse", "formal^{tree}") == item["plan"]["tree_oid"]
    assert git(release, "rev-parse", "HEAD") == before
    assert (release / ".git/config").read_bytes() == configuration
    assert not (release / ".git/FETCH_HEAD").exists()


def test_fast_forward_retains_exact_oid_and_requires_formatted_history(formal_setup, monkeypatch):
    _, _, repo = formal_setup
    item = prepare(formal_setup, method="ff")
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    result = command(formal_setup, "execute", checked)["item"]
    assert result["status"] == "integrated"
    assert git(repo, "rev-parse", "formal") == item["candidate_oid"]


@pytest.mark.parametrize("mode,expected", [("plugin", "PUBLISH_PLUGIN_MISSING"),
    ("template", "PUBLISH_TEMPLATE_UNAVAILABLE"), ("checked-out", "PUBLISH_TARGET_CHECKED_OUT"),
    ("test-escape", "PUBLISH_INVALID_TEST_TARGET")])
def test_unsupported_dependency_or_target_never_moves_formal(formal_setup, mode, expected):
    _, project, repo = formal_setup
    old = git(repo, "rev-parse", "formal")
    args = {}
    if mode == "plugin": project.commit_format["plugins"] = ["required-plugin"]
    if mode == "template": project.commit_format["template_name"] = "missing"
    if mode == "checked-out": git(repo, "switch", "formal")
    if mode == "test-escape": args["test_target"] = "../test_release.py"
    ConfigManager.save(Config(projects=[project]))
    result = call(formal_setup, "prepare", "blocked", destination="formal", method="squash", source_role="workspace",
        source_ref="HEAD", target_branch="formal", owner="maintainer", test_target=args.get("test_target", "test_release.py"))
    assert result["error"] == expected, result
    assert git(repo, "rev-parse", "formal") == old


def test_revise_preserves_old_tree_checks_but_requires_new_verification(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    revised = prepare(formal_setup, "revision2", item_id=checked["id"], expected_revision=checked["revision"])
    assert revised["binding"] != checked["binding"] and not revised["formal_eligible"]
    assert any(e["kind"] == "project_test" for e in revised["evidence"])


def test_secret_in_message_blocks_integration_without_persisting_raw_secret(formal_setup):
    _, _, repo = formal_setup
    token = "ghp_" + "x" * 36
    git(repo, "commit", "--allow-empty", "-m", "feat: " + token)
    item = prepare(formal_setup)
    assert item["reason"] == "FORMAL_CHECKS_FAILED"
    assert token not in str(read_ledger(repo)[0])
    assert command(formal_setup, "execute", item)["error"] == "PUBLISH_CHECKS_REQUIRED"


def test_ff_rejects_private_content_removed_from_final_tree(formal_setup):
    _, _, repo = formal_setup
    (repo / "CLAUDE.md").write_text("private tool data\n")
    git(repo, "add", "CLAUDE.md"); git(repo, "commit", "-m", "[ANBM-6] chore: private data")
    git(repo, "rm", "CLAUDE.md"); git(repo, "commit", "-m", "[ANBM-7] chore: remove private data")
    result = call(formal_setup, "prepare", "ff-private-history", destination="formal", method="ff", source_role="workspace",
        source_ref="HEAD", target_branch="formal", owner="maintainer", test_target="test_release.py")
    assert result["error"] == "PUBLISH_FF_REQUIRES_FILTERING"


def test_configured_formal_outside_workspace_needs_existing_resource_grant(formal_setup, tmp_path_factory):
    from backend.core.tools.publish_service import build_publish_tools
    from tests.test_file_feedback_contract import context
    _, project, repo = formal_setup
    release = tmp_path_factory / "release"
    git(repo, "clone", str(repo), str(release))
    project.backup_path = str(release)
    ConfigManager.save(Config(projects=[project]))
    tool = build_publish_tools(project)["publish_prepare"]
    args = {"request_id": "agent-formal", "destination": "formal", "method": "squash", "source_role": "workspace",
            "source_ref": "HEAD", "target_branch": "formal", "owner": "maintainer", "test_target": "test_release.py"}
    result = ToolPipeline().execute({"name": tool.name, "args": args}, tool, context(repo, [tool.name]), "scope")
    assert result.is_error and result.diagnostics["code"] == "RESOURCE_SCOPE_APPROVAL_REQUIRED"
    assert read_ledger(repo)[0] is None


def test_agent_uses_same_native_runtime_and_records_task_identity(formal_setup, monkeypatch):
    from backend.core.tools.publish_service import build_publish_tools
    from tests.test_file_feedback_contract import context
    _, project, repo = formal_setup
    item = prepare(formal_setup)
    protocol(monkeypatch)
    tools = build_publish_tools(project)
    ctx = context(repo, ["publish_verify", "publish_execute"])
    tool = tools["publish_verify"]
    result = ToolPipeline().execute({"name": tool.name, "args": {"request_id": "agent-check", "item_id": item["id"],
        "expected_revision": item["revision"]}}, tool, ctx, "check")
    assert not result.is_error, result.formatted
    assert result.data["item"]["formal_eligible"]
    evidence = result.data["item"]["evidence"][-1]
    assert evidence["runtime_owner"] == [ctx.process.process_id, ctx.process.active_task_id]
    assert result.receipt["native_producers"][0]["launch_state"] == "started"
    tool = tools["publish_execute"]
    result = ToolPipeline().execute({"name": tool.name, "args": {"request_id": "agent-integrate", "item_id": item["id"],
        "expected_revision": result.data["item"]["revision"]}}, tool, ctx, "integrate")
    assert not result.is_error and result.data["item"]["status"] == "integrated", result.formatted
    assert result.data["item"]["operations"][-1]["task_id"] == ctx.process.active_task_id


def test_custom_template_and_existing_counter_share_number_authority(formal_setup):
    from backend.core.template_manager import CommitTemplate, TemplateManager
    from backend.core.sync_session.state import reserve_formal_number
    _, project, repo = formal_setup
    TemplateManager.save([CommitTemplate(name="team", prefix_override="TEAM", body_format="Count: {commit_count}")])
    project.commit_format.update(template_name="team", number_start=50)
    ConfigManager.save(Config(projects=[project]))
    item = prepare(formal_setup)
    assert item["plan"]["commit_message"] == "[TEAM-50] feat: release candidate\n\nCount: 1"
    assert item["plan"]["number"] == 50
    assert reserve_formal_number(repo, 0) == 51


@pytest.mark.parametrize("existing", [70, 10000])
def test_number_reservation_observes_fixed_git_history_without_truncation(formal_setup, existing):
    from backend.core.operations.git import _find_next_number
    _, _, repo = formal_setup
    git(repo, "commit", "--allow-empty", "-m", f"[ANBM-{existing}] fix: prior numbered output")
    candidate = git(repo, "rev-parse", "HEAD")
    # A different live HEAD must not choose the fixed candidate's number floor.
    git(repo, "switch", "--detach", "formal")
    item = prepare(formal_setup, source_ref=candidate)
    assert item["plan"]["number"] == existing + 1
    assert _find_next_number(prefix="ANBM", git_runner=CandidateGit(repo).runner, revision=candidate) == existing + 1


from tests.test_native_sandbox import isolated_python
from tests.native_source_runtime import workspace_native_launcher
from tests.test_registered_test_service import pytest_native_runtime


def test_real_native_final_tree_test_and_local_formal_integration(formal_setup, pytest_native_runtime, monkeypatch):
    from backend.core.loop.registered_test_runtime import RegisteredTestRuntime
    original = RegisteredTestRuntime.run
    def diagnosed(self, args):
        try:
            return original(self, args)
        except Exception as exc:
            raise AssertionError("Native check failed before evidence recording") from exc
    monkeypatch.setattr(RegisteredTestRuntime, "run", diagnosed)
    _, _, repo = formal_setup
    (repo / "test_release.py").write_text("""from pathlib import Path
def test_release():
    assert not Path('.git').exists()
    assert not Path('uncommitted.txt').exists()
    Path('discarded.txt').write_text('temporary output')
""", encoding="utf8")
    git(repo, "add", "test_release.py"); git(repo, "commit", "-m", "test: isolated candidate")
    item = prepare(formal_setup)
    (repo / "uncommitted.txt").write_text("local only", encoding="utf8")
    checked = command(formal_setup, "verify", item)
    assert checked.get("item", {}).get("formal_eligible"), checked.get("item", {}).get("evidence", [{}])[-1].get("summary", str(checked))
    assert "1 passed" in checked["item"]["evidence"][-1]["summary"]
    integrated = command(formal_setup, "execute", checked["item"])
    assert integrated["item"]["status"] == "integrated", integrated
    assert not (repo / "discarded.txt").exists()
    assert "discarded.txt" not in git(repo, "ls-tree", "-r", "--name-only", "formal")
