from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.loop.manager import SessionStore
from backend.core.loop.session import AgentSession
from backend.core.storage import StorageRuntime


def _workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    return workspace


def test_legacy_jsonl_import_is_idempotent_and_archived(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    legacy = workspace / ".gitgo" / "sessions"
    legacy.mkdir(parents=True)
    process_id = "legacy-completed"
    synthetic_key = "sk-" + "supersecret123456789"
    checkpoint = {
        "process_id": process_id,
        "session_id": "legacy-session",
        "checkpoint_at": "2026-01-01T00:00:00Z",
        "messages": [{"role": "user", "content": "legacy checkpoint"}],
        "provider_state": {
            "reasoning-1": {
                "reasoning_content": "keep this reasoning",
                "api_key": synthetic_key,
            },
        },
        "context_epoch": 2,
    }
    (legacy / f"{process_id}.checkpoint.json").write_text(
        json.dumps(checkpoint), encoding="utf-8"
    )
    events = [
        {
            "ts": "2026-01-01T00:01:00Z",
            "event": "message_append",
            "data": {"message": {"role": "assistant", "content": "legacy tail"}},
        },
        {
            "ts": "2026-01-01T00:02:00Z",
            "event": "agent_complete",
            "data": {"status": "completed", "steps_used": 2},
        },
    ]
    (legacy / f"{process_id}.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in events), encoding="utf-8"
    )
    state_home = tmp_path_factory / "state"

    store = SessionStore(str(workspace), state_home=state_home)
    state = store.load_session_state(process_id)
    first_counts = store.storage_counts()
    cas_bytes = b"\n".join(
        item.read_bytes()
        for item in store.storage_paths.cas_dir.rglob("*")
        if item.is_file()
    )

    assert [item["content"] for item in state["messages"]] == [
        "legacy checkpoint", "legacy tail",
    ]
    assert state["context_epoch"] == 2
    assert "keep this reasoning" in state["provider_state"]["reasoning-1"]["reasoning_content"]
    assert synthetic_key.encode("utf-8") not in cas_bytes
    assert b"[REDACTED]" in cas_bytes
    assert process_id not in store.list_incomplete()
    assert not legacy.exists()
    archives = list((workspace / ".gitgo").glob("sessions.legacy-imported-*"))
    assert len(archives) == 1
    store.close()

    reopened = SessionStore(str(workspace), state_home=state_home)
    assert reopened.storage_counts() == first_counts
    assert reopened.load_session(process_id)[-1]["content"] == "legacy tail"
    reopened.close()


def test_new_session_writes_never_recreate_jsonl(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    store = SessionStore(str(workspace), state_home=tmp_path_factory / "state")
    store.append_event("process-live", "message_append", {
        "message": {"role": "user", "content": "sqlite only"},
    })
    session = AgentSession()
    session.append_user("checkpoint")
    reference = store.save_checkpoint("process-live", session)

    assert reference.startswith("sqlite:session/")
    assert not (workspace / ".gitgo" / "sessions").exists()
    assert store.load_session("process-live") == [{"role": "user", "content": "checkpoint"}]
    store.close()


def test_process_checkpoint_populates_task_message_and_receipt_tables(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    store = SessionStore(str(workspace), state_home=tmp_path_factory / "state")
    session = AgentSession()
    session.append_user("perform the task")
    session.append_assistant_provider(
        "done",
        continuation_state={"reasoning_content": "plain reasoning retained"},
    )
    process = SimpleNamespace(
        process_id="process-typed",
        session=session,
        status=SimpleNamespace(value="completed"),
        active_task_id="task-typed",
        task_description="perform typed migration test",
        task_kind="action",
        required_test_ids=["sqlite-migration"],
        role="supervisor",
        actor_kind="supervisor",
        capability_profile_id="supervisor.control",
        parent_id=None,
        steps_used=3,
        max_steps=10,
        created_at="2026-01-01T00:00:00Z",
        tool_receipts=[{
            "receipt_id": "receipt-typed",
            "tool_name": "run_test",
            "succeeded": True,
            "effect_state": "committed",
        }],
        result={"status": "completed", "response": "done"},
        coordination_snapshot=lambda: ([], {}, {}),
        mailbox=SimpleNamespace(durable_snapshot=lambda: {
            "closed": False,
            "pending_ids": ["mail-1"],
            "messages": [{"message_id": "mail-1", "content": "continue safely"}],
        }),
        task_budget=SimpleNamespace(snapshot=lambda: {
            "task_id": "task-typed", "used": {"provider_calls": 2},
        }),
        pending_decision={"decision_id": "decision-1", "question": "choose"},
        context_snapshot={},
        context_version=1,
        completion_rejections=[],
        completion_claim=None,
        review_claim=None,
        review_approvals=[],
        successful_actions=1,
        task_constraints=[],
        worktree_path=str(workspace),
        provider_id="provider-test",
        model_id="model-test",
    )

    store.save_process_checkpoint(process)
    counts = store.storage_counts()
    restored = store.load_session_state(process.process_id)
    restored_process = store.load_process_state(process.process_id)

    assert counts["sessions"] == 1
    assert counts["session_processes"] == 1
    assert counts["tasks"] == 1
    assert counts["messages"] == 2
    assert counts["receipts"] == 1
    assert restored["provider_state"]
    assert restored_process["runtime_state"]["pending_decision"]["decision_id"] == "decision-1"
    assert restored_process["runtime_state"]["mailbox"]["pending_ids"] == ["mail-1"]
    assert restored_process["runtime_state"]["task_budget"]["used"]["provider_calls"] == 2
    cas_payload = b"\n".join(
        item.read_bytes()
        for item in store.storage_paths.cas_dir.rglob("*")
        if item.is_file()
    )
    # Message.reasoning_ref and provider_state_refs must point at one shared
    # object; checkpointing must not duplicate the same reasoning body.
    assert cas_payload.count(b"plain reasoning retained") == 1
    store.close()


def test_terminal_checkpoint_is_committed_before_completion_barrier():
    from backend.core.daemon.persist import _save_session_checkpoint

    calls: list[tuple[str, str]] = []

    class Store:
        def save_process_checkpoint(self, process):
            calls.append(("checkpoint", process.process_id))

        def append_event(self, process_id, event_type, data):
            calls.append((event_type, process_id))

    process = SimpleNamespace(
        process_id="process-order",
        session=AgentSession(),
        status=SimpleNamespace(value="completed"),
        steps_used=2,
    )
    _save_session_checkpoint({"session_store": Store()}, process)

    assert calls == [
        ("checkpoint", "process-order"),
        ("agent_complete", "process-order"),
    ]


def test_checkpoint_transaction_rolls_back_all_authoritative_rows(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    runtime = StorageRuntime(workspace, state_home=tmp_path_factory / "state")
    store = SessionStore(str(workspace), storage=runtime, migrate_legacy=False)
    session = AgentSession()
    session.append_user("must not become partially visible")
    original = runtime._register_object
    calls = 0

    def fail_during_registration(connection, descriptor):
        nonlocal calls
        calls += 1
        original(connection, descriptor)
        if calls == 2:
            raise RuntimeError("injected transaction failure")

    runtime._register_object = fail_during_registration
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        store.save_checkpoint("process-rollback", session)

    counts = runtime.session_storage_counts()
    assert counts["sessions"] == 0
    assert counts["session_processes"] == 0
    assert counts["messages"] == 0
    runtime.close()
