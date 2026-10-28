"""Durable sequences, live authority, background delivery and no crash replay."""
import threading
import time

import pytest

from tests.test_publish_commands import setup, call, git
from tests.test_publish_formal import formal_setup, prepare, protocol
from backend.core.publish.service import PublishService
from backend.core.publish.state import read_ledger, Repository
from backend.core.storage import get_storage
from backend.core.loop.publish_queue import drive, ACTIVE


@pytest.fixture
def queued(formal_setup):
    yield formal_setup
    formal_setup[0].publish_runtime.close()


def run(setup, item, request="run1", steps=None):
    return call(setup, "run", request, item_id=item["id"], expected_revision=item["revision"],
                steps=steps or ["verify", "execute"])


def settled(setup, item):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        saved = read_ledger(setup[2])[0]["items"][item["id"]]
        if saved["run"]["state"] not in ACTIVE:
            return saved
        time.sleep(.02)
    pytest.fail("Publish Runtime did not settle")


def test_background_sequence_has_one_execution_and_durable_step_receipts(queued, monkeypatch):
    item = prepare(queued)
    protocol(monkeypatch)
    accepted = run(queued, item)
    assert accepted["item"]["run"]["state"] == "queued" and "_new_run" not in accepted
    finished = settled(queued, item)
    assert finished["status"] == "integrated" and finished["run"]["state"] == "completed"
    assert [row["operation"] for row in finished["run"]["observations"]] == ["verify", "execute"]
    target = git(queued[2], "rev-parse", "formal")
    assert run(queued, item) == accepted
    assert git(queued[2], "rev-parse", "formal") == target
    assert len(finished["operations"]) == 2


def test_failure_stops_automatic_integration_and_preserves_failure(queued, monkeypatch):
    item = prepare(queued)
    previous = git(queued[2], "rev-parse", "formal")
    protocol(monkeypatch, passed=False)
    run(queued, item)
    finished = settled(queued, item)
    assert finished["run"]["state"] == "blocked"
    assert finished["run"]["reason"] == "PUBLISH_CHECKS_REQUIRED"
    assert len(finished["run"]["observations"]) == 1
    assert git(queued[2], "rev-parse", "formal") == previous
    assert finished["evidence"][-1]["outcome"] == "failed"


def test_background_check_can_pause_and_live_run_rejects_other_writers(queued, monkeypatch):
    started, release = threading.Event(), threading.Event()
    protocol(monkeypatch, action=lambda _: (started.set(), release.wait(15)))
    item = prepare(queued)
    run(queued, item)
    try:
        assert started.wait(10)
        latest = read_ledger(queued[2])[0]["items"][item["id"]]
        result = call(queued, "reconcile", "while-live", item_id=item["id"], expected_revision=latest["revision"])
        assert result["error"] == "PUBLISH_OPERATION_RUNNING"
        paused = call(queued, "pause", "pause", item_id=item["id"], expected_revision=latest["revision"])["item"]
        assert paused["status"] == "pausing" and paused["run"]["state"] == "stopping"
    finally:
        release.set()
    finished = settled(queued, item)
    assert finished["status"] == "paused" and len(finished["operations"]) == 1
    resumed = call(queued, "resume", "resume", item_id=item["id"], expected_revision=finished["revision"])["item"]
    assert resumed["status"] == "prepared" and len(resumed["operations"]) == 1


def test_runtime_shutdown_cancels_checks_and_never_starts_next_step(queued, monkeypatch):
    started, release = threading.Event(), threading.Event()
    protocol(monkeypatch, action=lambda _: (started.set(), release.wait(15)))
    item = prepare(queued)
    run(queued, item)
    assert started.wait(10)
    closer = threading.Thread(target=queued[0].publish_runtime.close)
    closer.start()
    try:
        assert queued[0].publish_runtime._closed.wait(5)
    finally:
        release.set()
        closer.join(15)
    assert not closer.is_alive()
    finished = settled(queued, item)
    assert finished["status"] == "prepared" and len(finished["operations"]) == 1
    assert finished["evidence"][-1]["outcome"] == "failed"


def test_saved_queue_is_not_restart_authority_and_requires_reconciliation(queued, monkeypatch):
    item = prepare(queued)
    project = queued[1]
    args = {"request_id": "run1", "item_id": item["id"], "expected_revision": item["revision"], "steps": ["verify", "execute"]}
    saved = PublishService(project, actor="local_operator").command("run", args)["item"]
    monkeypatch.setattr(PublishService, "_verify", lambda *a, **k: pytest.fail("saved config restarted a check"))
    for _ in range(2):
        assert run(queued, item)["item"]["run"]["state"] == "queued"
    assert read_ledger(queued[2])[0]["items"][item["id"]]["revision"] == saved["revision"]
    blocked = call(queued, "execute", "manual-race", item_id=item["id"], expected_revision=saved["revision"])
    assert blocked["error"] in {"PUBLISH_RUN_ACTIVE", "PUBLISH_CHECKS_REQUIRED"}
    reconciled = call(queued, "reconcile", "recover", item_id=item["id"], expected_revision=saved["revision"])["item"]
    assert reconciled["run"]["state"] == "interrupted" and reconciled["status"] == "prepared"


