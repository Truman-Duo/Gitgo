from types import SimpleNamespace

from backend.core.application import ApplicationServices
from backend.core.application import services
from backend.core.history import HistoryManager
from backend.core.loop.trace import TraceJournal


def test_native_governance_feed_reads_existing_trace_without_copying_history(tmp_path_factory, monkeypatch):
    workspace = tmp_path_factory / "ws"
    workspace.mkdir()
    monkeypatch.setattr(services, "_project", lambda _name: (None, SimpleNamespace(workspace_path=workspace)))
    trace = TraceJournal(workspace, "task")
    trace.append({"event": "governance_snapshot", "process_id": "a", "signal_count": 2})
    trace.append({"event": "reasoning_delta", "delta": "not public governance content"})
    service = ApplicationServices()
    feed = service.governance_feed("p")
    assert len(feed) == 1
    assert feed[0]["operation"] == "governance_snapshot"
    assert feed[0]["detail"]["signal_count"] == 2
    assert service.governance_feed("p", limit=0) == []
    with HistoryManager.workspace_scope(str(workspace)):
        assert HistoryManager.load() == []


def test_governance_feed_pages_complete_native_event_scope(tmp_path_factory, monkeypatch):
    workspace = tmp_path_factory / "ws-paged"
    workspace.mkdir()
    monkeypatch.setattr(services, "_project", lambda _name: (None, SimpleNamespace(workspace_path=workspace)))
    trace = TraceJournal(workspace, "task-paged")
    for event in (
        "governance_snapshot", "context_compaction_failed", "worktree_cleanup_failed",
    ):
        trace.append({"event": event, "process_id": "a"})
    service = ApplicationServices()
    first = service.governance_feed("p", limit=2, cursor="")
    second = service.governance_feed("p", limit=2, cursor=first["page"]["next_cursor"])

    assert first["page"]["scope"] == "governance_history_and_native_trace"
    assert first["page"]["has_more"] is True
    assert second["page"]["has_more"] is False
    assert {item["operation"] for item in [*first["items"], *second["items"]]} == {
        "governance_snapshot", "context_compaction_failed", "worktree_cleanup_failed",
    }


def test_history_scope_is_nested_and_restores_previous_thread_context(tmp_path_factory):
    previous = getattr(HistoryManager._local, "workspace_path", None)
    with HistoryManager.workspace_scope(str(tmp_path_factory / "a")):
        assert HistoryManager._local.workspace_path.endswith("a")
        with HistoryManager.workspace_scope(str(tmp_path_factory / "b")):
            assert HistoryManager._local.workspace_path.endswith("b")
        assert HistoryManager._local.workspace_path.endswith("a")
    assert getattr(HistoryManager._local, "workspace_path", None) == previous
