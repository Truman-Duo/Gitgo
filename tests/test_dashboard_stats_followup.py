import io
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import pytest
from backend.core.storage import StorageRuntime
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.native_host import NativeHost


def save_usage(store, process, task, date, parent=None):
    store.save_agent_checkpoint({"process_id": process, "session_id": "s-"+process, "task_id": task,
        "parent_id": parent, "actor_kind": "worker" if parent else "supervisor", "status": "completed",
        "created_at": date, "messages": [], "result": {"status": "completed", "response": "done", "duration_ms": 100,
        "metadata": {"task_tree_budget": {"used": {"provider_calls": 2, "reported_input_tokens": 100,
        "reported_output_tokens": 20, "reported_cache_read_tokens": 80}}}}})


def test_daily_usage_is_all_roots_not_page_or_child_and_respects_offset(tmp_path_factory):
    ws = tmp_path_factory / "daily"
    ws.mkdir()
    with StorageRuntime(ws) as store:
        save_usage(store, "a", "task-a", "2026-10-16T20:00:00Z")
        save_usage(store, "b", "task-b", "2026-10-17T05:00:00Z")
        save_usage(store, "child", "task-c", "2026-10-17T06:00:00Z", "a")
        local = store.read_task_usage(limit=1, day_offset_minutes=480)
        assert len(local["tasks"]) == 1 and local["page"]["has_more"]
        assert local["daily"] == [{"date": "2026-10-17", "task_count": 2, "provider_calls": 4,
            "input_tokens": 200, "output_tokens": 40, "cache_read_tokens": 160, "tool_calls": 0}]
        assert local["cost_usd"] is None
        assert len(store.read_task_usage(day_offset_minutes=0)["daily"]) == 2
        older = store.read_task_usage(limit=1, cursor=local["page"]["next_cursor"], day_offset_minutes=480)
        assert older["daily"] == local["daily"]


def test_global_usage_includes_archive_without_double_counting_shared_identity(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory / "global"
    ws.mkdir()
    with StorageRuntime(ws) as store:
        save_usage(store, "root", "task", "2026-10-17T00:00:00Z")
        host = NativeHost(stdout=io.StringIO())
        host.services.project_list = lambda: [{"name": "active", "workspace": str(ws)}]
        host.services.project_list_archived = lambda: [{"name": "old-name", "workspace": str(ws)},
            {"name": "unavailable", "workspace": str(ws / "missing")}]
        monkeypatch.setattr("backend.core.storage.get_storage", lambda path: store)
        try:
            result = host._runtime_usage(day_offset_minutes=480)
            assert result["summary"]["task_count"] == 1
            assert len(result["tasks"]) == 1
            assert result["projects"][1] == {"project": "old-name", "alias_of": "active"}
            assert result["complete"] is False
            assert "error" in result["projects"][2]
            assert host._runtime_usage("old-name")["summary"]["task_count"] == 1
        finally:
            host.close()


def test_dashboard_status_never_uses_recovery_loader_and_decodes_each_transcript_once(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory / "control"
    ws.mkdir()
    with StorageRuntime(ws) as store:
        manager = AgentProcessManager()
        sessions = SessionStore(str(ws), storage=store)
        root = manager.fork(None, "supervisor", ToolRegistry([]), 50, RingLevel.RING_0,
            actor_kind="supervisor", task_id="root")
        root.session.host_ledger.append({"event": "provider_route_activated", "model": "recorded"})
        root.session.append_user("main")
        sessions.save_process_checkpoint(root)
        children = []
        for i in range(3):
            child = manager.fork(root.process_id, "worker", ToolRegistry([]), 50, RingLevel.RING_3, task_id=f"b-{i}")
            child.session.append_user(f"child {i}")
            sessions.save_process_checkpoint(child)
            children.append(child)
        monkeypatch.setattr(store, "load_agent_process_state", lambda _: (_ for _ in ()).throw(AssertionError("Recovery hydration in display")))
        root_projection = store.read_latest_root_process()
        assert root_projection["model_id"] == "recorded"
        assert len(store.read_project_b_processes()) == 3
        original = store._public_session_messages
        calls = Counter()
        def public(session):
            calls[session] += 1
            return original(session)
        monkeypatch.setattr(store, "_public_session_messages", public)
        children_view = store.read_project_b_conversations()
        view = store.read_latest_conversations(agent_conversations=children_view)
        assert view["main_conversation"][0]["content"] == "main"
        assert all(count == 1 for count in calls.values())
        assert len(calls) == 4