def test_lost_step_ack_never_replays_and_reconcile_only_observes(queued, monkeypatch):
    from backend.core.tools.publish_service import invoke_local_publish
    item = prepare(queued)
    protocol(monkeypatch)
    job = PublishService(queued[1], actor="local_operator").command("run", {
        "request_id": "run1", "item_id": item["id"], "expected_revision": item["revision"], "steps": ["verify", "execute"]})["item"]["run"]
    def dispatch(operation, args):
        invoke_local_publish(queued[1], operation, args)
        raise OSError("lost acknowledgement after completed check")
    result = drive(str(queued[2]), item["id"], job["id"], dispatch)["item"]
    assert result["run"]["current"] and result["status"] == "prepared"
    assert result["allowed_actions"] == ["reconcile"]
    monkeypatch.setattr(PublishService, "_verify", lambda *a, **k: pytest.fail("replayed a native check"))
    recovered = call(queued, "reconcile", "recover", item_id=item["id"], expected_revision=result["revision"])["item"]
    assert recovered["formal_eligible"] and len(recovered["operations"]) == 1
    assert recovered["run"]["state"] == "interrupted"


def test_changed_reviewed_binding_stops_after_check(queued, monkeypatch):
    item = prepare(queued)
    protocol(monkeypatch)
    original = PublishService._verify
    def changing(service, **args):
        result = original(service, **args)
        ledger, ref = service.repo.load()
        ledger["items"][args["item_id"]]["binding"] = "changed-by-plugin"
        service.repo.save(ledger, ref)
        return result
    monkeypatch.setattr(PublishService, "_verify", changing)
    run(queued, item)
    finished = settled(queued, item)
    assert finished["run"]["state"] == "blocked" and len(finished["operations"]) == 1


def test_agent_steps_reenter_pipeline_and_keep_original_owner(queued, monkeypatch):
    from tests.test_file_feedback_contract import context
    from backend.core.tools.publish_service import build_publish_tools
    from backend.core.loop.tool_pipeline import ToolPipeline
    item = prepare(queued)
    protocol(monkeypatch)
    catalog = build_publish_tools(queued[1])
    ctx = context(queued[2], ["publish_run", "publish_verify", "publish_execute"])
    ctx.artifacts["tool_catalog"] = catalog
    result = ToolPipeline().execute({"name": "publish_run", "args": {"request_id": "agent-sequence", "item_id": item["id"],
        "expected_revision": item["revision"], "steps": ["verify", "execute"]}}, catalog["publish_run"], ctx, "sequence")
    assert not result.is_error, result.formatted
    finished = result.data["item"]
    assert finished["status"] == "integrated" and finished["run"]["state"] == "completed", result.data
    assert all(op["actor"] == ctx.process.process_id and op["task_id"] == ctx.process.active_task_id for op in finished["operations"])
    assert len(result.receipt["child_receipts"]) == 2


def test_agent_removed_capability_blocks_following_step(queued, monkeypatch):
    from tests.test_file_feedback_contract import context
    from backend.core.tools.publish_service import build_publish_tools
    from backend.core.loop.tool_pipeline import ToolPipeline
    item = prepare(queued)
    catalog = build_publish_tools(queued[1])
    ctx = context(queued[2], ["publish_run", "publish_verify", "publish_execute"])
    ctx.artifacts["tool_catalog"] = catalog
    protocol(monkeypatch, action=lambda _: ctx.process.tool_registry.remove_scoped("publish_execute"))
    result = ToolPipeline().execute({"name": "publish_run", "args": {"request_id": "revoked", "item_id": item["id"],
        "expected_revision": item["revision"], "steps": ["verify", "execute"]}}, catalog["publish_run"], ctx, "revoked")
    finished = result.data["item"]
    assert finished["status"] == "prepared" and finished["run"]["state"] == "blocked"
    assert len(finished["operations"]) == 1


@pytest.mark.parametrize("steps", [["execute", "verify"], ["verify", "verify"], ["adopt"], []])
def test_unreviewable_sequences_are_rejected(queued, steps):
    item = prepare(queued)
    result = call(queued, "run", "bad", item_id=item["id"], expected_revision=item["revision"], steps=steps)
    assert result["error"] == "PUBLISH_INVALID_STEPS"
    assert not read_ledger(queued[2])[0]["items"][item["id"]].get("run")


from tests.test_native_sandbox import isolated_python
from tests.native_source_runtime import workspace_native_launcher
from tests.test_registered_test_service import pytest_native_runtime


def test_real_native_background_run_checks_environment_then_integrates(queued, pytest_native_runtime):
    item = prepare(queued)
    previous = git(queued[2], "rev-parse", "formal")
    run(queued, item)
    finished = settled(queued, item)
    assert finished["status"] == "integrated", finished["run"]
    environment = next(e for e in finished["evidence"] if e["kind"] == "environment")
    assert environment["outcome"] == "passed" and environment["environment"]["file_count"] > 100
    tests = next(e for e in finished["evidence"] if e["kind"] == "project_test")
    assert tests["outcome"] == "passed" and "1 passed" in tests["summary"]
    assert tests["native_producers"][0]["launch_state"] == "started"
    result = git(queued[2], "rev-parse", "formal")
    assert result != previous and git(queued[2], "show", "-s", "--format=%P", result) == previous
