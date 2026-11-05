"""Required check matrices, partial execution, revisions and native final-tree inputs."""
from pathlib import Path

import pytest

from backend.core.config import Config, ConfigManager
from backend.core.publish.checks import normalize, accepted
from backend.core.publish.state import PublishError, read_ledger
from backend.core.loop.publish_runtime import PublishCheckRuntime
from backend.core.loop.tool_pipeline import ToolPipeline
from tests.test_publish_commands import setup, call, git
from tests.test_publish_formal import formal_setup, prepare, command, protocol


MATRIX = [{"id": "smoke", "target": "test_release.py", "seeds": [42, 7], "timeout": 20},
          {"id": "regression", "target": "test_release.py::test_release", "seeds": [11], "timeout": 30}]


@pytest.mark.parametrize("values", [None, {}, ["test.py"], [{"target": "../test.py"}],
    [{"target": "test.py", "seeds": [True]}], [{"target": "test.py", "seeds": [1, 1]}],
    [{"target": "test.py", "timeout": False}], [{"target": "test.py", "required": False}],
    [{"id": "same", "target": "a.py"}, {"id": "same", "target": "b.py"}],
    [{"target": "test.py", "seeds": [1, 2], "timeout": 120}]])
def test_invalid_policy_never_becomes_an_execution_plan(values):
    with pytest.raises(PublishError):
        normalize(values)


def test_project_policy_roundtrips_and_cannot_be_removed_by_empty_request(formal_setup, monkeypatch):
    services, project, _ = formal_setup
    saved = services.invoke("publish.set", {"project": project.name, "section": "checks", "value": MATRIX})
    assert saved["ok"], saved
    assert services.invoke("publish.get", {"project": project.name})["checks"] == MATRIX
    item = prepare(formal_setup, test_target="", checks=[])
    assert item["plan"]["checks"] == MATRIX and item["plan"]["requested_checks"] == []
    launched = []
    protocol(monkeypatch)
    original = ToolPipeline._execute_isolated
    def invoke(self, name, args, timeout, **kwargs):
        launched.append((args["target"], args["seeds"], args["timeout"]))
        return original(self, name, args, timeout, **kwargs)
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", invoke)
    checked = command(formal_setup, "verify", item)["item"]
    assert checked["formal_eligible"]
    assert launched == [(c["target"], c["seeds"], c["timeout"]) for c in MATRIX]
    assert len(checked["evidence"][-1]["checks"]) == 2
    changed = [{**MATRIX[0], "seeds": [12]}, MATRIX[1]]
    services.invoke("publish.set", {"project": project.name, "section": "checks", "value": changed})
    assert command(formal_setup, "execute", checked)["error"] == "PUBLISH_PLAN_STALE"
    revised = prepare(formal_setup, "revise-policy", item_id=item["id"], expected_revision=checked["revision"], test_target="", checks=[])
    assert revised["plan"]["checks"] == changed and not revised["formal_eligible"]
    assert any(e["kind"] == "project_test" and not e["current"] for e in revised["evidence"])


def test_failed_member_blocks_integration_while_later_members_still_run(formal_setup, monkeypatch):
    item = prepare(formal_setup, test_target="", checks=MATRIX)
    protocol(monkeypatch)
    original = ToolPipeline._execute_isolated
    def invoke(self, name, args, timeout, **kwargs):
        result = original(self, name, args, timeout, **kwargs)
        if args["seeds"] == [42, 7]:
            result["seed_results"][1].update(exit_code=1, output_tail="seed 7 failed")
        return result
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", invoke)
    checked = command(formal_setup, "verify", item)["item"]
    rows = checked["evidence"][-1]["checks"]
    assert not checked["formal_eligible"] and [row["passed"] for row in rows] == [False, True]
    assert command(formal_setup, "execute", checked)["error"] == "PUBLISH_CHECKS_REQUIRED"


