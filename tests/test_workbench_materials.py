"""Declared fixtures for intake, source preservation and confirmation isolation."""
from concurrent.futures import ThreadPoolExecutor
import json
from queue import Queue
from types import SimpleNamespace

import pytest

from backend.core.storage import StorageRuntime, bind_storage, unbind_storage
from backend.core.workbench.sheets import SheetStore
from backend.core.workbench.sources import SourceWorkbench, SourceUnavailable
from backend.core.workbench.materials import submit_material
from backend.core.loop.context_store import ContextObjectStore
from backend.core.history import HistoryManager
from backend.core.knowledge import harvest
from backend.core.knowledge.applicability import capture_lesson_evidence, assess_lesson
from backend.core.knowledge.review import mark_reviewed
from backend.core.knowledge.manager import LessonManager
from tests.knowledge_fixtures import fields


@pytest.fixture
def work(tmp_path_factory):
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as runtime:
        bind_storage(root, runtime)
        sheets = SheetStore(runtime, session_id="s", process_id="p", owner_scope="main_session")
        context = {"project_name": "p"}
        source = SourceWorkbench(sheets, ContextObjectStore(root), lambda: context)
        sheet = sheets.create("Investigation", [{"id": "note", "text": "A scoped fixture finding, still unconfirmed."},
                                               {"id": "private", "text": "unselected private note"}])
        try:
            yield root, runtime, sheets, source, sheet
        finally:
            unbind_storage(root, runtime)


def submit(work, sheet=None, ids=None, **kwargs):
    _, _, sheets, source, default = work
    selected = sheet or default
    return submit_material(sheets, source, sheet_id=selected["sheet_id"], revision=selected["revision"],
        cell_ids=ids or ["note"], purpose=kwargs.pop("purpose", "A reusable fixture compatibility finding"),
        project="p", task_id="task", context={"project_name": "p"}, **kwargs)


def test_material_is_selected_pinned_and_never_an_active_lesson(work):
    root, runtime, sheets, _, sheet = work
    a = submit(work)
    assert a["state"] == "pending"
    record = runtime.load_harvest_signal_records()[0]
    assert record["detail"]["material"]["revision"] == sheet["revision"]
    assert record["detail"]["material"]["cell_ids"] == ["note"]
    assert "unselected private note" not in json.dumps(record)
    assert LessonManager.load_pending(root, "p") == []
    assert sheets.open(sheet["sheet_id"])["revision"] == sheet["revision"]


def test_repeated_material_cannot_reset_review_or_a_lease(work):
    _, runtime, _, _, _ = work
    a = submit(work)
    with HistoryManager.workspace_scope(str(work[0])):
        harvest.lease_harvest_signal_ids("p", [a["signal_id"]])
    before = runtime.load_harvest_signal_records()[0]
    b = submit(work, purpose="Different prose must not reset the identical material")
    assert b["signal_id"] == a["signal_id"] and b["state"] == "leased"
    assert runtime.load_harvest_signal_records()[0] == before


def test_two_writers_create_only_never_overwrite_a_terminal_record(work):
    root, runtime, _, _, _ = work
    a = submit(work)
    record = runtime.load_harvest_signal_records()[0]
    record.update(state="processed", lesson_ids=["declared-test-fixture"])
    runtime.save_harvest_signal_records([record])
    stale = {**record, "state": "pending", "lesson_ids": []}
    with StorageRuntime(root, state_home=root / "state") as other:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda r: r.save_harvest_signal_records([stale], create_only=True), [runtime, other]))
    assert results == [0, 0]
    assert runtime.load_harvest_signal_records()[0]["state"] == "processed"
    assert submit(work)["state"] == "processed"


def test_stale_and_oversized_selections_do_not_silently_change_scope(work):
    _, runtime, sheets, _, sheet = work
    sheets.revise(sheet["sheet_id"], sheet["revision"], [{"id": "note", "text": "new"}])
    old = submit(work, sheet)
    assert old["revision"] == sheet["revision"]  # Pinned historical selection is intentional.
    with pytest.raises(ValueError, match="missing"):
        submit(work, ids=["invented"])
    big = sheets.create("Large", [{"id": "big", "text": "x" * 20000}])
    with pytest.raises(ValueError, match="16000"):
        submit(work, big, ["big"])
    assert len(runtime.load_harvest_signal_records()) == 1


