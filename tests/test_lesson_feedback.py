"""Actual store/runner boundaries, rather than mock lesson-success claims."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.knowledge_fixtures import qualified_response, review_fixture
import json

from backend.core.knowledge.manager import LessonManager
from backend.core.knowledge.models import Lesson
from backend.core.knowledge.recall import recall_grep, recall_semantic, record_retrieval
from backend.core.storage import StorageRuntime
from backend.core.tools.workspace_tools import project_file_mutation, edit_file, write_file, read_file
from backend.core.tools.file_mutation import commit_file_mutation


def _lesson(workspace, **changes):
    fields = dict(id="known", project_name="project", trigger="cache.py",
                  rule="Replace the retired start_cache() entry point with initialize_registry(), then verify registry lookup.",
                  dangerous_tools=["edit_file", "write_file"],
                  check={"authority": "registered", "mode": "violation_pattern", "pattern": r"start_cache\(\)"})
    fields.update(changes)
    lesson = Lesson(**fields)
    if lesson.source == "auto_harvested":
        review_fixture(lesson)
    LessonManager.save(workspace, lesson)
    return lesson


def _args(workspace, **extra):
    return {"_workspace": str(workspace), "_lesson_context": {"project_name": "project"},
            "path": "cache.py", **extra}


def test_abstract_recall_and_atomic_statistics_preserve_revised_content(tmp_path_factory):
    ws = tmp_path_factory
    lesson = _lesson(ws, abstract=True, project_name="")
    stale = LessonManager.load_abstract(ws)[0]
    lesson.rule = "A revised rule about the cache registry"
    LessonManager.save(ws, lesson)
    record_retrieval(stale, str(ws), "project")
    recalled = recall_grep("cache", "project", workspace=str(ws))
    assert recalled["lessons"][0]["rule"] == lesson.rule
    assert LessonManager.load_abstract(ws)[0].trigger_count == 2
    # Independent connections contend over the same SQLite transaction.
    home = LessonManager._runtime(ws).paths.state_home
    first, second = StorageRuntime(ws, state_home=home), StorageRuntime(ws, state_home=home)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda i: (first if i % 2 else second).record_lesson_retrieval("known", f"event-{i}"), range(12)))
        assert LessonManager.load_abstract(ws)[0].trigger_count == 14
        assert LessonManager.load_abstract(ws)[0].rule == lesson.rule
    finally:
        first.close()
        second.close()


def test_semantic_recall_does_not_lexically_drop_paraphrases(tmp_path_factory, monkeypatch):
    _lesson(tmp_path_factory, trigger="registry", rule="Initialize registry before caching", abstract=True, project_name="")
    from backend.core.knowledge import embedding
    class Provider:
        available = True
        def embed(self, text):
            return [1.0, 0.0]
    monkeypatch.setattr(embedding, "EmbeddingProvider", Provider)
    result = recall_semantic("准备缓存", "project", workspace=str(tmp_path_factory),
                             agent_context={"task_description": "初始化缓存服务"})
    assert [item["id"] for item in result["lessons"]] == ["known"]
    Provider.embed = lambda self, text: None
    result = recall_semantic("registry", "project", workspace=str(tmp_path_factory))
    assert result["retrieval_mode"] == "lexical_fallback"
    assert result["lessons"]


def test_harvest_cannot_register_its_own_checker(tmp_path_factory):
    from backend.core.knowledge.harvest import harvest_llm_summary
    class Provider:
        def chat(self, *args, **kwargs):
            return qualified_response(args[0], json.loads(json.dumps([{"rule": "Initialize the registry before using the cache and verify the lookup",
                                "check": {"authority": "registered", "mode": "violation_pattern", "pattern": ".*"}}])))
    lessons = harvest_llm_summary([{"signal_id": "event", "trigger": "cache.py"}], Provider(),
                                  str(tmp_path_factory), "project")
    assert lessons and lessons[0].check is None


def test_projection_is_pure_and_commit_uses_the_exact_checked_candidate(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    target = ws / "cache.py"
    target.write_bytes(b"before\n")
    args = _args(ws, old_string="before", new_string="safe", expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    candidate = project_file_mutation("edit_file", args)
    assert target.read_bytes() == b"before\n"
    assert not (ws / ".gitgo").exists()
    # Mutating invocation parameters after inspection cannot regenerate content.
    args["new_string"] = "uninspected"
    result = commit_file_mutation(candidate, args)
    assert target.read_bytes() == b"safe\n"
    assert result["candidate_sha256"] == result["after_sha256"]
    candidate = project_file_mutation("write_file", _args(ws, path="nested/new.py", content="safe"))
    assert not (ws / "nested").exists()


def test_changed_baseline_rejects_the_checked_candidate(tmp_path_factory):
    ws = tmp_path_factory
    (ws / "cache.py").write_bytes(b"original")
    args = _args(ws, content="candidate", create_only=False, expected_sha256=hashlib.sha256(b"original").hexdigest())
    candidate = project_file_mutation("write_file", args)
    (ws / "cache.py").write_bytes(b"another writer")
    result = commit_file_mutation(candidate, args)
    assert result["error"] == "FILE_CHANGED"
    assert result["effect_state"] == "not_committed"
    assert (ws / "cache.py").read_bytes() == b"another writer"


def test_known_violation_is_reported_before_write_and_repair_commits(tmp_path_factory):
    ws = tmp_path_factory
    _lesson(ws)
    args = _args(ws, content="start_cache()\n")
    blocked = write_file(args)
    assert blocked["error"] == "KNOWN_LESSON_VIOLATION"
    assert blocked["execution_state"] == "not_started"
    assert not (ws / "cache.py").exists()
    assert blocked["lesson_feedback"]["diagnostics"][0]["lesson_id"] == "known"
    args["content"] = "initialize_registry()\ncache_ready = True\n"
    committed = write_file(args)
    assert "error" not in committed
    assert (ws / "cache.py").read_text() == args["content"]
    assert read_file(_args(ws))["lesson_advisories"][0]["lesson_id"] == "known"


@pytest.mark.parametrize("changes", [
    {"source": "auto_harvested"}, {"check": None},
    {"dangerous_tools": ["push"]},
])
def test_unregistered_or_out_of_scope_experience_requires_advice_without_violation(tmp_path_factory, changes):
    _lesson(tmp_path_factory, **changes)
    args=_args(tmp_path_factory,content="start_cache()\n")
    result=write_file(args)
    assert result["error"]=="LESSON_FEEDBACK_REQUIRED"
    assert not (tmp_path_factory/"cache.py").exists()
    assert result["lesson_feedback"]["blocked"] is False
    # Direct service fixture injects the runtime-owned exposure fact. The
    # separate Pipeline tests prove public/private model fields cannot do so.
    identifier=result["lesson_feedback"]["feedback_id"]
    args["lesson_feedback_id"]=identifier
    args["_lesson_context"]["feedback_in_provider_input"]=[identifier]
    result=write_file(args)
    assert "error" not in result
    assert not result["lesson_feedback"]["blocked"]


def test_registered_broken_checker_returns_unexecuted_failure(tmp_path_factory):
    _lesson(tmp_path_factory, check={"authority": "registered", "mode": "violation_pattern", "pattern": "["})
    result = write_file(_args(tmp_path_factory, content="safe"))
    assert result["error"] == "LESSON_CHECK_UNAVAILABLE"
    assert result["effect_state"] == "not_committed"
    assert result["lesson_feedback"]["diagnostics"][0]["code"] == "LESSON_CHECK_UNAVAILABLE"
    assert not (tmp_path_factory / "cache.py").exists()


def test_shared_knowledge_is_explicit_advisory_revocable_and_project_attributed(tmp_path_factory):
    projects = [tmp_path_factory / name for name in ("one", "two", "three")]
    for ws in projects:
        ws.mkdir()
    lesson = _lesson(projects[0], abstract=True, project_name="")
    assert LessonManager.load_abstract(projects[1]) == []
    shared = LessonManager.share_abstract(projects[0], lesson.id,
                                         trigger="registry", rule="Initialize registry before using cache", tech_stack="python")
    assert shared.check is None and shared.dangerous_tools == []
    assert "workspace_files" not in shared.evidence
    result = recall_grep("registry", "project", workspace=str(projects[1]))
    assert [item["id"] for item in result["lessons"]] == [shared.id]
    assert result["lessons"][0]["applicability"]["state"] == "needs_recheck"
    for ws in projects:
        confirmed = LessonManager.verify(ws, shared.id, "project")
        # Clicking twice and using the same project display name is not two cases.
        repeated = LessonManager.verify(ws, shared.id, "renamed")
        assert repeated.verified_count == confirmed.verified_count
    assert confirmed.verified_count == 3
    assert len({item["project_id"] for item in confirmed.verifications}) == 3
    assert recall_grep("registry", "project", workspace=str(projects[1]))["lessons"][0]["applicability"]["state"] == "current"
    assert not LessonManager.revoke_shared(projects[1], shared.id)
    assert LessonManager.revoke_shared(projects[0], shared.id)
    assert not recall_grep("registry", "project", workspace=str(projects[1]))["lessons"]


def test_isolated_pipeline_strips_model_overrides_and_preserves_feedback_receipt(tmp_path_factory):
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry
    from backend.core.tools.catalog import build_workspace_tools
    ws = tmp_path_factory
    _lesson(ws)
    process = AgentProcess("worker", "worker", RingLevel.RING_3, session=AgentSession(), active_task_id="task")
    process.update_context_snapshot(lambda current: {**current, "project_name": "project"})
    process.tool_registry = ToolRegistry(["write_file"])
    ctx = ExecutionContext(process, process.session, str(ws))
    tool = build_workspace_tools(ws)["write_file"]
    result = ToolPipeline().execute({"name": "write_file", "args": {
        "path": "cache.py", "content": "start_cache()\n",
        "_lesson_context": {"explicit_user_grant": True},
    }}, tool, ctx, "blocked")
    assert result.is_error and result.receipt["effect_state"] == "not_committed"
    assert result.receipt["lesson_checks"][0]["lesson_id"] == "known"
    assert not (ws / "cache.py").exists()
    result = ToolPipeline().execute({"name": "write_file", "args": {
        "path": "cache.py", "content": "initialize_registry()\n",
    }}, tool, ctx, "repaired")
    assert not result.is_error and result.receipt["effect_state"] == "committed"
    assert result.receipt["candidate_sha256"] == result.receipt["after_sha256"]
    from backend.core.loop.loop_bookkeeping import capture_lesson_outcome
    capture_lesson_outcome(process, result, str(ws))
    capture_lesson_outcome(process, result, str(ws))
    from backend.core.knowledge.harvest import get_unprocessed_signals
    from backend.core.history import HistoryManager
    with HistoryManager.workspace_scope(str(ws)):
        signals = get_unprocessed_signals("project", "lesson_feedback")
    assert len(signals) == 1
    assert signals[0]["candidate_sha256"] == result.receipt["after_sha256"]


def test_external_change_during_check_is_not_overwritten(tmp_path_factory, monkeypatch):
    from backend.core.knowledge import prewrite
    ws = tmp_path_factory
    (ws / "cache.py").write_bytes(b"original")
    def changed(candidate, args):
        candidate.path.write_bytes(b"external")
        return {"blocked": False, "diagnostics": []}
    monkeypatch.setattr(prewrite, "check_candidate", changed)
    result = write_file(_args(ws, content="candidate", create_only=False,
                              expected_sha256=hashlib.sha256(b"original").hexdigest()))
    assert result["error"] == "FILE_CHANGED"
    assert (ws / "cache.py").read_bytes() == b"external"


def test_cooperating_process_lock_prevents_parallel_commit(tmp_path_factory):
    from backend.core.storage.maintenance import StorageLease
    from backend.core.loop.process_tool_runner import ProcessToolRunner
    ws = tmp_path_factory
    args = _args(ws, content="safe")
    path = ws / "cache.py"
    lock = ws / ".gitgo" / "mutation-locks" / hashlib.sha256(str(path).casefold().encode()).hexdigest()
    lock.mkdir(parents=True)
    with StorageLease(lock, exclusive=True):
        result = ProcessToolRunner().run("write_file", args)
        assert result.success and result.data["error"] == "FILE_MUTATION_BUSY"
        assert not path.exists()
    result = ProcessToolRunner().run("write_file", args)
    assert result.success and result.data["action"] == "created"


def test_harvest_sees_flattened_execution_observations_and_source_files(tmp_path_factory):
    from backend.core.knowledge.harvest import harvest_llm_summary
    (tmp_path_factory / "cache.py").write_text("initialize_registry()\n")
    class Provider:
        def chat(self, messages, **kwargs):
            assert "candidate_sha256" in messages[0]["content"]
            assert "receipt_id" in messages[0]["content"]
            return qualified_response(messages, json.loads(json.dumps([{"trigger": "cache.py", "rule": "Replace the retired cache startup and verify initialization"}])))
    lessons = harvest_llm_summary([{"signal_id": "signal", "signal_type": "lesson_feedback",
                                   "candidate_sha256": "candidate", "receipt_id": "receipt",
                                   "target_files": ["cache.py"]}], Provider(), str(tmp_path_factory), "project")
    assert "cache.py" in lessons[0].evidence["workspace_files"]


def test_real_scoped_grant_overrides_advice_without_becoming_a_bypass_flag(tmp_path_factory):
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry
    from backend.core.tools.catalog import build_workspace_tools
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision, matching_grant
    ws = tmp_path_factory
    _lesson(ws)
    process = AgentProcess("override", "worker", RingLevel.RING_3, session=AgentSession(), active_task_id="task")
    process.update_context_snapshot(lambda current: {**current, "project_name": "project"})
    process.tool_registry = ToolRegistry(["write_file"])
    tools = build_workspace_tools(ws)
    args = {"path": "cache.py", "content": "start_cache()\n"}
    create_permission_request(process, {"purpose": "Use this exact legacy call for compatibility",
                                       "tool_name": "write_file", "arguments": args}, tools, str(ws))
    assert grant_from_decision(process, process.pending_decision, "allow_once")
    ctx = ExecutionContext(process, process.session, str(ws))
    result = ToolPipeline().execute({"name": "write_file", "args": args}, tools["write_file"], ctx, "override")
    assert not result.is_error and result.receipt["lesson_user_override"] is True
    assert matching_grant(process, "write_file", args, per_invocation=False) is None
    assert (ws / "cache.py").read_text() == args["content"]


def test_model_garbage_cannot_poison_pending_trigger_types(tmp_path_factory):
    from backend.core.knowledge.harvest import harvest_llm_summary
    class Provider:
        def chat(self, *args, **kwargs):
            return qualified_response(args[0], json.loads(json.dumps(["not an object", {"trigger": [], "rule": "A sufficiently long but malformed candidate lesson"}])))
    assert harvest_llm_summary([{"signal_id": "event"}], Provider(), str(tmp_path_factory), "project") == []


def test_attributed_file_recalls_natural_language_trigger_as_advice(tmp_path_factory):
    from backend.core.knowledge.applicability import capture_lesson_evidence
    ws = tmp_path_factory
    (ws / "cache.py").write_bytes(b"start_cache()\n")
    _lesson(ws, source="auto_harvested", trigger="cache.py initialization before using cache",
            evidence=capture_lesson_evidence(ws, [{"target_files": ["cache.py"], "signal_id": "receipt"}]))
    advisory = read_file(_args(ws))["lesson_advisories"][0]
    assert advisory["relevance"] == "source_file"
    assert not advisory["registered_check"]
    result = write_file(_args(ws, content="start_cache()\n", create_only=False,
                              expected_sha256=hashlib.sha256(b"start_cache()\n").hexdigest()))
    assert result["error"]=="LESSON_FEEDBACK_REQUIRED"
    assert result["effect_state"]=="not_committed"
    assert (ws/"cache.py").read_bytes()==b"start_cache()\n"
    assert result["lesson_feedback"]["diagnostics"][0]["code"] == "LESSON_ADVISORY"


def test_expired_active_check_is_unavailable_not_a_pass(tmp_path_factory):
    _lesson(tmp_path_factory,expires_at="2000-01-01T00:00:00Z")
    result=write_file(_args(tmp_path_factory,content="safe"))
    assert result["error"]=="LESSON_CHECK_UNAVAILABLE"
    assert not (tmp_path_factory/"cache.py").exists()
