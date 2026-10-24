import json
from types import SimpleNamespace
import pytest
from backend.core.history import HistoryManager
from backend.core.knowledge import harvest
from backend.core.knowledge.models import Lesson
from backend.core.knowledge.manager import LessonManager
from backend.core.daemon import dispatch
from backend.core.loop import executor

def setup(ws):
    with HistoryManager.workspace_scope(str(ws)):
        sources = [harvest.capture_signal("execution_failure", {"task_id": "task", "observation": "fact-" + str(i) + "x"*1500},
            "p", source_event_id=str(i)) for i in range(15)]
        harvest.lease_harvest_signal_ids("p", sources)
        lesson = Lesson(trigger="adapter", rule="Gateway 3 needs its token under the verified metadata field",
            qualification={"knowledge_type": "compatibility", "conditions": "x"*1500, "validation": "Original test passed",
                "novelty": "New private gateway behavior", "limitations": "Only release 3"},
            evidence={"source_signal_ids": sources}, project_name="p")
        return harvest.stage_harvest_proposal("p", sources, [lesson])

def test_discussion_context_has_bounded_real_sources_and_no_write(tmp_path_factory):
    ws = tmp_path_factory
    proposal = setup(ws)
    with HistoryManager.workspace_scope(str(ws)):
        context = harvest.get_harvest_proposal_context("p", proposal["proposal_id"], proposal["proposal_version"])
        assert len(context["sources"]) == 12 and context["sources_partial"]
        assert all(len(x["excerpt"]) == 1000 and x["truncated"] for x in context["sources"])
        assert context["candidate"]["qualification_truncated_fields"] == ["conditions"]
        assert len(json.dumps(context)) < 20000
        assert LessonManager.load_pending(ws, "p") == []
        assert harvest.get_harvest_proposal("p", proposal["proposal_id"])
        with pytest.raises(ValueError, match="NOT_FOUND"):
            harvest.get_harvest_proposal_context("another", proposal["proposal_id"], proposal["proposal_version"])
        with pytest.raises(ValueError, match="CHANGED"):
            harvest.get_harvest_proposal_context("p", proposal["proposal_id"], "stale")
        with pytest.raises(ValueError, match="INVALID"):
            harvest.get_harvest_proposal_context("p", proposal["proposal_id"], proposal["proposal_version"], True)
        harvest.resolve_harvest_proposal("p", proposal["proposal_id"], accepted=False)
        with pytest.raises(ValueError, match="NOT_FOUND"):
            harvest.get_harvest_proposal_context("p", proposal["proposal_id"], proposal["proposal_version"])

def test_btw_reads_proposal_at_dispatch_and_retains_read_only_profile(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory; proposal = setup(ws)
    calls, events = [], []
    class InlineThread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): self.target()
    def step(process, instruction, **kwargs):
        calls.append(instruction)
        names = process.tool_registry.list_all()
        assert not set(names) & {"write_file", "edit_file", "exec_command", "lesson_verify", "lesson_promote"}
        assert proposal["proposal_id"] in instruction and "unconfirmed" in instruction
        assert "User revised wording" in instruction and "not submitted or accepted" in instruction
        return {"status": "completed", "response": "Suggestion only, not accepted"}
    monkeypatch.setattr(dispatch.threading, "Thread", InlineThread)
    monkeypatch.setattr(executor, "agent_step", step)
    ref = {"proposal_id": proposal["proposal_id"], "proposal_version": proposal["proposal_version"], "candidate_index": 0,
        "draft": {"trigger": "User revised gateway condition", "rule": "User revised wording about the exact metadata token field",
                  "qualification": proposal["candidates"][0]["qualification"]}}
    ctx = {"apm": SimpleNamespace(storage=None), "llm": object(), "dispatcher": object()}
    cmd = {"action": "btw", "question": "Check conditions", "lesson_proposal": ref, "sidecar_id": "side"}
    dispatch._cmd_task(cmd, SimpleNamespace(workspace_path=ws), SimpleNamespace(name="p"), ctx, events.append)
    assert len(calls) == 1
    assert any(e.get("event") == "btw_complete" for e in events)
    assert LessonManager.load_pending(ws, "p") == []
    with HistoryManager.workspace_scope(str(ws)):
        assert harvest.get_harvest_proposal("p", proposal["proposal_id"])
        harvest.resolve_harvest_proposal("p", proposal["proposal_id"], accepted=False)
    events.clear()
    dispatch._cmd_task(cmd, SimpleNamespace(workspace_path=ws), SimpleNamespace(name="p"), ctx, events.append)
    assert len(calls) == 1
    assert any("HARVEST_PROPOSAL_NOT_FOUND" in str(e.get("error")) for e in events)


def test_native_btw_transport_passes_only_reference_and_draft_without_implicit_parent():
    import io
    from backend.core.native_host import NativeHost
    from tests.test_native_host import FakeDaemon
    class BtwDaemon(FakeDaemon):
        def send_command(self, command, timeout=30):
            self.last_command = command
            return {"answer": "Read-only discussion", "sidecar_id": command["sidecar_id"]}
    host = NativeHost(stdout=io.StringIO(), daemon_factory=BtwDaemon)
    try:
        host._conversation_processes["p"] = "unrelated-root"
        draft = {"trigger": "gateway", "rule": "A complete and bounded proposed rule about gateway 3",
                 "qualification": {"knowledge_type": "compatibility", "conditions": "Gateway 3",
                    "validation": "One test", "novelty": "Private behavior", "limitations": "Only 3"}}
        ref = {"proposal_id": "proposal", "proposal_version": "version", "candidate_index": 0,
               "draft": draft, "fake_approval": True}
        host._runtime_btw("p", {"question": "Discuss", "lesson_proposal": ref}, request_id="discussion")
        sent = host._daemons["p"].last_command
        assert sent["process_ids"] == [] and sent["process_id"] == ""
        assert sent["lesson_proposal"] == {key: ref[key] for key in ("proposal_id", "proposal_version", "candidate_index", "draft")}
        assert not host._active_btw
    finally:
        host.close()


def test_host_announces_discard_without_claiming_saved_knowledge(monkeypatch):
    import io
    from backend.core.native_host import NativeHost
    stream = io.StringIO(); host = NativeHost(stdout=stream)
    monkeypatch.setattr(host.services, "invoke", lambda *args: {
        "proposal_id": "proposal", "status": "dismissed", "count": 0})
    try:
        host._dispatch({"operation": "lesson.harvest.resolve", "arguments": {"project": "p"}})
        events = [json.loads(line)["payload"] for line in stream.getvalue().splitlines()]
        assert [event["event"] for event in events] == ["lesson_harvest_resolved"]
        assert events[0]["status"] == "dismissed"
    finally:
        host.close()
