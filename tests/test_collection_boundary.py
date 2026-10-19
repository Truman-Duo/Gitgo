"""User review is a content/version boundary, not a tier name or counter."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend.core.application.services import ApplicationServices, OperationError
from backend.core.history import HistoryManager
from backend.core.knowledge import harvest
from backend.core.knowledge.manager import LessonManager
from backend.core.knowledge.models import Lesson
from backend.core.knowledge.review import can_consume, content_version, review_status
from backend.core.knowledge.recall import recall_grep
from backend.core.knowledge.summary import HarvestSummaryInvalid
from backend.core.storage import StorageRuntime
from backend.core.tools.workspace_tools import read_file, write_file


def qualified(**changes):
    item = {"information_kind": "knowledge", "source_signal_ids": ["source"],
        "trigger": "adapter.py", "rule": "For the legacy adapter, supply the verified token field and test the returned record",
        "qualification": {"knowledge_type": "compatibility", "conditions": "Local adapter release 3",
            "validation": "The token-field regression passes; alternate releases not tested",
            "novelty": "Private adapter behavior absent from its public description",
            "limitations": "Local release 3 only"}}
    item.update(changes)
    return item


class Provider:
    max_output_tokens = 8192
    def __init__(self, data):
        self.data, self.calls = data, 0
    def chat(self, messages, **kwargs):
        self.calls += 1
        assert "existing_knowledge" in messages[0]["content"]
        return json.dumps(self.data)


def prepare(ws, candidates=None):
    source = harvest.capture_signal("execution_failure", {"task_id": "task", "error": "bad adapter field"}, "p",
                                    source_event_id="receipt")
    harvest.lease_harvest_signal_ids("p", [source])
    candidates = candidates if candidates is not None else [Lesson(trigger="adapter.py",
        rule="For the legacy adapter, supply the verified token field and test the returned record", project_name="p")]
    return harvest.stage_harvest_proposal("p", [source], candidates)


@pytest.fixture
def app(tmp_path_factory, monkeypatch):
    import backend.core.application.services as services
    import backend.core.sync_session as sync
    monkeypatch.setattr(services, "_project", lambda project: (None, SimpleNamespace()))
    monkeypatch.setattr(sync, "SyncSession", lambda *args: SimpleNamespace(workspace_path=str(tmp_path_factory)))
    return ApplicationServices()


@pytest.mark.parametrize("kind", ["style", "identity", "task", "runtime_fact", "component_fix",
                                  "existing_source", "history", "skill", "none"])
def test_non_knowledge_is_an_analysis_disposition_not_a_lesson(tmp_path_factory, kind):
    provider = Provider([{"information_kind": kind, "source_signal_ids": ["source"], "reason": "Owned by another component"}])
    report = {}
    assert harvest.harvest_llm_summary([{"signal_id": "source", "detail": "a genuine observation"}],
        provider, str(tmp_path_factory), "p", raise_on_error=True, summary_report=report) == []
    assert report["classifications"][0]["information_kind"] == kind
    assert LessonManager.load_pending(tmp_path_factory, "p") == []
    assert provider.calls == 1


@pytest.mark.parametrize("item", [
    {"trigger": "adapter.py", "rule": "A long legacy verdict without qualification"},
    qualified(qualification={}), qualified(information_kind="imaginary"),
    qualified(source_signal_ids=["invented"]),
])
def test_unclassified_or_unsupported_candidates_fail_explicitly(tmp_path_factory, item):
    with pytest.raises(HarvestSummaryInvalid):
        harvest.harvest_llm_summary([{"signal_id": "source"}], Provider([item]),
            str(tmp_path_factory), "p", raise_on_error=True)


def test_model_cannot_return_approval_or_a_checker(tmp_path_factory):
    item = qualified(review={"status": "accepted", "kind": "user_proposal"},
                     check={"authority": "registered", "pattern": ".*"})
    lesson = harvest.harvest_llm_summary([{"signal_id": "source"}], Provider([item]),
        str(tmp_path_factory), "p", raise_on_error=True)[0]
    assert lesson.qualification["conditions"] == "Local adapter release 3"
    assert lesson.review is None and lesson.check is None and not can_consume(lesson)


def test_unknown_legacy_record_is_preserved_but_not_injected(tmp_path_factory):
    ws = tmp_path_factory
    (ws / "adapter.py").write_text("before", encoding="utf-8")
    runtime = LessonManager._runtime(ws)
    legacy = Lesson(id="legacy", project_name="p", trigger="adapter.py", rule="A legacy rule of unknown approval",
        source="manual", verified_count=99, check={"authority": "registered", "mode": "violation_pattern", "pattern": "bad"},
        dangerous_tools=["write_file"])
    runtime.upsert_lesson_record(legacy.to_dict(), scope="instance", content_hash="legacy")
    assert LessonManager.load_instance(ws, "p")[0].verified_count == 99
    assert recall_grep("adapter", "p", workspace=str(ws))["lessons"] == []
    args = {"_workspace": str(ws), "_lesson_context": {"project_name": "p"}, "path": "adapter.py"}
    assert read_file(args).get("lesson_advisories", []) == []
    from backend.core.knowledge.prewrite import relevant_diagnostics
    assert relevant_diagnostics(ws, ws / "adapter.py", "before", "bad", "write_file",
                                [legacy]) == []
    from backend.core.loop.context_builder import _get_relevant_lessons
    assert _get_relevant_lessons(ws, "p") == []
    assert runtime.get_lesson_record("legacy")[1] == legacy.to_dict()
    with pytest.raises(ValueError, match="LESSON_REVIEW_REQUIRED"):
        LessonManager.verify(ws, "legacy", "p")  # model/maintenance path has no approval
    accepted = LessonManager.verify(ws, "legacy", "p", user_confirmation=True)
    assert accepted and can_consume(accepted)
    assert accepted.check is None and accepted.dangerous_tools == []


def test_acceptance_is_atomic_and_content_bound(tmp_path_factory, app):
    ws = tmp_path_factory
    with HistoryManager.workspace_scope(str(ws)):
        proposal = prepare(ws)
        assert app.lesson_harvest_list("p")["proposals"][0]["proposal_version"] == proposal["proposal_version"]
        assert LessonManager.load_pending(ws, "p") == []
        assert recall_grep("adapter", "p", workspace=str(ws))["lessons"] == []
        result = app.lesson_harvest_resolve("p", proposal["proposal_id"], "accept",
            expected_version=proposal["proposal_version"])
        lesson = LessonManager.load_pending(ws, "p")[0]
        assert result["count"] == 1 and can_consume(lesson)
        assert lesson.review["reference"] == proposal["proposal_id"] + ":" + proposal["proposal_version"]
        assert recall_grep("adapter", "p", workspace=str(ws))["lessons"][0]["id"] == lesson.id
        assert harvest.harvest_status("p")["states"] == {"processed": 1}
        records = HistoryManager._storage().load_harvest_signal_records()
        assert records[0]["resolved_source_ref"]
        original = HistoryManager._storage()._read_json_blob(records[0]["resolved_source_ref"])
        assert original["state"] == "awaiting_confirmation" and original["proposal_lessons"]
        with pytest.raises(OperationError, match="missing|resolved"):
            app.lesson_harvest_resolve("p", proposal["proposal_id"], "accept", expected_version=proposal["proposal_version"])
        lesson.rule = "A changed but unreviewed instruction must not inherit the old approval"
        LessonManager.save_pending(ws, lesson)
        assert review_status(lesson) == "content_changed"
        assert recall_grep("adapter", "p", workspace=str(ws))["lessons"] == []


def test_wrong_proposal_version_and_discard_write_no_knowledge(tmp_path_factory, app):
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        proposal = prepare(tmp_path_factory)
        with pytest.raises(OperationError) as exc:
            app.lesson_harvest_resolve("p", proposal["proposal_id"], "accept", expected_version="stale")
        assert exc.value.code == "HARVEST_PROPOSAL_CHANGED"
        assert LessonManager.load_pending(tmp_path_factory, "p") == []
        result = app.lesson_harvest_resolve("p", proposal["proposal_id"], "discard",
            expected_version=proposal["proposal_version"])
        assert result["status"] == "dismissed" and result["count"] == 0


def test_storage_failure_rolls_back_both_candidates_and_source_resolution(tmp_path_factory, app, monkeypatch):
    ws = tmp_path_factory
    with HistoryManager.workspace_scope(str(ws)):
        proposal = prepare(ws, [Lesson(trigger="a", rule="First actual candidate of sufficient length", project_name="p"),
                                Lesson(trigger="b", rule="Second actual candidate of sufficient length", project_name="p")])
        runtime = HistoryManager._storage()
        original = runtime._prepare_state_json
        calls = []
        def failing(value, *, media_type):
            if media_type.endswith("lesson+json"):
                calls.append(value)
                if len(calls) == 2:
                    raise OSError("authoritative storage unavailable")
            return original(value, media_type=media_type)
        monkeypatch.setattr(runtime, "_prepare_state_json", failing)
        with pytest.raises(OSError):
            app.lesson_harvest_resolve("p", proposal["proposal_id"], "accept", expected_version=proposal["proposal_version"])
        assert LessonManager.load_pending(ws, "p") == []
        assert harvest.get_harvest_proposal("p", proposal["proposal_id"]) is not None


def test_independent_connections_cannot_accept_and_discard_the_same_version(tmp_path_factory):
    ws = tmp_path_factory
    with HistoryManager.workspace_scope(str(ws)):
        proposal = prepare(ws)
        home = HistoryManager._storage().paths.state_home
    first, second = StorageRuntime(ws, state_home=home), StorageRuntime(ws, state_home=home)
    def resolve(runtime, accept):
        try:
            return runtime.resolve_harvest_proposal_records("p", proposal["proposal_id"],
                accepted=accept, candidates=None, expected_version=proposal["proposal_version"])
        except ValueError as exc:
            return str(exc)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: resolve(*pair), [(first, True), (second, False)]))
        assert sum(isinstance(result, dict) for result in results) == 1
        assert "HARVEST_PROPOSAL_NOT_FOUND" in results
        winning = next(result for result in results if isinstance(result, dict))
        assert len(first.load_lesson_records(scope="pending", project_name="p")) == len(winning["lesson_ids"])
    finally:
        first.close()
        second.close()


def test_old_signal_snapshot_cannot_overwrite_review_result(tmp_path_factory, app):
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        proposal = prepare(tmp_path_factory)
        runtime = HistoryManager._storage()
        stale = runtime.load_harvest_signal_records(with_version=True)
        app.lesson_harvest_resolve("p", proposal["proposal_id"], "accept", expected_version=proposal["proposal_version"])
        stale[0]["state"] = "retry"
        with pytest.raises(ValueError, match="HARVEST_SIGNAL_CHANGED"):
            runtime.save_harvest_signal_records(stale)
        assert harvest.harvest_status("p")["states"] == {"processed": 1}


def test_editing_all_candidates_to_discard_leaves_no_accepted_receipt(tmp_path_factory, app):
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        proposal = prepare(tmp_path_factory)
        result = app.lesson_harvest_resolve("p", proposal["proposal_id"], "accept",
            [{"index": 0, "action": "discard"}], expected_version=proposal["proposal_version"])
        assert result["status"] == "dismissed" and result["count"] == 0
        assert LessonManager.load_pending(tmp_path_factory, "p") == []


def test_legacy_discovery_and_repeated_user_corrections_do_not_save_lessons(tmp_path_factory):
    ws = tmp_path_factory
    (ws / "CLAUDE.md").write_text("## Rules\n- Please use a much shorter response style\n", encoding="utf-8")
    with HistoryManager.workspace_scope(str(ws)):
        for i in range(5):
            HistoryManager.add_operation("p", "sync", "success", {"file_count": 200})
        assert harvest.harvest_lessons(ws, "p") == []
        assert LessonManager.load_pending(ws, "p") == []
        observations = harvest.get_unprocessed_signals("p")
        assert any(item["signal_type"] == "document_observation" for item in observations)
        assert all(item.get("lesson_ids") is None for item in observations)
        from backend.core.daemon.policy_helpers import _harvest_from_rejection_chain
        corrections = [SimpleNamespace(detail={"reason": "change scope", "instruction": "remove a deliverable"},
                        timestamp=f"time-{i}", correlation_id=f"decision-{i}") for i in range(3)]
        _harvest_from_rejection_chain("p", corrections, SimpleNamespace(workspace_path=str(ws)))
        assert LessonManager.load_pending(ws, "p") == []
        assert len(harvest.get_unprocessed_signals("p", "user_task_revision")) == 3
        assert harvest.auto_verify_high_confidence(ws, "p") == 0
        assert harvest.auto_discard_invalid(ws, "p") == 0


def test_task_closure_samples_real_incident_but_not_greeting_child_or_cancel(tmp_path_factory):
    from backend.core.daemon import _observe_task_closure
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    ws = tmp_path_factory
    process = AgentProcess("proc", "worker", RingLevel.RING_3, session=AgentSession(),
                           active_task_id="task", task_description="Fix private adapter behavior")
    process.update_context_snapshot(lambda old: {**old, "task_contract": {
        "revision": 2, "goal": "adapter", "acceptance_criteria": ["same behavior"]}})
    event = {"task_id": "task", "outcome": {"status": "completed"}}
    assert not _observe_task_closure(event, process, workspace_path=str(ws), project_name="p")
    with HistoryManager.workspace_scope(str(ws)):
        harvest.capture_signal("execution_failure", {"task_id": "task"}, "p", source_event_id="failed")
    process.parent_id = "parent"
    assert not _observe_task_closure(event, process, workspace_path=str(ws), project_name="p")
    process.parent_id = None
    process.cancel_requested = True
    assert not _observe_task_closure(event, process, workspace_path=str(ws), project_name="p")
    process.cancel_requested = False
    assert _observe_task_closure(event, process, workspace_path=str(ws), project_name="p")
    assert _observe_task_closure(event, process, workspace_path=str(ws), project_name="p")
    with HistoryManager.workspace_scope(str(ws)):
        closure = harvest.get_unprocessed_signals("p", "task_closed")
        assert len(closure) == 1 and closure[0]["task_context"]["revision"] == 2



class ImmediateThread:
    def __init__(self, *, target, **kwargs):
        self.target = target
    def start(self):
        self.target()


@pytest.mark.parametrize("knowledge", [True, False])
def test_background_worker_stages_or_closes_only_the_selected_task(tmp_path_factory, knowledge):
    import queue
    from backend.core.daemon import _start_background_harvest
    ws = tmp_path_factory
    with HistoryManager.workspace_scope(str(ws)):
        first = harvest.capture_signal("execution_failure", {"task_id": "first", "observation": {"error": "bad token"}},
            "p", source_event_id="first-failure")
        closed = harvest.capture_signal("task_closed", {"task_id": "first", "outcome_status": "completed"},
            "p", source_event_id="first-closed")
        other = harvest.capture_signal("execution_failure", {"task_id": "second"}, "p", source_event_id="second")
    class SummaryProvider:
        max_output_tokens = 8192
        def chat(self, messages, **kwargs):
            # Native IDs: unrelated task facts must not become evidence.
            content = messages[0]["content"]
            assert first in content and closed in content and other not in content
            return json.dumps([qualified(source_signal_ids=[first])] if knowledge else [
                {"information_kind": "component_fix", "source_signal_ids": [first],
                 "reason": "The defect belongs to the tool implementation"}])
    events = queue.Queue()
    assert _start_background_harvest({}, event_queue=events, provider=SummaryProvider(),
        workspace_path=str(ws), project_name="p", signal_type="task_closed", task_id="first",
        thread_factory=ImmediateThread)
    result = events.get_nowait()
    with HistoryManager.workspace_scope(str(ws)):
        assert LessonManager.load_pending(ws, "p") == []
        assert recall_grep("adapter", "p", workspace=str(ws))["lessons"] == []
        if knowledge:
            assert result["status"] == "awaiting_confirmation"
            assert harvest.get_harvest_proposal("p", result["proposal"]["proposal_id"])
        else:
            assert result["status"] == "no_knowledge"
            assert result["summary"]["classifications"][0]["information_kind"] == "component_fix"
        assert [item["signal_id"] for item in harvest.get_unprocessed_signals("p")] == [other]


def test_committed_proposal_remains_available_when_secondary_audit_fails(tmp_path_factory, monkeypatch):
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        source = harvest.capture_signal("execution_failure", {"task_id": "t"}, "p")
        harvest.lease_harvest_signal_ids("p", [source])
        monkeypatch.setattr(HistoryManager, "add_operation",
            lambda *a, **k: (_ for _ in ()).throw(OSError("audit unavailable")))
        proposal = harvest.stage_harvest_proposal("p", [source], [Lesson(trigger="a", rule="Real candidate")])
        assert proposal["audit_warning"] == "OSError"
        assert harvest.get_harvest_proposal("p", proposal["proposal_id"])
        assert LessonManager.load_pending(tmp_path_factory, "p") == []


def test_execution_observation_uses_native_diff_and_marks_its_limit(tmp_path_factory):
    from backend.core.loop.loop_bookkeeping import capture_lesson_outcome
    from backend.core.loop.tool_pipeline import ToolResult
    process = SimpleNamespace(active_task_id="t", process_id="proc", session=SimpleNamespace(host_ledger=[]),
        read_context_snapshot=lambda: ({"project_name": "p", "task_contract": {"revision": 3}}, 0))
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        harvest.capture_signal("execution_failure", {"task_id": "t"}, "p", source_event_id="failure")
    diff = "-old adapter field\n+verified field\n" * 100
    result = ToolResult(id="write", tool_name="edit_file", data={"diff": diff},
        receipt={"receipt_id": "write", "effect_state": "committed", "files": ["adapter.py"]})
    capture_lesson_outcome(process, result, str(tmp_path_factory))
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        observed = harvest.get_unprocessed_signals("p", "execution_followup")[0]
    assert observed["observation"]["diff"] == diff[:1500]
    assert observed["observation_truncated_fields"] == ["diff"]
    assert observed["task_context"]["revision"] == 3


def test_confirmation_requires_displayed_version_and_binds_revised_qualification(tmp_path_factory, app):
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        candidate = Lesson.from_dict({**qualified(), "project_name": "p"})
        proposal = prepare(tmp_path_factory, [candidate])
        with pytest.raises(OperationError) as exc:
            app.lesson_harvest_resolve("p", proposal["proposal_id"], "accept")
        assert exc.value.code == "HARVEST_PROPOSAL_VERSION_REQUIRED"
        assert LessonManager.load_pending(tmp_path_factory, "p") == []
        updated = {**candidate.qualification, "conditions": "Only gateway 3.1 in this project",
                   "limitations": "Release 3.2 has not been checked"}
        app.lesson_harvest_resolve("p", proposal["proposal_id"], "accept",
            [{"index": 0, "action": "accept", "qualification": updated}],
            expected_version=proposal["proposal_version"])
        stored = LessonManager.load_pending(tmp_path_factory, "p")[0]
        assert can_consume(stored)
        assert stored.qualification == updated
        assert stored.review["content_version"] == content_version(stored)
        assert stored.resolution_history[0]["previous_qualification"] == candidate.qualification


def test_qualification_revision_cannot_install_an_executable_checker(tmp_path_factory, app):
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        candidate = Lesson.from_dict({**qualified(), "project_name": "p"})
        proposal = prepare(tmp_path_factory, [candidate])
        with pytest.raises(OperationError) as exc:
            app.lesson_harvest_resolve("p", proposal["proposal_id"], "accept",
                [{"index": 0, "action": "accept",
                  "qualification": {**candidate.qualification, "code": "execute_this()"}}],
                expected_version=proposal["proposal_version"])
        assert exc.value.code == "INVALID_HARVEST_REVIEW"
        assert LessonManager.load_pending(tmp_path_factory, "p") == []
        assert harvest.get_harvest_proposal("p", proposal["proposal_id"])


def test_scope_change_invalidates_previous_review():
    from tests.knowledge_fixtures import review_fixture
    lesson = review_fixture(Lesson(project_name="first", rule="Project-specific knowledge"))
    assert can_consume(lesson)
    lesson.project_name = "another"
    assert review_status(lesson) == "content_changed"


def test_legacy_user_review_rejects_stale_content_and_does_not_register_checker(tmp_path_factory, app):
    ws = tmp_path_factory
    runtime = LessonManager._runtime(ws)
    legacy = Lesson(id="legacy-check", project_name="p", trigger="adapter", rule="The exact legacy rule shown to the user",
        check={"authority": "registered", "mode": "violation_pattern", "pattern": "bad"},
        dangerous_tools=["write_file"])
    runtime.upsert_lesson_record(legacy.to_dict(), scope="pending", content_hash="legacy")
    with pytest.raises(OperationError) as exc:
        app.lesson_verify("p", legacy.id)
    assert exc.value.code == "LESSON_VERSION_REQUIRED"
    with pytest.raises(OperationError) as exc:
        app.lesson_verify("p", legacy.id, expected_version="old-display-version")
    assert exc.value.code == "LESSON_CONTENT_CHANGED"
    assert runtime.get_lesson_record(legacy.id)[0] == "pending"
    app.lesson_verify("p", legacy.id, expected_version=content_version(legacy))
    stored = LessonManager.load_instance(ws, "p")[0]
    assert can_consume(stored) and stored.check is None and stored.dangerous_tools == []
