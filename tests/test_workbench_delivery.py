from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.core.loop.alignment_evidence import answer_digest
from backend.core.loop.completion_protocol import CompletionClaim, HostCompletionEvaluator
from backend.core.loop.context_store import ContextObjectStore
from backend.core.loop.delivery_runtime import DeliveryRuntime
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.loop_guard import LoopGuard
from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel
from backend.core.loop.protocol_tools.registry import build_internal_tools
from backend.core.loop.protocol_tools.workbench import bound_sources
from backend.core.loop.session import AgentSession
from backend.core.loop.tool_outcome_runtime import ToolOutcomeRuntime
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry
from backend.core.storage import StorageRuntime


@pytest.fixture
def workbench(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as storage:
        process = AgentProcess(process_id="p", role="worker", ring_level=RingLevel.RING_3,
            session=AgentSession(session_id="s"), active_task_id="task", runtime_storage=storage,
            worktree_path=str(tmp_path_factory), tool_registry=ToolRegistry(["workbench"]))
        workspace = str(tmp_path_factory)
        tools = build_internal_tools(process, {}, workspace, None, None, None)
        ctx = ExecutionContext(process=process, session=process.session, workspace_path=workspace, storage=storage)
        pipeline = ToolPipeline()

        def call(operation, **args):
            process.steps_used += 1
            result = pipeline.execute({"name": "workbench", "args": {"operation": operation, **args}},
                                      tools["workbench"], ctx, operation)
            assert not result.is_error, result.formatted
            return result.data

        yield process, ContextObjectStore(workspace), call


def _aggregate(call, sheet, cell_id, reducer, **source):
    return call("aggregate", sheet_id=sheet["sheet_id"], expected_revision=sheet["revision"],
                cell_id=cell_id, selector="", reducer=reducer, **source)


def _import(call, contexts, logical_name, value):
    sheet = call("create_sheet", title=logical_name)
    source = contexts.put(logical_name, value)
    return call("import_context", sheet_id=sheet["sheet_id"], expected_revision=sheet["revision"],
                cell_id="items", source_ref=source["pinned"])


def _candidate(line, note=""):
    return "```text\n" + line + "\n```" + ("\n\n" + note if note else "")


def _delivery(process):
    def increment(p, key):
        p._nudge_counters[key] = p._nudge_counters.get(key, 0) + 1

    return DeliveryRuntime(process, process.session, LoopGuard(), None, emit=lambda _: None,
        apply_mailbox=lambda: None, pause_blocked=lambda *_: None, capture_recovery=lambda _: None,
        increment_nudge=increment, nudge_count=lambda p, key: p._nudge_counters.get(key, 0))


def test_pinned_set_reuse_across_sheets_and_visible_delivery_repair(workbench):
    process, contexts, call = workbench
    source = _import(call, contexts, "test/completed", ["A", "B", "A", "C"])
    members = _aggregate(call, source, "members", "distinct", source_cell_id="items")
    second = call("create_sheet", title="Delivery")
    total = _aggregate(call, second, "total", "count", source_result=members["result_ref"])
    assert total["result"]["value"] == 3
    resolved = call("read_result", result_ref=total["result_ref"])
    assert resolved["dependencies"][0] == members["result_ref"]
    assert resolved["source"]["logical_name"] == "test/completed"

    bad = _candidate("Completed independent tasks: 4")
    checked = call("check_delivery", scope="Completed independent tasks", result_ref=total["result_ref"], candidate=bad)
    assert checked["status"] == "contradiction"
    delivery = _delivery(process)
    window = {"action": "continue", "usage_ratio": 0}
    assert delivery.evaluate_text(bad, 0, window, []).retry
    assert delivery.evaluate_text("All four tasks finished.", 0, window, []).retry
    assert process.status is ProcessStatus.RUNNING

    good = _candidate("Completed independent tasks: 3", "These are the recorded independent tasks.")
    accepted = delivery.evaluate_text(good, 0, window, [])
    assert process.status is ProcessStatus.COMPLETED
    assert accepted.result["workbench_checks"][0]["status"] == "match"
    assert accepted.result["workbench_checks"][0]["candidate_digest"] == answer_digest(good)


def test_set_comparison_and_tool_completion_use_delivered_text(workbench):
    process, contexts, call = workbench
    source = _import(call, contexts, "test/set", [1, "1", True, 1.0])
    members = _aggregate(call, source, "members", "distinct", source_cell_id="items")
    candidate = _candidate('Items: [true, "1", 1.0]')
    assert call("check_delivery", scope="Items", result_ref=members["result_ref"], candidate=candidate)["status"] == "match"
    wrong = _candidate('Items: [true, "2", 1]')
    process.completion_claim = CompletionClaim.from_args({"result": wrong}, step=0)
    completion = SimpleNamespace(tool_name="complete_task", is_error=False, data={"accepted": True})
    runtime = ToolOutcomeRuntime(process, process.session, _delivery(process), lambda _: None,
                                 lambda *_: None, lambda _: None)
    assert runtime.evaluate([completion], "", 0, {"action": "continue", "usage_ratio": 0}, []).retry
    assert process.status is ProcessStatus.RUNNING
    assert process.completion_claim is None


def test_source_change_rechecks_affected_chain_and_contract_cannot_be_omitted(workbench):
    process, contexts, call = workbench
    source = _import(call, contexts, "test/count", [1, 2, 3])
    result = _aggregate(call, source, "total", "count", source_cell_id="items")
    descendant = _aggregate(call, result, "reused", "sum", source_cell_id="total")
    unrelated = _import(call, contexts, "test/other", [8])
    other = _aggregate(call, unrelated, "other", "sum", source_cell_id="items")
    candidate = _candidate("Count: 3")
    assert call("check_delivery", scope="Count", result_ref=descendant["result_ref"], candidate=candidate)["status"] == "match"

    contexts.put("test/count", [1, 2, 3, 4])
    evaluation = HostCompletionEvaluator.evaluate(process, candidate)
    assert not evaluation.allowed and evaluation.workbench_checks[0]["status"] == "stale"
    assert call("read_result", result_ref=other["result_ref"])["applicability"]["state"] == "current"
    assert call("read_result", result_ref=result["result_ref"])["result"]["value"] == 3
    assert HostCompletionEvaluator.evaluate(process, _candidate("Count: unverified")).allowed

    process.update_context_snapshot(lambda context: {**context,
        "task_contract": {"required_workbench_claims": ["Count", "Unbound"]}})
    evaluation = HostCompletionEvaluator.evaluate(process, _candidate("Count: unverified"))
    assert not evaluation.allowed
    assert [(check["scope"], check["status"]) for check in evaluation.workbench_checks] == [
        ("Count", "stale"), ("Unbound", "insufficient")]
    historical = _candidate("Count (historical snapshot): 3")
    assert call("check_delivery", scope="Count", result_ref=descendant["result_ref"],
                basis="historical", candidate=historical)["status"] == "match"


def test_unverified_model_data_scope_and_owner_boundary(workbench):
    process, _, call = workbench
    sheet = call("create_sheet", title="Model input")
    source = call("add_data", sheet_id=sheet["sheet_id"], expected_revision=sheet["revision"],
                  cell_id="items", json_text="[1, 2, 3]", description="Model transcription")
    result = _aggregate(call, source, "total", "count", source_cell_id="items")
    assert call("check_delivery", scope="Count", result_ref=result["result_ref"],
                candidate=_candidate("Count: 3"))["status"] == "insufficient"
    context, _ = process.read_context_snapshot()
    foreign = AgentProcess(process_id="child", role="worker", parent_id=process.process_id,
        ring_level=RingLevel.RING_3, active_task_id=process.active_task_id, session=process.session,
        context_snapshot=deepcopy(context), runtime_storage=process.bound_storage)
    evaluation = HostCompletionEvaluator.evaluate(foreign, _candidate("Count: 3"))
    assert evaluation.workbench_checks == ()
    with pytest.raises(KeyError):
        bound_sources(foreign, process.worktree_path).read_result(result["result_ref"])

    process.active_task_id = "next-task"
    assert HostCompletionEvaluator.evaluate(process, "Simple answer").workbench_checks == ()
