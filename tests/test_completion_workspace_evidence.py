"""A successful observed command need not modify a formal file to count."""
import pytest
from tests.test_workspace_candidate_service import make, fake_producer
from backend.core.loop.completion_protocol import HostCompletionEvaluator, HostCompletionEvidence


def required(ctx):
    ctx.process.update_context_snapshot(lambda s: {**s, "task_contract": {"required_tool_calls": [{"tool_name": "exec_command", "min_calls": 1}]}})
    reasons = []
    HostCompletionEvaluator._verify_required_tool_calls(ctx.process, reasons)
    return reasons


def test_readonly_native_command_counts_without_manufacturing_a_write(tmp_path_factory, monkeypatch):
    fake_producer(monkeypatch, tmp_path_factory)
    ctx, call = make(tmp_path_factory)
    result = call({"argv": ["python", "--version"]})
    assert not result.is_error
    assert result.receipt["effect_state"] == "not_committed"
    ctx.process.tool_receipts = [result.receipt]
    assert required(ctx) == []
    assert HostCompletionEvidence.collect(ctx.process).action_receipt_ids == ()


@pytest.mark.parametrize("change", [
    {"succeeded": False}, {"producer_completed": None}, {"producer_exit_code": 1},
    {"input_applicability": "changed"}, {"output_publication": "withheld"},
    {"output_publication": "unknown"}, {"file_service_result": False},
    {"task_id": "another-task"},
])
def test_unfinished_stale_failed_or_other_task_call_does_not_count(tmp_path_factory, monkeypatch, change):
    fake_producer(monkeypatch, tmp_path_factory)
    ctx, call = make(tmp_path_factory)
    result = call({"argv": ["python", "--version"]})
    ctx.process.tool_receipts = [{**result.receipt, **change}]
    assert required(ctx)
