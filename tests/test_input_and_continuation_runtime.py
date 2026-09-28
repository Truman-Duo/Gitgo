from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from backend.core.loop.budget import TaskTreeBudget, TaskBudgetExceeded
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.recovery import restore_incomplete_processes
from backend.core.config import Config
from .test_task_contract_recovery import _supervisor


def test_continue_original_worker_keeps_session_not_outcome(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    with patch.object(manager, "start"):
        first = tools["delegate_task"]({"task_description": "Build page", "target_files": ["index.html"],
                                        "acceptance_criteria": ["HTML"], "display_name": "页面负责人"})
        assert first["delegated"], first
        old = manager.get(first["process_id"])
        old.status = ProcessStatus.TIMED_OUT
        old.result = {"status": "timed_out"}
        old.session.append_user("preserve design decisions")
        second = tools["delegate_task"]({"task_description": "Revise appearance", "target_files": ["index.html"],
            "acceptance_criteria": ["New appearance"], "continue_process_id": old.process_id})
    assert second["delegated"], second
    new = manager.get(second["process_id"])
    assert new is not old and new.session is old.session
    assert new.session.display_name == "页面负责人"
    assert new.session.messages[-1]["content"] == "preserve design decisions"
    assert new.tool_receipts == [] and new.completion_claim is None
    assert old.result["status"] == "timed_out"
    assert root.delegated_contracts[old.process_id]["superseded_by"] == new.process_id


def test_parallel_session_admission_allows_exactly_one_execution(tmp_path_factory):
    root, _ = _supervisor(tmp_path_factory)
    manager = root._manager
    first = manager.fork(root.process_id, "executor", ToolRegistry([]), 5, RingLevel.RING_3)
    first.status = ProcessStatus.COMPLETED
    def attempt(_):
        try:
            return manager.fork(root.process_id, "executor", ToolRegistry([]), 5, RingLevel.RING_3,
                                session=first.session)
        except ValueError as exc:
            assert "SESSION_EXECUTION_ACTIVE" in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(attempt, range(2)))
    assert sum(result is not None for result in attempts) == 1


def test_cold_terminal_root_hydrates_inherited_children_without_replay(tmp_path_factory):
    root, _ = _supervisor(tmp_path_factory)
    manager = root._manager
    child = manager.fork(root.process_id, "executor", ToolRegistry([]), 5, RingLevel.RING_3)
    child.status = ProcessStatus.TIMED_OUT
    child.result = {"status": "timed_out"}
    root.register_child_contract(child.process_id, {"required_for_parent_completion": True})
    root.status = ProcessStatus.COMPLETED
    successor = manager.fork(None, "supervisor", ToolRegistry([]), 5, RingLevel.RING_0,
                             session=root.session, task_id="next-root", actor_kind="supervisor")
    manager.inherit_terminal_coordination(successor, root)
    successor.status = ProcessStatus.COMPLETED
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / "isolated-state")
    try:
        for item in (root, child, successor):
            store.save_process_checkpoint(item)
            store.append_event(item.process_id, "agent_complete", {"status": item.status.value})
        restored = AgentProcessManager()
        candidates = restore_incomplete_processes(store, restored, tmp_path_factory,
                                                   include_process_ids=[successor.process_id])
        assert not candidates
        assert restored.get(child.process_id).status is ProcessStatus.TIMED_OUT
        assert restored.owns_child(restored.get(successor.process_id), restored.get(child.process_id))
        assert not restored._threads
    finally:
        store.close()


def test_host_time_allowance_extends_for_long_phases_but_never_hard_budget():
    with patch("backend.core.loop.budget.time.monotonic", return_value=100.0) as clock:
        budget = TaskTreeBudget.create("t", {"max_seconds": 1800, "initial_seconds": 300})
        budget.begin_provider_call()
        assert budget.remaining_seconds() == 360
        clock.return_value = 440.0
        budget.consume_output("still thinking")
        assert budget.remaining_seconds() == 300
        budget.reserve_time(900, reason="tool_phase:run_test")
        assert budget.remaining_seconds() == 900
        assert [e["version"] for e in budget.drain_extensions()] == [1, 2, 3]
        restored = TaskTreeBudget.from_snapshot(budget.snapshot())
        assert restored.remaining_seconds() == 900
        assert restored.hard_remaining_seconds() == 1460
        clock.return_value = 1901.0
        with pytest.raises(TaskBudgetExceeded):
            budget.reserve_time(10000, reason="not allowed")


