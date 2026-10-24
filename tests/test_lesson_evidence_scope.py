"""Native evidence references and task relations, not semantic repair proofs."""
import json
from types import SimpleNamespace

import pytest

from backend.core.history import HistoryManager
from backend.core.knowledge import harvest
from backend.core.knowledge.applicability import assess_lesson, capture_lesson_evidence
from backend.core.knowledge.evidence_context import task_context_snapshot, correlation_hint
from backend.core.knowledge.excerpts import changed_excerpts, text_hash, capture_excerpts, MAX_TEXT
from backend.core.knowledge.manager import LessonManager
from backend.core.knowledge.prewrite import relevant_diagnostics
from backend.core.knowledge.summary import HarvestSummaryInvalid
from backend.core.tools.workspace_tools import edit_file, write_file, read_file
from tests.knowledge_fixtures import fields, review_fixture


BODY = "\n".join([f"# setup {i}" for i in range(8)] + [
    "def submit(gateway, token):",
    '    payload = {"lease_token": token}',
    "    return gateway.send(payload)",
] + [f"# separation {i}" for i in range(12)] + [
    "def render_title(name):", '    return "old title " + name', ""
])
FIXED = BODY.replace('{"lease_token": token}', '{"metadata": {"lease_token": token}}')


def native_source(ws, *, signal_id="native"):
    (ws / "adapter.py").write_bytes(BODY.encode())
    result = edit_file({"_workspace": str(ws), "path": "adapter.py",
        "old_string": '{"lease_token": token}', "new_string": '{"metadata": {"lease_token": token}}'})
    assert "error" not in result
    return {"signal_id": signal_id, "target_files": ["adapter.py"],
            "knowledge_excerpts": result["knowledge_excerpts"]}, result


class Selecting:
    max_output_tokens = 8192
    def __init__(self, transform=None, choose=True):
        self.transform, self.choose, self.index = transform, choose, []
    def chat(self, messages, **kwargs):
        for line in messages[0]["content"].splitlines():
            try: value = json.loads(line)
            except ValueError: continue
            if isinstance(value, dict) and value.get("signal_id"):
                self.index.append(value)
        source = self.index[0]
        item = {**fields([source["signal_id"]]), "trigger": "adapter.py",
                "rule": "For this private gateway build, put the lease token under metadata and verify the unchanged regression"}
        if self.choose:
            item["source_excerpt_ids"] = [source["available_excerpts"][0]["excerpt_id"]]
        if self.transform:
            item = self.transform(item, self.index)
        return json.dumps([item])


def summary(ws, signals, provider=None):
    return harvest.harvest_llm_summary(signals, provider or Selecting(), str(ws), "project",
                                       raise_on_error=True)[0]


def accepted(ws, source):
    lesson = summary(ws, [source])
    review_fixture(lesson)
    return lesson


def test_native_edit_indexes_actual_committed_text_without_extra_write(tmp_path_factory):
    ws = tmp_path_factory
    source, result = native_source(ws)
    assert (ws / "adapter.py").read_bytes() == FIXED.encode()
    assert result["candidate_sha256"] == result["after_sha256"]
    excerpts = source["knowledge_excerpts"]
    assert len(excerpts) == 1
    excerpt = excerpts[0]
    assert excerpt["origin"] == "native_file_diff" and excerpt["unique_at_capture"]
    assert "metadata" in excerpt["text"] and "render_title" not in excerpt["text"]
    assert excerpt["sha256"] == text_hash(excerpt["text"])
    assert excerpt["observed_file_sha256"] == result["after_sha256"]
    assert capture_excerpts(ws, source)[0]["native_excerpt_id"] == excerpt["excerpt_id"]
    bad = edit_file({"_workspace": str(ws), "path": "adapter.py", "old_string": "absent", "new_string": "x"})
    assert "knowledge_excerpts" not in bad and bad["effect_state"] == "not_committed"


def test_selected_fragment_is_bound_to_shown_source_and_does_not_approve(tmp_path_factory):
    source, _ = native_source(tmp_path_factory)
    provider = Selecting()
    lesson = summary(tmp_path_factory, [source], provider)
    evidence = lesson.evidence
    assert evidence["schema_version"] == 2 and evidence["binding_scope"] == "explicit_source_excerpts"
    assert len(evidence["workspace_excerpts"]) == 1
    assert evidence["workspace_excerpts"][0]["signal_id"] == "native"
    assert "knowledge_excerpts" not in json.loads(provider.index[0]["observation_excerpt"])
    assert lesson.review is None and assess_lesson(lesson, tmp_path_factory)["state"] == "needs_recheck"