def test_notes_only_evidence_does_not_become_current_by_empty_file_fallback(work):
    root, _, _, _, _ = work
    a = submit(work)
    with HistoryManager.workspace_scope(str(root)):
        signal = harvest.get_unprocessed_signals("p")[0]
        evidence = capture_lesson_evidence(root, [signal])
    assert evidence["workbench_materials"][0]["material_digest"] == a["material_digest"]
    from backend.core.knowledge.models import Lesson
    lesson = Lesson(trigger="fixture", rule="Only the controlled fixture uses this scoped adapter behavior.",
                    source="auto_harvested", qualification=fields([])["qualification"], evidence=evidence)
    mark_reviewed(lesson, kind="explicit_verification", reference="declared-test-fixture")
    assert assess_lesson(lesson, root)["reason"] == "workbench_material_has_no_independent_source"


def test_material_source_binding_survives_summary_and_later_source_change(work):
    root, _, _, source, sheet = work
    ref = source.contexts.put("fixture/data", {"release": "7.3", "passed": True})
    captured = source.import_context(sheet["sheet_id"], sheet["revision"], cell_id="observed", source_ref=ref["latest"])
    a = submit(work, captured, ["note", "observed"])
    class Provider:
        max_output_tokens = 8192
        def chat(self, messages, **kwargs):
            assert "working_material_excerpt" in messages[0]["content"]
            return {"content": json.dumps([{**fields([a["signal_id"]]), "source_excerpt_ids": [],
                "trigger": "fixture 7.3", "rule": "Only fixture release 7.3 uses the observed adapter placement."}])}
    with HistoryManager.workspace_scope(str(root)):
        batch = harvest.lease_harvest_signal_ids("p", [a["signal_id"]])
        lessons = harvest.harvest_llm_summary(batch, Provider(), str(root), "p", raise_on_error=True)
        assert len(lessons) == 1 and lessons[0].evidence["workbench_materials"]
        proposal = harvest.stage_harvest_proposal("p", [a["signal_id"]], lessons)
        context = harvest.get_harvest_proposal_context("p", proposal["proposal_id"], proposal["proposal_version"])
        assert context["authority"].startswith("unconfirmed")
        assert LessonManager.load_pending(root, "p") == []
        mark_reviewed(lessons[0], kind="explicit_verification", reference="declared-test-fixture")
        assert assess_lesson(lessons[0], root)["state"] == "current"
        source.contexts.put("fixture/data", {"release": "7.4", "passed": True})
        assert assess_lesson(lessons[0], root)["state"] == "needs_recheck"
        assert submit(work, captured, ["observed", "note"])["state"] == "awaiting_confirmation"


def test_task_end_reuses_existing_harvest_trigger_for_material(work):
    from backend.core.daemon import _observe_task_closure, _start_background_harvest
    root, _, _, _, _ = work
    submitted = submit(work)
    process = SimpleNamespace(parent_id=None, cancel_requested=False, active_task_id="task", process_id="p",
        task_description="Review one compatibility finding", steps_used=1, read_context_snapshot=lambda: ({}, 1))
    assert _observe_task_closure({"outcome": {"status": "completed"}, "task_id": "task"}, process,
        workspace_path=str(root), project_name="p")
    class Inline:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
    class Provider:
        max_output_tokens = 8192
        def chat(self, messages, **kwargs):
            return {"content": json.dumps([{**fields([submitted["signal_id"]]), "source_excerpt_ids": [],
                "trigger": "fixture", "rule": "Only the fixture release supports this bounded adapter placement."}])}
    events = Queue()
    assert _start_background_harvest({}, event_queue=events, provider=Provider(), workspace_path=str(root),
        project_name="p", signal_type="task_closed", task_id="task", thread_factory=Inline)
    result = next(e for e in list(events.queue) if e.get("event") == "knowledge_harvest_result")
    assert result["status"] == "awaiting_confirmation"
    assert LessonManager.load_pending(root, "p") == []


def test_postcommit_audit_failure_is_warning_without_duplicate_material(work, monkeypatch):
    _, runtime, _, _, _ = work
    def fail(*args, **kwargs): raise OSError("declared-test-fault")
    monkeypatch.setattr(runtime, "append_history_record", fail)
    a = submit(work)
    assert a["warnings"][0]["catalog_id"] == "GITGO-W7602"
    b = submit(work)
    assert a["signal_id"] == b["signal_id"] and len(runtime.load_harvest_signal_records()) == 1


