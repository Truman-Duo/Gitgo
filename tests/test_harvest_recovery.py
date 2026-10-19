"""Recovery and attribution regressions for first-time experience harvest."""
import json
from types import SimpleNamespace

import pytest

from backend.core.knowledge import harvest
from backend.core.knowledge.applicability import assess_lesson
from backend.core.knowledge.summary import HarvestBudgetExceeded, HarvestSummaryInvalid
from backend.core.loop.provider_protocol import ProviderResponseIncomplete


def candidate(**extra):
    from tests.knowledge_fixtures import fields
    return {**fields(["one"]), "trigger": "app.py", "rule": "When changing initialization, run the regression and verify readiness", **extra}


class Provider:
    max_output_tokens = 8192
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
    def chat(self, messages, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def summarize(ws, provider, signals=None, report=None):
    return harvest.harvest_llm_summary(signals or [{"signal_id": "one", "target_files": ["app.py"]}],
        provider, str(ws), "project", raise_on_error=True, summary_report=report)


def test_truncation_recovers_once_and_preserves_both_usage_reports(tmp_path_factory):
    report = {}
    provider = Provider(ProviderResponseIncomplete("max_output_tokens", {"output_tokens": 4096}),
                        {"content": json.dumps([candidate(source_signal_ids=["one"])]),
                         "usage": {"output_tokens": 750}})
    lessons = summarize(tmp_path_factory, provider, report=report)
    assert len(lessons) == 1 and lessons[0].check is None
    assert [call["max_tokens"] for call in provider.calls] == [4096, 8192]
    assert all(call["max_retries"] == 0 for call in provider.calls)
    assert [a["usage"]["output_tokens"] for a in report["attempts"]] == [4096, 750]
    assert report["requested_output_tokens"] == 12288


@pytest.mark.parametrize("reason,ceiling,calls", [("max_output_tokens", 8192, 2),
    ("max_output_tokens", 4096, 1), ("content_filter", 8192, 1)])
def test_incomplete_never_becomes_empty_success_or_unlimited_retry(tmp_path_factory, reason, ceiling, calls):
    provider = Provider(*[ProviderResponseIncomplete(reason) for _ in range(3)])
    provider.max_output_tokens = ceiling
    report = {}
    with pytest.raises(ProviderResponseIncomplete):
        summarize(tmp_path_factory, provider, report=report)
    assert len(provider.calls) == calls
    assert report["status"] == "failed"


@pytest.mark.parametrize("response", ['{"lessons": []}', '[{"rule":',
    json.dumps([candidate(source_signal_ids=["fabricated"])]),
    json.dumps([candidate(source_signal_ids=[])]),
    json.dumps([candidate(required_tools="exec_command")]),
    json.dumps([candidate()] * 6)])
def test_invalid_verdicts_fail_without_extra_paid_attempt(tmp_path_factory, response):
    provider = Provider(response)
    with pytest.raises(HarvestSummaryInvalid):
        summarize(tmp_path_factory, provider)
    assert len(provider.calls) == 1


def test_route_and_generation_fallbacks_share_the_request_allowance(tmp_path_factory):
    class Routed:
        max_output_tokens = 8192
        stream_session_events = True
        def chat(self, messages, **kwargs):
            for i in range(3):
                kwargs["before_attempt"](i, self)
    report = {}
    with pytest.raises(HarvestBudgetExceeded):
        summarize(tmp_path_factory, Routed(), report=report)
    assert len(report["attempts"]) == 2
    assert report["requested_output_tokens"] == 8192
    class GenerationFallback(Provider):
        def chat(self, messages, **kwargs):
            kwargs["before_generation_fallback"]()
            kwargs["before_generation_fallback"]()
    report = {}
    with pytest.raises(HarvestBudgetExceeded):
        summarize(tmp_path_factory, GenerationFallback(), report=report)
    assert len(report["attempts"]) == 2


def test_sources_are_independent_and_snapshot_precedes_provider_work(tmp_path_factory):
    ws = tmp_path_factory
    (ws / "app.py").write_bytes(b"original")
    (ws / "unrelated.py").write_bytes(b"unrelated")
    class Mutating(Provider):
        def chat(self, *args, **kwargs):
            (ws / "app.py").write_bytes(b"changed during analysis")
            return json.dumps([candidate(source_signal_ids=["one"])])
    lesson = summarize(ws, Mutating(), signals=[{"signal_id": "one", "target_files": ["app.py"]},
        {"signal_id": "two", "target_files": ["unrelated.py"]}])[0]
    assert lesson.evidence["source_signal_ids"] == ["one"]
    assert set(lesson.evidence["workspace_files"]) == {"app.py"}
    from tests.knowledge_fixtures import review_fixture
    review_fixture(lesson)
    assert assess_lesson(lesson, ws)["reason"] == "source_file_changed"


def test_legacy_mixed_batch_cannot_claim_every_source(tmp_path_factory):
    legacy = {"trigger": "app.py", "rule": "A legacy verdict cannot establish a qualification or sources"}
    with pytest.raises(HarvestSummaryInvalid):
        summarize(tmp_path_factory, Provider(json.dumps([legacy])),
                  signals=[{"signal_id": "one"}, {"signal_id": "two"}])


def test_signal_payload_cannot_replace_state_owned_identity(tmp_path_factory):
    from backend.core.history import HistoryManager
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        real = harvest.capture_signal("execution_failure", {"signal_id": "forged", "signal_type": "forged",
            "timestamp": "forged", "harvest_retry_count": 99}, "project", source_event_id="receipt")
        signal = harvest.get_unprocessed_signals("project")[0]
        assert signal["signal_id"] == real and signal["signal_type"] == "execution_failure"
        assert signal["harvest_retry_count"] == 0
        signal = harvest.lease_harvest_signal_ids("project", [real])[0]
        assert signal["signal_id"] == real and signal["signal_type"] == "execution_failure"


def test_first_failure_and_later_success_are_durable_without_existing_lesson(tmp_path_factory):
    from backend.core.loop.loop_bookkeeping import capture_lesson_outcome
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tool_pipeline import ToolResult
    from backend.core.history import HistoryManager
    from backend.core.knowledge.manager import LessonManager
    ws = tmp_path_factory
    process = AgentProcess("first", "worker", RingLevel.RING_3, session=AgentSession(), active_task_id="task")
    process.update_context_snapshot(lambda old: {**old, "project_name": "project"})
    assert LessonManager.load_pending(ws, "project") == []
    failure = ToolResult(id="failure", tool_name="exec_command", data={"stderr": "AssertionError: cache not ready"},
        receipt={"receipt_id": "failure", "command_completed": True, "command_exit_code": 1})
    capture_lesson_outcome(process, failure, str(ws))
    capture_lesson_outcome(process, failure, str(ws))
    with HistoryManager.workspace_scope(str(ws)):
        signals = harvest.get_unprocessed_signals("project")
        assert len(signals) == 1
        # A failure already consumed by harvest still correlates later evidence.
        harvest.lease_harvest_signal_ids("project", [signals[0]["signal_id"]])
        harvest.complete_harvest("project", [signals[0]["signal_id"]], [])
    success = ToolResult(id="success", tool_name="exec_command", data={"stdout": "1 regression passed"},
        receipt={"receipt_id": "success", "command_completed": True, "command_exit_code": 0})
    capture_lesson_outcome(process, success, str(ws))
    with HistoryManager.workspace_scope(str(ws)):
        followup = harvest.get_unprocessed_signals("project")[0]
        assert followup["related_failure_signal_ids"] == [signals[0]["signal_id"]]
        assert followup["signal_type"] == "execution_followup"
    # A different task must not inherit an unrelated failure correlation.
    process.active_task_id = "different-task"
    success.id = "different-success"
    success.receipt["receipt_id"] = "different-success"
    capture_lesson_outcome(process, success, str(ws))
    with HistoryManager.workspace_scope(str(ws)):
        assert len(harvest.get_unprocessed_signals("project")) == 1


def test_observer_failure_does_not_reexecute_or_invalidate_tool_result(tmp_path_factory, monkeypatch):
    from backend.core.loop.loop_bookkeeping import capture_lesson_outcome
    from backend.core.loop.tool_pipeline import ToolResult
    process = SimpleNamespace(active_task_id="task", process_id="proc", session=SimpleNamespace(host_ledger=[]),
        read_context_snapshot=lambda: ({"project_name": "project"}, 0))
    monkeypatch.setattr(harvest, "capture_signal", lambda *a, **kw: (_ for _ in ()).throw(OSError("storage unavailable")))
    result = ToolResult(id="failed", tool_name="read_file", is_error=True, error="missing")
    capture_lesson_outcome(process, result, str(tmp_path_factory))
    assert result.is_error and result.error == "missing"


def test_harvest_has_an_independent_bounded_generation_profile():
    from backend.core.loop.provider_generation import reasoning_effort
    assert reasoning_effort("openai_responses", "knowledge_harvest") == "low"
    assert reasoning_effort("openai_responses", "work") is None
    assert reasoning_effort("anthropic_messages", "knowledge_harvest") is None
    assert reasoning_effort("openai_responses", "knowledge_harvest", unsupported=True) is None


def test_review_can_revise_one_candidate_and_discard_another_before_pending_write(tmp_path_factory, monkeypatch):
    from backend.core.application.services import ApplicationServices
    from backend.core.knowledge.manager import LessonManager
    from backend.core.knowledge.models import Lesson
    from backend.core.history import HistoryManager
    import backend.core.application.services as services
    import backend.core.sync_session as sync
    ws = tmp_path_factory
    monkeypatch.setattr(services, "_project", lambda project: (None, SimpleNamespace()))
    monkeypatch.setattr(sync, "SyncSession", lambda *args: SimpleNamespace(workspace_path=str(ws)))
    report = {"status": "success", "attempts": [{"max_output_tokens": 4096, "status": "complete"}]}
    with HistoryManager.workspace_scope(str(ws)):
        source = harvest.capture_signal("manual_explicit", {"observation": "a reusable incident"}, "project")
        harvest.lease_harvest_signal_ids("project", [source])
        proposal = harvest.stage_harvest_proposal("project", [source], [
            Lesson.from_dict({**candidate(), "project_name": "project", "required_tools": ["edit_file"]}),
            Lesson.from_dict({**candidate(trigger="irrelevant"), "project_name": "project"})], summary_report=report)
        assert LessonManager.load_pending(ws, "project") == []
        result = ApplicationServices().invoke("lesson.harvest.resolve", {"project": "project",
            "proposal_id": proposal["proposal_id"], "action": "accept", "expected_version": proposal["proposal_version"], "candidate_reviews": [
                {"index": 0, "action": "accept", "rule": "When using a mutable default, allocate it per call and test independence"},
                {"index": 1, "action": "discard"}]})
        assert result["count"] == 1
        lessons = LessonManager.load_pending(ws, "project")
        assert len(lessons) == 1 and lessons[0].rule.startswith("When using a mutable default")
        from backend.core.knowledge.review import can_consume
        assert can_consume(lessons[0])
        assert lessons[0].required_tools == [] and lessons[0].check is None
        assert lessons[0].resolution_history[0]["kind"] == "candidate_user_revision"
        assert LessonManager.load_instance(ws, "project") == []
        assert any(entry.detail.get("summary") == report for entry in HistoryManager.load()
                   if entry.operation == "harvest_proposal")


@pytest.mark.parametrize("reviews", [[], [{"index": 0, "action": "accept", "check": {"authority": "registered"}},
    {"index": 1, "action": "accept"}], [{"index": 0, "action": "accept"}, {"index": 0, "action": "discard"}],
    [{"index": 0, "action": "accept"}, {"index": 1, "action": "accept", "rule": "too short"}]])
def test_invalid_review_never_partially_saves_candidates(tmp_path_factory, monkeypatch, reviews):
    from backend.core.application.services import ApplicationServices, OperationError
    from backend.core.knowledge.models import Lesson
    from backend.core.knowledge.manager import LessonManager
    from backend.core.history import HistoryManager
    import backend.core.application.services as services
    import backend.core.sync_session as sync
    ws = tmp_path_factory
    monkeypatch.setattr(services, "_project", lambda project: (None, SimpleNamespace()))
    monkeypatch.setattr(sync, "SyncSession", lambda *args: SimpleNamespace(workspace_path=str(ws)))
    with HistoryManager.workspace_scope(str(ws)):
        source = harvest.capture_signal("manual_explicit", {}, "project")
        harvest.lease_harvest_signal_ids("project", [source])
        proposal = harvest.stage_harvest_proposal("project", [source], [
            Lesson.from_dict({**candidate(), "project_name": "project"}), Lesson.from_dict({**candidate(trigger="other"), "project_name": "project"})])
        with pytest.raises(OperationError) as error:
            ApplicationServices().lesson_harvest_resolve("project", proposal["proposal_id"], "accept", reviews, expected_version=proposal["proposal_version"])
        assert error.value.code == "INVALID_HARVEST_REVIEW"
        assert LessonManager.load_pending(ws, "project") == []
        assert harvest.get_harvest_proposal("project", proposal["proposal_id"]) is not None