@pytest.mark.parametrize("mode", ["invented", "wrong_source", "native_id"])
def test_model_cannot_select_invented_or_other_observation_excerpts(tmp_path_factory, mode):
    source, _ = native_source(tmp_path_factory)
    other = {**source, "signal_id": "other"}
    def transform(item, index):
        item["source_excerpt_ids"] = [
            "invented" if mode == "invented" else
            index[1]["available_excerpts"][0]["excerpt_id"] if mode == "wrong_source" else
            source["knowledge_excerpts"][0]["excerpt_id"]]
        return item
    with pytest.raises(HarvestSummaryInvalid, match="selected real sources"):
        summary(tmp_path_factory, [source, other], Selecting(transform))


def test_global_excerpt_limit_is_visible_and_cannot_be_guessed_around(tmp_path_factory):
    source, _ = native_source(tmp_path_factory)
    signals = [{**source, "signal_id": f"source-{i}"} for i in range(13)]
    hidden = capture_excerpts(tmp_path_factory, signals[-1])[0]["excerpt_id"]
    def transform(item, index):
        assert sum(len(value["available_excerpts"]) for value in index) == 12
        assert index[-1]["available_excerpts_partial"] and not index[-1]["available_excerpts"]
        return {**item, "source_signal_ids": ["source-12"], "source_excerpt_ids": [hidden]}
    with pytest.raises(HarvestSummaryInvalid):
        summary(tmp_path_factory, signals, Selecting(transform))


def test_unrelated_changes_and_line_shift_keep_selected_text_current(tmp_path_factory):
    ws = tmp_path_factory
    source, _ = native_source(ws)
    lesson = accepted(ws, source)
    revised = "# added unrelated preface\n" + FIXED.replace("old title", "new title")
    (ws / "adapter.py").write_bytes(revised.encode())
    assert assess_lesson(lesson, ws)["state"] == "current"
    from backend.core.knowledge.excerpts import assess_excerpts
    observed = assess_excerpts(ws, lesson.evidence["workspace_excerpts"])
    span = next(iter(observed["locations"].values()))
    assert span["start_line"] == lesson.evidence["workspace_excerpts"][0]["start_line"] + 1


@pytest.mark.parametrize("change", ["changed", "duplicate", "deleted", "encoding", "oversize"])
def test_changed_ambiguous_or_unavailable_excerpt_is_not_current(tmp_path_factory, change):
    ws = tmp_path_factory
    source, _ = native_source(ws)
    lesson = accepted(ws, source)
    path = ws / "adapter.py"
    if change == "deleted": path.unlink()
    elif change == "changed": path.write_bytes(FIXED.replace("metadata", "other").encode())
    elif change == "duplicate": path.write_bytes((FIXED + lesson.evidence["workspace_excerpts"][0]["text"]).encode())
    elif change == "encoding": path.write_bytes(b"\xff\xfe")
    else: path.write_bytes(b"x" * (MAX_TEXT + 1))
    assert assess_lesson(lesson, ws)["state"] == "needs_recheck"


def test_legacy_unselected_sources_keep_whole_file_freshness(tmp_path_factory):
    source, _ = native_source(tmp_path_factory)
    lesson = summary(tmp_path_factory, [source], Selecting(choose=False))
    review_fixture(lesson)
    assert lesson.evidence["schema_version"] == 1
    (tmp_path_factory / "adapter.py").write_bytes(FIXED.replace("old title", "new title").encode())
    assert assess_lesson(lesson, tmp_path_factory)["reason"] == "source_file_changed"


def test_absence_of_explicit_file_is_bound_without_inventing_a_path_from_prose(tmp_path_factory):
    ws = tmp_path_factory
    source = {"signal_id": "absent", "target_files": ["removed.py"], "trigger": "a natural language discovery"}
    evidence = capture_lesson_evidence(ws, [source])
    assert evidence["workspace_files"] == {"removed.py": ""}
    lesson = summary(ws, [source], Selecting(choose=False))
    review_fixture(lesson)
    assert assess_lesson(lesson, ws)["state"] == "current"
    (ws / "removed.py").write_bytes(b"created later")
    assert assess_lesson(lesson, ws)["reason"] == "source_file_changed"


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_crlf_and_bom_are_exact_evidence_not_universal_newline_normalization(tmp_path_factory, newline):
    ws = tmp_path_factory
    before = "\ufeff" + BODY.replace("\n", newline)
    after = before.replace('{"lease_token": token}', '{"metadata": {"lease_token": token}}')
    (ws / "adapter.py").write_bytes(after.encode())
    source = {"signal_id": "crlf", "target_files": ["adapter.py"],
              "knowledge_excerpts": changed_excerpts("adapter.py", before, after, text_hash(after))}
    lesson = accepted(ws, source)
    assert newline in lesson.evidence["workspace_excerpts"][0]["text"]
    from backend.core.knowledge.excerpts import excerpt_location
    span = excerpt_location(after, lesson.evidence["workspace_excerpts"][0])
    assert span["start_line"] == lesson.evidence["workspace_excerpts"][0]["start_line"]
    assert span["end_line"] == lesson.evidence["workspace_excerpts"][0]["end_line"]
    assert assess_lesson(lesson, ws)["state"] == "current"
    (ws / "adapter.py").write_bytes(after.replace(newline, "\n").encode())
    assert assess_lesson(lesson, ws)["state"] == "needs_recheck"