def test_provided_data_can_calculate_without_claiming_independent_observation(work):
    _, _, _, source, sheet = work
    a = source.add_data(sheet["sheet_id"], sheet["revision"], cell_id="provided", json_text='[0.1,0.2]', description="User list transcribed by model")
    b = source.aggregate(a["sheet_id"], a["revision"], cell_id="sum", source_cell_id="provided", selector="", reducer="sum")
    assert b["result"]["value"] == "0.3" and b["applicability"]["reason"] == "model_assembled_data"


def test_owned_registered_tool_result_is_complete_and_checked_across_pages(work):
    _, runtime, sheets, source, sheet = work
    data = '{"rows":[' + ','.join(str(i) for i in range(6000)) + ']}'
    locator = runtime.put_tool_result(data, process_id="p", task_id="task", tool_name="fixture")["locator"]
    a = source.import_result(sheet["sheet_id"], sheet["revision"], cell_id="native", locator=locator)
    b = source.aggregate(a["sheet_id"], a["revision"], cell_id="count", source_cell_id="native", selector="/rows", reducer="count")
    assert b["result"]["value"] == 6000
    assert b["applicability"]["state"] == "current" and "recorded" in b["applicability"]["scope_note"]
    assert sheets.open(b["sheet_id"])["cell_count"] == 4


def test_other_process_or_unregistered_file_cannot_be_imported_as_a_tool_result(work):
    _, runtime, sheets, source, sheet = work
    locator = runtime.put_tool_result('[1]', process_id="other", task_id="task", tool_name="fixture")["locator"]
    for bad in (locator, "C:/fake.json", "tool-result:sha256:" + "0" * 64):
        with pytest.raises(SourceUnavailable):
            source.import_result(sheet["sheet_id"], sheet["revision"], cell_id="bad", locator=bad)
    assert sheets.open(sheet["sheet_id"])["revision"] == sheet["revision"]


def test_tool_submission_requires_bound_main_task_identity(work):
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    root, runtime, _, _, sheet = work
    process = AgentProcess(process_id="p", role="worker", ring_level=RingLevel.RING_3, runtime_storage=runtime,
                           session=AgentSession(session_id="s"), active_task_id="task", parent_id="parent")
    process.replace_context_snapshot({"project_name": "p"})
    tool = build_internal_tools(process, {}, str(root), None, None, None)["workbench"]
    args = {"operation": "submit_material", "sheet_id": sheet["sheet_id"], "revision": sheet["revision"],
            "cell_ids": ["note"], "purpose": "Fixture finding"}
    assert tool.execute(args)["error"] == "WORKBENCH_UNAVAILABLE"
    process.parent_id = None
    assert tool.execute(args)["state"] == "pending"
    assert runtime.load_harvest_signal_records()[0]["project_name"] == "p"


def test_simultaneous_new_capture_has_one_creator(work):
    root, runtime, _, _, _ = work
    import threading
    gate = threading.Barrier(2)
    record = {"signal_id": "hs_parallel_fixture", "project_name": "p", "signal_type": "fixture", "state": "pending"}
    with StorageRuntime(root, state_home=root / "state") as other:
        def create(r):
            gate.wait()
            return r.save_harvest_signal_records([record], create_only=True)
        with ThreadPoolExecutor(2) as pool:
            replies = [pool.submit(create, r) for r in (runtime, other)]
            assert sorted(f.result() for f in replies) == [0, 1]
    assert len(runtime.load_harvest_signal_records()) == 1


def test_windows_same_object_publication_race_only_accepts_matching_bytes(work, monkeypatch):
    import os
    from backend.core.storage.repositories import objects
    _, runtime, _, _, _ = work
    original = objects.os.replace
    content = b"same immutable object"
    def another_writer_published(src, dst):
        Path(dst).write_bytes(content)
        raise PermissionError("declared Windows same-object race")
    from pathlib import Path
    monkeypatch.setattr(objects.os, "replace", another_writer_published)
    ref = runtime.put_blob(content)
    assert runtime.read_blob(ref) == content
    def conflicting_writer(src, dst):
        Path(dst).write_bytes(b"conflicting bytes")
        raise PermissionError("declared unexpected permission failure")
    monkeypatch.setattr(objects.os, "replace", conflicting_writer)
    with pytest.raises(PermissionError, match="unexpected"):
        runtime.put_blob(b"must not certify different bytes")
    monkeypatch.setattr(objects.os, "replace", original)