def test_explicit_fixed_deadline_is_not_silently_extended():
    with patch("backend.core.loop.budget.time.monotonic", return_value=0.0) as clock:
        budget = TaskTreeBudget.create("t", {"max_seconds": 10})
        budget.begin_provider_call()
        assert budget.remaining_seconds() == 10
        clock.return_value = 11.0
        with pytest.raises(TaskBudgetExceeded):
            budget.consume_output("too late")


def test_routing_preference_defaults_and_round_trips():
    assert Config.from_dict({}).agent_routing == "owner"
    assert Config.from_dict({"projects": [], "agent_routing": "fresh"}).agent_routing == "fresh"


def test_historical_ownership_is_not_an_unrelated_new_task_obligation(tmp_path_factory):
    from backend.core.loop.completion_protocol import HostCompletionEvaluator
    from backend.core.loop.executor import _build_internal_tools
    from types import SimpleNamespace
    root, _ = _supervisor(tmp_path_factory)
    manager = root._manager
    child = manager.fork(root.process_id, "executor", ToolRegistry([]), 5, RingLevel.RING_3)
    child.status = ProcessStatus.TIMED_OUT
    child.result = {"status": "timed_out"}
    root.register_child_contract(child.process_id, {"required_for_parent_completion": True})
    root.status = ProcessStatus.COMPLETED
    successor = manager.fork(None, "supervisor", ToolRegistry([]), 5, RingLevel.RING_0,
        session=root.session, task_id="new-goal", actor_kind="supervisor", task_kind="supervisor")
    manager.inherit_terminal_coordination(successor, root)
    assert root.delegated_contracts[child.process_id]["required_for_parent_completion"] is True
    assert manager.owns_child(successor, child)
    assert HostCompletionEvaluator.evaluate(successor, "Unrelated task answered").allowed
    tools = _build_internal_tools(successor, {}, str(tmp_path_factory), object(), SimpleNamespace(_executors={}))
    result = tools["declare_task_contract"]({"goal": "Review previous work", "execution_mode": "review",
                                           "adopt_process_ids": [child.process_id]})
    assert result["accepted"], result
    assert not HostCompletionEvaluator.evaluate(successor, "Previous task completed").allowed


def test_terminal_coordination_inheritance_does_not_grow_transitively(tmp_path_factory):
    root, _ = _supervisor(tmp_path_factory)
    manager = root._manager
    old = manager.fork(root.process_id, "executor", ToolRegistry([]), 5, RingLevel.RING_3)
    old.status = ProcessStatus.COMPLETED
    root.register_child_contract(old.process_id, {"required_for_parent_completion": True})
    root.status = ProcessStatus.COMPLETED

    middle = manager.fork(None, "supervisor", ToolRegistry([]), 5, RingLevel.RING_0,
                          session=root.session, task_id="middle", actor_kind="supervisor")
    manager.inherit_terminal_coordination(middle, root)
    direct = manager.fork(middle.process_id, "executor", ToolRegistry([]), 5, RingLevel.RING_3)
    direct.status = ProcessStatus.COMPLETED
    middle.register_child_contract(direct.process_id, {"required_for_parent_completion": True})
    middle.status = ProcessStatus.COMPLETED

    latest = manager.fork(None, "supervisor", ToolRegistry([]), 5, RingLevel.RING_0,
                          session=root.session, task_id="latest", actor_kind="supervisor")
    manager.inherit_terminal_coordination(latest, middle)
    assert latest.child_ids == [direct.process_id]
    assert old.process_id not in latest.delegated_contracts


def test_corrupt_storage_open_publishes_blocked_health_without_reset(tmp_path_factory):
    import json
    from backend.core.storage import StorageRuntime
    from backend.core.storage.models import StorageCorruptionDetected
    runtime = StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state")
    paths = runtime.paths
    runtime.close()
    paths.observability_db.write_bytes(b"damaged database retained")
    with pytest.raises(StorageCorruptionDetected):
        StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state")
    assert paths.observability_db.read_bytes() == b"damaged database retained"
    health = json.loads(paths.health_file.read_text(encoding="utf-8"))
    assert health["level"] == "blocked"
    assert health["reasons"][0].startswith("storage_open_failed:")


def test_runner_wire_is_utf8_even_with_legacy_stdout():
    import io
    import json
    from backend.core.tools.runner import _emit_success
    raw = io.BytesIO()
    legacy = io.TextIOWrapper(raw, encoding="cp1252")
    with patch("sys.stdout", legacy):
        _emit_success({"content": "中文结果 ◉"})
    assert json.loads(raw.getvalue().decode("utf-8"))["data"]["content"] == "中文结果 ◉"