def test_wide_changes_fallback_to_file_evidence_and_malformed_scope_is_not_current(tmp_path_factory):
    from backend.core.knowledge.excerpts import edit_touches_excerpts
    assert changed_excerpts("wide.py", "", "x" * 2001, "digest") == []
    assert changed_excerpts("large.py", "", "x" * (MAX_TEXT + 1), "digest") == []
    assert changed_excerpts("lines.py", "", "x\n" * 12001, "digest") == []
    assert edit_touches_excerpts("x\n" * 12001, "y\n" * 12001, [])
    source, _ = native_source(tmp_path_factory)
    lesson = summary(tmp_path_factory, [source], Selecting(
        lambda item, index: {**item, "source_excerpt_ids": []}))
    lesson.evidence["workspace_excerpts"] = ["malformed"]
    review_fixture(lesson)
    assert assess_lesson(lesson, tmp_path_factory)["reason"] == "invalid_source_excerpts"


def test_unrelated_edit_and_read_window_do_not_trigger_automatic_fragment_advice(tmp_path_factory):
    ws = tmp_path_factory
    source, _ = native_source(ws)
    lesson = accepted(ws, source)
    LessonManager.save_pending(ws, lesson)
    path = ws / "adapter.py"
    unrelated = FIXED.replace("old title", "new title")
    assert relevant_diagnostics(ws, path, FIXED, unrelated, "edit_file", [lesson]) == []
    touched = relevant_diagnostics(ws, path, FIXED, FIXED.replace("metadata", "wrong"), "edit_file", [lesson])
    assert len(touched) == 1 and touched[0]["level"] == "warning"
    args = {"_workspace": str(ws), "_lesson_context": {"project_name": "project"}, "path": "adapter.py"}
    assert read_file({**args, "offset": 23, "limit": 3})["lesson_advisories"] == []
    assert read_file({**args, "offset": 8, "limit": 3})["lesson_advisories"][0]["lesson_id"] == lesson.id
    assert read_file({**args, "offset": 999, "limit": 3})["lesson_advisories"] == []


def test_explicit_registered_checker_is_not_narrowed_by_auto_fragment_matching(tmp_path_factory):
    from backend.core.knowledge.models import Lesson
    source, _ = native_source(tmp_path_factory)
    evidence = summary(tmp_path_factory, [source]).evidence
    lesson = Lesson(id="manual", source="manual", trigger="adapter.py", project_name="project",
                    rule="Do not introduce a retired entry point anywhere in this file", evidence=evidence,
                    dangerous_tools=["edit_file"], check={"authority": "registered",
                    "mode": "violation_pattern", "pattern": "retired_entry"})
    LessonManager.save(tmp_path_factory, lesson)
    diagnostics = relevant_diagnostics(tmp_path_factory, tmp_path_factory / "adapter.py", FIXED,
        FIXED.replace("old title", "retired_entry"), "edit_file", [lesson])
    assert diagnostics[0]["code"] == "KNOWN_LESSON_VIOLATION"


def test_excerpt_observer_failure_preserves_one_committed_write(tmp_path_factory, monkeypatch):
    import backend.core.knowledge.excerpts as excerpts
    import backend.core.tools.workspace_tools as tools
    actual_write, writes = tools._atomic_write, []
    def counted(path, body):
        writes.append(body); actual_write(path, body)
    def broken(*args): raise OSError("observer unavailable")
    monkeypatch.setattr(tools, "_atomic_write", counted)
    monkeypatch.setattr(excerpts, "changed_excerpts", broken)
    result = write_file({"_workspace": str(tmp_path_factory), "path": "new.py", "content": "actual"})
    assert "error" not in result and writes == [b"actual"]
    assert result["after_sha256"] == result["candidate_sha256"]
    assert result["knowledge_capture_warning"]["code"] == "SOURCE_EXCERPTS_UNAVAILABLE"