def test_partial_result_survives_crash_without_accepting_or_replaying(formal_setup, monkeypatch):
    from backend.core.loop import publish_runtime
    from backend.core.loop import publish_recovery
    item = prepare(formal_setup, test_target="", checks=MATRIX)
    protocol(monkeypatch)
    original = publish_recovery.save
    class Crash(BaseException):
        pass
    def save(storage, key, record):
        original(storage, key, record)
        if len(record.get("check_results", [])) == 1:
            raise Crash()
    monkeypatch.setattr(publish_recovery, "save", save)
    with pytest.raises(Crash):
        command(formal_setup, "verify", item)
    monkeypatch.setattr(publish_recovery, "save", original)
    monkeypatch.setattr(PublishCheckRuntime, "verify_tree", lambda *args: pytest.fail("must not replay checks"))
    checking = read_ledger(formal_setup[2])[0]["items"][item["id"]]
    recovered = command(formal_setup, "reconcile", checking)["item"]
    assert not recovered["formal_eligible"]
    assert recovered["evidence"][-1]["summary"] == "PUBLISH_CHECK_INTERRUPTED"
    assert recovered["evidence"][-1]["checks"][0]["passed"] is True


def test_incomplete_or_interrupted_aggregate_is_never_accepted():
    plan = {"checks": MATRIX}
    good = [{"check": check, "passed": True, "verification_state": "current"} for check in MATRIX]
    assert accepted(plan, {"check_results": good})
    for result in [{"check_results": good[:1]}, {"check_results": list(reversed(good))},
                   {"check_results": good, "error": "PUBLISH_CHECK_INTERRUPTED"},
                   {"check_results": good, "cleanup_pending": True}]:
        assert not accepted(plan, result)


def test_required_id_cannot_be_overridden_by_request(formal_setup):
    _, project, _ = formal_setup
    project.publish_checks = MATRIX
    ConfigManager.save(Config(projects=[project]))
    result = call(formal_setup, "prepare", "override", destination="formal", source_role="workspace", source_ref="HEAD",
        target_branch="formal", owner="maintainer", checks=[{**MATRIX[0], "seeds": [9]}])
    assert result["error"] == "PUBLISH_INVALID_CHECK_POLICY"


def test_pause_does_not_hide_pending_cleanup_or_launch_next_check(formal_setup, monkeypatch):
    item = prepare(formal_setup, test_target="", checks=MATRIX)
    protocol(monkeypatch)
    launches = []
    def invoke(self, name, args, timeout, **kwargs):
        launches.append(name)
        checking = read_ledger(formal_setup[2])[0]["items"][item["id"]]
        assert command(formal_setup, "pause", checking)["item"]["status"] == "pausing"
        kwargs["runtime_metadata"].update(launch_state="started", exit_code=0, cleanup={"complete": False, "mode": "protocol-fixture"})
        return {"seed_results": []}
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", invoke)
    pending = command(formal_setup, "verify", item)
    assert launches == ["run_test"]
    assert pending["reason"] == "SANDBOX_CLEANUP_PENDING" and pending["effect_state"] == "ambiguous"
    assert pending["item"]["status"] == "pausing"
    assert pending["item"]["operations"][-1]["state"] == "cleanup_pending"


from tests.test_native_sandbox import isolated_python
from tests.native_source_runtime import workspace_native_launcher
from tests.test_registered_test_service import pytest_native_runtime


def test_real_native_matrix_gets_fresh_final_tree_for_each_member(formal_setup, pytest_native_runtime):
    _, _, repo = formal_setup
    (repo / "test_release.py").write_text("""from pathlib import Path
def test_release():
    assert not Path('previous-check.txt').exists()
    assert not Path('dirty.txt').exists()
    assert not Path('.git').exists()
    Path('previous-check.txt').write_text('private output')
""", encoding="utf8")
    git(repo, "add", "test_release.py")
    git(repo, "commit", "-m", "test: matrix isolation")
    checks = [{**row, "seeds": [42]} for row in MATRIX]
    item = prepare(formal_setup, test_target="", checks=checks)
    (repo / "dirty.txt").write_text("preserve", encoding="utf8")
    checked = command(formal_setup, "verify", item)["item"]
    assert checked["formal_eligible"], checked["evidence"][-1]
    assert len(checked["evidence"][-1]["native_producers"]) == 2
    assert all(row["passed"] for row in checked["evidence"][-1]["checks"])
    assert command(formal_setup, "execute", checked)["item"]["status"] == "integrated"
    assert not (repo / "previous-check.txt").exists() and (repo / "dirty.txt").read_text() == "preserve"
