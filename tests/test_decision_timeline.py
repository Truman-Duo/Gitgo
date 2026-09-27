from backend.core.loop.decision_timeline import decision_timeline
from backend.core.storage import StorageRuntime


def request(identity, sequence):
    return {"event": "user_decision_requested", "decision_id": identity, "process_id": "a",
            "task_id": "task", "question": f"Question {identity}", "why_user_must_decide": "Product choice",
            "options": [], "allow_free_form": True, "message_sequence": sequence,
            "created_at": f"2026-09-03T10:00:0{sequence}"}


def test_multiple_decisions_remain_distinct_and_only_matching_answer_resolves():
    ledger = [request("one", 1), request("two", 2),
              {"event": "user_decision_received", "decision_id": "one", "answer": "first choice"}]
    cards = decision_timeline(ledger + [ledger[0]])
    assert [card["message_id"] for card in cards] == ["decision:one", "decision:two"]
    assert [card["status"] for card in cards] == ["answered", "awaiting_user"]
    assert cards[0]["decision_answer"] == "first choice"


def test_decisions_survive_checkpoint_reopen_without_duplicate_control_reply(tmp_path_factory):
    workspace = tmp_path_factory / "ws"
    workspace.mkdir()
    snapshot = {
        "process_id": "a", "session_id": "s", "task_id": "task", "parent_id": None,
        "actor_kind": "supervisor", "task_kind": "supervisor", "status": "awaiting_user",
        "messages": [{"role": "user", "content": "Make a page"}],
        "session_metadata": {"host_ledger": [request("one", 1),
            {"event": "user_decision_received", "decision_id": "one", "answer": "simple"}, request("two", 2)]},
        "result": {"status": "awaiting_user", "response": "internal formatted control reply"},
    }
    with StorageRuntime(workspace) as storage:
        storage.save_agent_checkpoint(snapshot)
    with StorageRuntime(workspace) as reopened:
        messages = reopened.read_latest_conversations()["main_conversation"]
        assert [message["kind"] for message in messages] == ["conversation", "decision", "decision"]
        assert messages[1]["decision_answer"] == "simple"
        assert messages[2]["decision"]["decision_id"] == "two"
        assert "internal formatted control reply" not in str(messages)


def test_child_question_is_projected_as_one_compact_a_summary(tmp_path_factory):
    workspace = tmp_path_factory / "child-question"
    workspace.mkdir()
    child_request = {
        **request("child-choice", 2),
        "process_id": "b", "task_id": "child-task",
        "source_process_id": "b", "source_display_name": "B2",
        "source_actor_kind": "worker", "owner_process_id": "a",
    }
    with StorageRuntime(workspace) as storage:
        storage.save_agent_checkpoint({
            "process_id": "a", "session_id": "root-session", "task_id": "root-task",
            "parent_id": None, "actor_kind": "supervisor", "status": "running",
            "messages": [{"role": "user", "content": "Build the page", "timestamp": "2026-09-03T10:00:00"}],
            "session_metadata": {"host_ledger": []},
        })
        storage.save_agent_checkpoint({
            "process_id": "b", "session_id": "child-session", "task_id": "child-task",
            "parent_id": "a", "actor_kind": "worker", "status": "awaiting_user",
            "messages": [],
            "session_metadata": {"display_name": "B2", "host_ledger": [
                child_request,
                {"event": "user_decision_received", "decision_id": "child-choice", "answer": "Minimal"},
            ]},
            "runtime_state": {"pending_decision": child_request},
        })
        projection = storage.read_latest_conversations()
        summaries = [row for row in projection["main_conversation"] if row["kind"] == "agent_question"]
        assert len(summaries) == 1
        assert summaries[0]["content"] == "B2 asked: Question child-choice · answered: Minimal"
        assert projection["agent_conversations"]["b"][0]["kind"] == "decision"


def test_old_checkpoint_context_meter_is_reconstructed_from_session(tmp_path_factory):
    workspace = tmp_path_factory / "old-context"
    workspace.mkdir()
    with StorageRuntime(workspace) as storage:
        storage.save_agent_checkpoint({
            "process_id": "a", "session_id": "s", "task_id": "t", "parent_id": None,
            "actor_kind": "supervisor", "status": "completed",
            "messages": [{"role": "user", "content": "x" * 400}],
            "provider_state": {"p": {"reasoning": "y" * 40}},
            "session_metadata": {
                "model_context_limit": 8192, "context_epoch": 3,
                "cache_telemetry": [{
                    "input_tokens": 100, "cache_read_tokens": 90,
                    "eligible_for_reuse": True, "miss_reason": "",
                }],
            },
            "runtime_state": {},
        })
        root = storage.read_latest_root_process()
        assert root["estimated_tokens"] >= 110
        assert root["context"]["estimated_tokens"] == root["estimated_tokens"]
        assert root["context"]["limit"] == 8192
        assert root["context"]["epoch"] == 3
        assert root["context"]["breakdown"]["estimated_tokens"] == root["estimated_tokens"]
        assert sum(
            item["estimated_tokens"]
            for item in root["context"]["breakdown"]["sections"]
        ) == root["estimated_tokens"]
        assert root["cache_summary"]["eligible_hit_ratio"] == 0.9