def test_contract_digest_reuses_semantic_fields_not_revision_or_lease():
    old = {"task_contract": {"revision": 1, "goal": "Fix private compatibility",
                            "acceptance_criteria": ["original regression passes"], "lease_until": 1}}
    new = {"task_contract": {**old["task_contract"], "revision": 2, "lease_until": 999}}
    first, second = task_context_snapshot(old), task_context_snapshot(new)
    hint = correlation_hint({"task_context": first, "receipt": {"command_digest": "original"}}, second,
                            {"command_digest": "original"})
    assert hint["same_contract"] is True and hint["same_revision"] is False and hint["same_command"] is True
    assert hint["causal_repair"] == "not_established"
    assert correlation_hint({}, {}, {})["same_contract"] is None


def test_changed_acceptance_is_recorded_but_not_claimed_as_repair_of_old_failure(tmp_path_factory):
    from backend.core.loop.loop_bookkeeping import capture_lesson_outcome
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tool_pipeline import ToolResult
    ws = tmp_path_factory
    process = AgentProcess("worker", "worker", RingLevel.RING_3, session=AgentSession(), active_task_id="task")
    process.update_context_snapshot(lambda old: {**old, "project_name": "project",
        "task_contract": {"revision": 1, "goal": "Deliver original result", "acceptance_criteria": ["original regression"]}})
    failure = ToolResult(id="failed", tool_name="exec_command", data={"stderr": "original regression failed"},
        receipt={"receipt_id": "failed", "command_completed": True, "command_exit_code": 1, "command_digest": "test"})
    capture_lesson_outcome(process, failure, str(ws))
    process.update_context_snapshot(lambda old: {**old,
        "task_contract": {"revision": 2, "goal": "Deliver reduced result", "acceptance_criteria": ["reduced regression"]}})
    success = ToolResult(id="passed", tool_name="exec_command", data={"stdout": "reduced regression passed"},
        receipt={"receipt_id": "passed", "command_completed": True, "command_exit_code": 0, "command_digest": "test"})
    capture_lesson_outcome(process, success, str(ws))
    with HistoryManager.workspace_scope(str(ws)):
        followup = harvest.get_unprocessed_signals("project", "execution_followup")[0]
        assert followup["related_failure_signal_ids"] == []
        relation = followup["failure_relations"][0]
        assert relation["same_contract"] is False and relation["same_command"] is True
        assert relation["relation"] == "different_contract" and relation["causal_repair"] == "not_established"


def test_same_contract_different_failures_remain_candidates_not_a_single_issue(tmp_path_factory):
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        snapshot = task_context_snapshot({"task_contract": {"goal": "Fix two failures"}})
        ids = [harvest.capture_signal("execution_failure", {"task_id": "task", "task_context": snapshot,
            "error": error, "receipt": {"command_digest": command}}, "project", source_event_id=command)
            for error, command in [("gateway field wrong", "test-gateway"), ("render title wrong", "test-render")]]
        relations = harvest.task_failure_relations("project", "task", snapshot, {"command_digest": "test-gateway"})
        assert {row["signal_id"] for row in relations} == set(ids)
        assert all(row["same_contract"] and row["causal_repair"] == "not_established" for row in relations)
        assert sorted(row["same_command"] for row in relations) == [False, True]

def test_review_and_btw_receive_selected_native_snapshot_without_approval(tmp_path_factory):
    ws = tmp_path_factory
    source, _ = native_source(ws)
    with HistoryManager.workspace_scope(str(ws)):
        sid = harvest.capture_signal("execution_followup", {key: value for key, value in source.items() if key != "signal_id"},
                                      "project", source_event_id="actual-write")
        batch = harvest.lease_harvest_signal_ids("project", [sid])
        lesson = summary(ws, batch)
        proposal = harvest.stage_harvest_proposal("project", [sid], [lesson])
        context = harvest.get_harvest_proposal_context("project", proposal["proposal_id"], proposal["proposal_version"])
        binding = context["evidence_binding"]
        assert binding["scope"] == "selected_source_excerpts"
        snapshot = binding["selected_excerpts"][0]
        assert snapshot["signal_id"] == sid and snapshot["path"] == "adapter.py"
        assert snapshot["text"] == lesson.evidence["workspace_excerpts"][0]["text"]
        (ws / "adapter.py").write_bytes(b"changed after summary")
        again = harvest.get_harvest_proposal_context("project", proposal["proposal_id"], proposal["proposal_version"])
        assert again["evidence_binding"] == binding and "Captured" in binding["coverage"]
        assert LessonManager.load_pending(ws, "project") == []

def test_excerpt_coordinates_stay_on_the_actual_line_when_a_prefix_is_inserted(tmp_path_factory):
    from backend.core.knowledge.excerpts import excerpt_location
    source, _ = native_source(tmp_path_factory)
    item = capture_excerpts(tmp_path_factory, source)[0]
    changed = FIXED.replace(item["text"], "new-prefix " + item["text"])
    span = excerpt_location(changed, item)
    assert span["start_line"] == item["start_line"]
    assert span["end_line"] == item["end_line"]
