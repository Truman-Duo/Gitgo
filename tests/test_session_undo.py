from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.storage import StorageRuntime


def _runtime(root: Path):
    workspace = root / "workspace"
    workspace.mkdir()
    storage = StorageRuntime(workspace, state_home=root / "state")
    store = SessionStore(str(workspace), storage=storage)
    manager = AgentProcessManager(max_concurrency=2)
    process = manager.fork(
        parent_id=None,
        role="supervisor",
        tool_registry=ToolRegistry([]),
        max_steps=4,
        ring_level=RingLevel.RING_0,
        workspace_path=str(workspace),
        task_id="turn-1",
        task_kind="answer",
        actor_kind="supervisor",
    )
    process._manager = manager
    return storage, store, manager, process


def test_undo_restores_provider_valid_snapshot_without_deleting_old_branch(
    tmp_path_factory: Path,
):
    storage, store, _manager, process = _runtime(tmp_path_factory)
    first = store.checkpoint_before_user_turn(
        process, turn_id="turn-1", turn_preview="first question",
    )
    process.session.append_user("first question")
    process.session.append_assistant("first answer")
    process.status = ProcessStatus.COMPLETED
    store.save_process_checkpoint(process)

    preview = store.preview_undo(process.process_id)
    assert preview["checkpoint_id"] == first["checkpoint_id"]
    assert preview["messages_removed"] == 2
    assert preview["workspace_reverted"] is False

    result, restored, _target = store.undo(
        process.process_id, first["checkpoint_id"],
    )
    assert result["scope"] == "session_only"
    assert restored.messages == []
    assert store.load_session_state(process.process_id)["messages"] == []
    with pytest.raises(ValueError, match="SESSION_UNDO_UNAVAILABLE"):
        store.preview_undo(process.process_id)

    # Immutable CAS history remains retained even though the active transcript
    # moved back.  GC sees the lineage snapshot as a live reference.
    with storage._state_lock:
        lineage = storage._state.execute(
            "SELECT state, snapshot_ref FROM session_lineage WHERE checkpoint_id=?",
            (first["checkpoint_id"],),
        ).fetchone()
    assert lineage["state"] == "rewound"
    assert storage.read_blob(str(lineage["snapshot_ref"]))
    storage.close()


def test_new_turn_after_undo_creates_a_new_branch(tmp_path_factory: Path):
    storage, store, _manager, process = _runtime(tmp_path_factory)
    first = store.checkpoint_before_user_turn(
        process, turn_id="turn-1", turn_preview="one",
    )
    process.session.append_user("one")
    process.session.append_assistant("answer one")
    store.save_process_checkpoint(process)
    second = store.checkpoint_before_user_turn(
        process, turn_id="turn-2", turn_preview="two",
    )
    process.session.append_user("two")
    process.session.append_assistant("answer two")
    process.status = ProcessStatus.COMPLETED
    store.save_process_checkpoint(process)

    _result, restored, _target = store.undo(
        process.process_id, second["checkpoint_id"],
    )
    process.session = restored
    branch = store.checkpoint_before_user_turn(
        process, turn_id="turn-3", turn_preview="alternate two",
    )

    assert branch["parent_checkpoint_id"] == first["checkpoint_id"]
    assert branch["checkpoint_id"] != second["checkpoint_id"]
    storage.close()
