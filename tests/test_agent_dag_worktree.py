"""Production DAG scheduling and real linked-worktree lifecycle tests."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.completion_protocol import HostCompletionEvaluator
from backend.core.loop.interface_contract import (
    capture_declared_contract,
    verify_declared_contract,
)
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.recovery import inspect_worktree_recovery
from backend.core.loop.test_manifest import SeedResult, TestManifest, TestRecord
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.worktree import AgentWorktreeManager, WorktreeError, WorktreeLease
from backend.core.storage import StorageRuntime


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Gitgo Test")
    _git(repo, "config", "user.email", "test@gitgo.invalid")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _process(manager: AgentProcessManager, *, parent_id=None, task_id="task", depends_on=None):
    return manager.fork(
        parent_id=parent_id,
        role="supervisor" if parent_id is None else "executor",
        tool_registry=ToolRegistry([]),
        max_steps=4,
        ring_level=RingLevel.RING_0 if parent_id is None else RingLevel.RING_3,
        workspace_path=str(manager.worktree_manager.repo_root)
        if manager.worktree_manager is not None else "",
        task_id=task_id,
        task_kind="action" if parent_id else "action",
        actor_kind="supervisor" if parent_id is None else "worker",
        depends_on=depends_on,
    )


def test_dag_waits_without_consuming_execution_order_and_propagates_failure(tmp_path_factory: Path):
    manager = AgentProcessManager(max_concurrency=1)
    root = manager.fork(
        parent_id=None, role="supervisor", tool_registry=ToolRegistry([]),
        max_steps=2, ring_level=RingLevel.RING_0, task_id="root",
    )
    upstream = manager.fork(
        parent_id=root.process_id, role="executor", tool_registry=ToolRegistry([]),
        max_steps=2, ring_level=RingLevel.RING_3, task_id="upstream",
    )
    downstream = manager.fork(
        parent_id=root.process_id, role="executor", tool_registry=ToolRegistry([]),
        max_steps=2, ring_level=RingLevel.RING_3, task_id="downstream",
        depends_on=[upstream.process_id],
    )
    downstream_started = threading.Event()
    upstream_release = threading.Event()

    manager.start(
        downstream.process_id,
        lambda: downstream_started.set() or {"status": "completed"},
    )
    assert not downstream_started.wait(0.15)

    def fail_upstream():
        upstream_release.wait(1)
        raise RuntimeError("upstream broke")

    manager.start(upstream.process_id, fail_upstream)
    upstream_release.set()
    manager.wait(upstream.process_id, timeout=2)
    result = manager.wait(downstream.process_id, timeout=2)
    assert result["code"] == "UPSTREAM_DEPENDENCY_FAILED"
    assert downstream.status == ProcessStatus.FAILED
    assert not downstream_started.is_set()


def test_real_worktree_dag_materializes_dirty_snapshot_and_promotes(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    repo = _repo(tmp_path)
    # The Agent task must see the admitted working-tree state, not only HEAD.
    (repo / "app.txt").write_text("user baseline\n", encoding="utf-8")
    (repo / "new.txt").write_text("untracked baseline\n", encoding="utf-8")
    storage = StorageRuntime(repo, state_home=tmp_path / "state")
    worktrees = AgentWorktreeManager(repo, storage)
    manager = AgentProcessManager(worktree_manager=worktrees)
    root = _process(manager, task_id="root-task")
    first = _process(manager, parent_id=root.process_id, task_id="root-task:1")

    first_path = Path(manager.materialize_process_worktree(first))
    assert first_path.parent == worktrees.worktrees_root
    assert storage.paths.project_root not in first_path.parents
    assert first_path != repo
    assert (first_path / "app.txt").read_text(encoding="utf-8") == "user baseline\n"
    assert (first_path / "new.txt").read_text(encoding="utf-8") == "untracked baseline\n"
    (first_path / "app.txt").write_text("first agent\n", encoding="utf-8")
    first.status = ProcessStatus.COMPLETED
    manager.seal_process_worktree(first)
    assert first.worktree["state"] == "sealed"
    assert first.worktree["own_commit"]

    second = _process(
        manager, parent_id=root.process_id, task_id="root-task:2",
        depends_on=[first.process_id],
    )
    second_path = Path(manager.materialize_process_worktree(second))
    assert (second_path / "app.txt").read_text(encoding="utf-8") == "first agent\n"
    (second_path / "second.txt").write_text("second agent\n", encoding="utf-8")
    second.status = ProcessStatus.COMPLETED
    manager.seal_process_worktree(second)

    root.child_reviews[first.process_id] = {"verdict": "approved", "summary": "ok"}
    root.child_reviews[second.process_id] = {"verdict": "approved", "summary": "ok"}
    promoted = manager.promote_process_results(root, [second.process_id])
    assert promoted["process_ids"] == [first.process_id, second.process_id]
    assert promoted["dependency_graph"] == {
        "updated": True,
        "rebuild_required": False,
        "reason": "no_graph_relevant_changes",
    }
    assert (repo / "app.txt").read_text(encoding="utf-8") == "first agent\n"
    assert (repo / "second.txt").read_text(encoding="utf-8") == "second agent\n"
    assert first.worktree["promoted"] is True
    assert second.worktree["promoted"] is True

    loaded = storage.load_worktree_state(second.process_id)
    assert loaded is not None
    assert loaded["promoted"] is True
    assert loaded["snapshot_commit"] == first.worktree["snapshot_commit"]
    manager.dispose_process_worktree(first)
    manager.dispose_process_worktree(second)
    storage.close()


def test_worktree_git_does_not_inherit_nested_host_control_pipe(tmp_path_factory: Path):
    """A daemon grandchild must not consume or retain its host JSON stdin."""
    tmp_path = tmp_path_factory
    repo = _repo(tmp_path)
    (repo / "app.txt").write_text("nested dirty state\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("nested input\n", encoding="utf-8")
    state_home = tmp_path / "nested-state"
    code = r"""
import sys
import threading
from pathlib import Path
from backend.core.loop.worktree import AgentWorktreeManager
from backend.core.storage import StorageRuntime

repo = Path(sys.argv[1])
storage = StorageRuntime(repo, state_home=Path(sys.argv[2]))
manager = AgentWorktreeManager(repo, storage)
result = {}

def run():
    try:
        lease = manager.create(process_id="nested-control-pipe", task_id="nested")
        manager.dispose(lease, keep_ref=False)
        result["state"] = lease.state
    except BaseException as exc:
        result["error"] = repr(exc)

thread = threading.Thread(target=run, daemon=True)
thread.start()
thread.join(15)
storage.close()
if thread.is_alive():
    raise SystemExit("nested worktree thread did not terminate")
if result != {"state": "disposed"}:
    raise SystemExit(repr(result))
print("nested-worktree-ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(repo), str(state_home)],
        cwd=Path(__file__).resolve().parents[1],
        input="host-protocol-input-must-not-be-consumed\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=25,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "nested-worktree-ok"


def test_worktree_seal_blocks_secret_before_internal_commit(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    repo = _repo(tmp_path)
    storage = StorageRuntime(repo, state_home=tmp_path / "state")
    worktrees = AgentWorktreeManager(repo, storage)
    manager = AgentProcessManager(worktree_manager=worktrees)
    root = _process(manager, task_id="privacy-root")
    child = _process(manager, parent_id=root.process_id, task_id="privacy-root:1")
    path = Path(manager.materialize_process_worktree(child))
    (path / "secret.txt").write_text("sk-" + "x" * 24, encoding="utf-8")
    child.status = ProcessStatus.COMPLETED
    with pytest.raises(WorktreeError, match="privacy scan blocked") as caught:
        manager.seal_process_worktree(child)
    message = str(caught.value)
    assert "secret.txt" in message
    assert '"line":1' in message
    assert "sk-" not in message
    assert "x" * 24 not in message
    assert storage.load_worktree_state(child.process_id)["state"] == "privacy_blocked"
    ref_check = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/gitgo/agents/{child.process_id}"],
        cwd=repo, capture_output=True, timeout=15, check=False,
    )
    assert ref_check.returncode != 0
    manager.dispose_process_worktree(child, keep_ref=False)
    storage.close()


def test_supervisor_reads_only_owned_sealed_child_artifacts(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    repo = _repo(tmp_path)
    storage = StorageRuntime(repo, state_home=tmp_path / "state")
    manager = AgentProcessManager(
        worktree_manager=AgentWorktreeManager(repo, storage)
    )
    root = _process(manager, task_id="artifact-root")
    child = _process(
        manager, parent_id=root.process_id, task_id="artifact-root:1",
    )
    path = Path(manager.materialize_process_worktree(child))
    (path / "result.txt").write_text("sealed result\n", encoding="utf-8")
    child.status = ProcessStatus.COMPLETED
    manager.seal_process_worktree(child)

    result = manager.read_sealed_artifact(
        root, child.process_id, "result.txt",
    )
    assert result["content"] == "sealed result\n"
    assert result["sealed_commit"] == child.worktree["result_commit"]

    stranger = _process(manager, task_id="other-root")
    with pytest.raises(PermissionError, match="owned child"):
        manager.read_sealed_artifact(stranger, child.process_id, "result.txt")
    with pytest.raises(WorktreeError, match="workspace-relative"):
        manager.read_sealed_artifact(root, child.process_id, "../result.txt")

    assert "read_child_artifact" in CapabilityProfiles.resolve_tools(
        "supervisor.control"
    )
    manager.dispose_process_worktree(child, keep_ref=False)
    storage.close()


def test_dag_edges_and_worktree_metadata_survive_store_reopen(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    repo = _repo(tmp_path)
    state_home = tmp_path / "state"
    storage = StorageRuntime(repo, state_home=state_home)
    worktrees = AgentWorktreeManager(repo, storage)
    manager = AgentProcessManager(worktree_manager=worktrees)
    root = _process(manager, task_id="persist-root")
    first = _process(manager, parent_id=root.process_id, task_id="persist-root:1")
    second = _process(
        manager, parent_id=root.process_id, task_id="persist-root:2",
        depends_on=[first.process_id],
    )
    Path(manager.materialize_process_worktree(first), "app.txt").write_text(
        "persisted\n", encoding="utf-8"
    )
    store = SessionStore(repo, storage=storage)
    store.save_process_checkpoint(root)
    store.save_process_checkpoint(first)
    store.save_process_checkpoint(second)
    storage.close()

    reopened = SessionStore(repo, state_home=state_home)
    state = reopened.load_process_state(second.process_id)
    assert [item["depends_on_process_id"] for item in state["dependencies"]] == [
        first.process_id
    ]
    worktree = reopened.load_worktree_state(first.process_id)
    assert worktree["state"] == "leased"
    assert Path(worktree["path"]).is_dir()
    reopened.close()


def test_supervisor_delegate_review_promote_uses_production_worktree(
    tmp_path_factory: Path, monkeypatch,
):
    from backend.core.loop import executor as executor_module

    repo = _repo(tmp_path_factory)
    storage = StorageRuntime(repo, state_home=tmp_path_factory / "state")
    manager = AgentProcessManager(
        worktree_manager=AgentWorktreeManager(repo, storage)
    )
    supervisor = manager.fork(
        parent_id=None, role="supervisor",
        tool_registry=ToolRegistry(
            CapabilityProfiles.resolve_tools("supervisor.control")
        ),
        max_steps=8, ring_level=RingLevel.RING_0,
        workspace_path=str(repo), task_id="production-root", task_kind="supervisor",
        actor_kind="supervisor", capability_profile_id="supervisor.control",
    )

    def fake_agent_step(*, process, workspace_path, **_kwargs):
        assert Path(workspace_path) != repo
        Path(workspace_path, "app.txt").write_text("delegated change\n", encoding="utf-8")
        process.tool_receipts.append({
            "receipt_id": "write-1", "tool_name": "write_file",
            "succeeded": True, "committed": True, "effect": "workspace_write",
        })
        process.status = ProcessStatus.COMPLETED
        process.result = {
            "status": "completed", "process_id": process.process_id,
            "response": "implemented", "metadata": {},
        }
        return process.result

    monkeypatch.setattr(executor_module, "agent_step", fake_agent_step)

    class Dispatcher:
        _executors = {}

    tools = executor_module._build_internal_tools(
        supervisor, {}, str(repo), object(), Dispatcher(),
    )
    delegated = tools["delegate_task"].execute({
        "task_description": "Change app.txt",
        "capability_profile_id": "development.workspace",
        "task_kind": "action",
        "target_files": ["app.txt"],
        "acceptance_criteria": ["app.txt is updated"],
        "max_steps": 3,
    })
    child = manager.get(delegated["process_id"])
    manager.wait(child.process_id, timeout=5)
    assert child.status == ProcessStatus.COMPLETED
    assert child.worktree["state"] == "sealed"
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\n"

    reviewed = tools["review_child_outcome"].execute({
        "process_id": child.process_id,
        "verdict": "approved",
        "summary": "The bounded change is correct",
    })
    assert reviewed["accepted"] is True
    blocked = HostCompletionEvaluator.evaluate(supervisor, "done")
    assert not blocked.allowed
    assert any("not promoted" in reason for reason in blocked.reasons)

    promoted = tools["promote_agent_changes"].execute({
        "process_ids": [child.process_id],
    })
    assert promoted["promoted"] is True
    assert (repo / "app.txt").read_text(encoding="utf-8") == "delegated change\n"
    assert HostCompletionEvaluator.evaluate(supervisor, "done").allowed
    manager.dispose_process_worktree(child)
    storage.close()


def test_approved_no_change_action_disposes_checkout_but_keeps_artifact_ref(
    tmp_path_factory: Path, monkeypatch,
):
    from backend.core.loop import executor as executor_module

    repo = _repo(tmp_path_factory)
    storage = StorageRuntime(repo, state_home=tmp_path_factory / "state")
    manager = AgentProcessManager(
        worktree_manager=AgentWorktreeManager(repo, storage)
    )
    supervisor = manager.fork(
        parent_id=None, role="supervisor",
        tool_registry=ToolRegistry(
            CapabilityProfiles.resolve_tools("supervisor.control")
        ),
        max_steps=8, ring_level=RingLevel.RING_0,
        workspace_path=str(repo), task_id="no-change-root", task_kind="supervisor",
        actor_kind="supervisor", capability_profile_id="supervisor.control",
    )

    def fake_agent_step(*, process, **_kwargs):
        process.tool_receipts.append({
            "receipt_id": "test-1", "tool_name": "run_test",
            "succeeded": True, "committed": True, "effect": "process",
        })
        process.status = ProcessStatus.COMPLETED
        process.result = {
            "status": "completed", "process_id": process.process_id,
            "response": "verified without source changes", "metadata": {},
        }
        return process.result

    monkeypatch.setattr(executor_module, "agent_step", fake_agent_step)

    class Dispatcher:
        _executors = {}

    tools = executor_module._build_internal_tools(
        supervisor, {}, str(repo), object(), Dispatcher(),
    )
    delegated = tools["delegate_task"].execute({
        "task_description": "Verify the current app without changing it",
        "capability_profile_id": "development.workspace",
        "task_kind": "action",
        "target_files": ["app.txt"],
        "acceptance_criteria": ["return test evidence"],
        "max_steps": 3,
    })
    child = manager.get(delegated["process_id"])
    manager.wait(child.process_id, timeout=5)
    checkout = Path(child.worktree["path"])
    assert child.worktree["state"] == "sealed"
    assert child.worktree["own_commit"] == ""
    assert checkout.exists()

    reviewed = tools["review_child_outcome"].execute({
        "process_id": child.process_id,
        "verdict": "approved",
        "summary": "Host evidence is sufficient",
    })
    assert reviewed["accepted"] is True
    assert reviewed["review"]["worktree_cleanup"] == "disposed_no_changes"
    assert child.worktree["state"] == "disposed"
    assert not checkout.exists()
    artifact = manager.read_sealed_artifact(
        supervisor, child.process_id, "app.txt",
    )
    assert artifact["content"] == "base\n"
    storage.close()


def test_delegate_task_dag_preflights_atomically_then_runs_topological_worktrees(
    tmp_path_factory: Path, monkeypatch,
):
    from backend.core.loop import executor as executor_module

    repo = _repo(tmp_path_factory)
    (repo / "api.py").write_text(
        "def stable_api(value: str) -> str:\n    return value\n",
        encoding="utf-8",
    )
    _git(repo, "add", "api.py")
    _git(repo, "commit", "-m", "add stable interface")
    storage = StorageRuntime(repo, state_home=tmp_path_factory / "state")
    manager = AgentProcessManager(
        worktree_manager=AgentWorktreeManager(repo, storage)
    )
    supervisor = manager.fork(
        parent_id=None, role="supervisor",
        tool_registry=ToolRegistry(
            CapabilityProfiles.resolve_tools("supervisor.control")
        ),
        max_steps=8, ring_level=RingLevel.RING_0,
        workspace_path=str(repo), task_id="dag:root/unsafe-name",
        task_kind="supervisor", actor_kind="supervisor",
        capability_profile_id="supervisor.control",
    )

    def fake_agent_step(*, process, workspace_path, instruction, **_kwargs):
        path = Path(workspace_path)
        if "first" in instruction:
            (path / "first.txt").write_text("first\n", encoding="utf-8")
        else:
            assert (path / "first.txt").read_text(encoding="utf-8") == "first\n"
            (path / "second.txt").write_text("second\n", encoding="utf-8")
        process.status = ProcessStatus.COMPLETED
        process.result = {
            "status": "completed", "process_id": process.process_id,
            "response": instruction, "metadata": {"large": "x" * 5000},
        }
        return process.result

    monkeypatch.setattr(executor_module, "agent_step", fake_agent_step)

    class Dispatcher:
        _executors = {}

    events = []
    tools = executor_module._build_internal_tools(
        supervisor, {}, str(repo), object(), Dispatcher(),
        on_stream_event=events.append,
    )
    invalid = tools["delegate_task_dag"].execute({
        "nodes": [
            {
                "node_id": "valid", "task_description": "first",
                "depends_on": [], "target_files": ["first.txt"],
                "acceptance_criteria": ["created"],
            },
            {
                "node_id": "invalid", "task_description": "second",
                "depends_on": ["valid"], "target_files": ["second.txt"],
                "acceptance_criteria": ["created"],
                "capability_profile_id": "missing.profile",
            },
        ],
    })
    assert invalid["delegated"] is False
    assert manager.children_of(supervisor.process_id) == []

    admitted = tools["delegate_task_dag"].execute({
        "nodes": [
            {
                "node_id": "first", "task_description": "first",
                "depends_on": [], "target_files": ["first.txt"],
                "acceptance_criteria": ["created"],
                "output_interfaces": ["api.py:stable_api"],
            },
            {
                "node_id": "second", "task_description": "second",
                "depends_on": [], "target_files": ["second.txt"],
                "acceptance_criteria": ["created"],
                "input_interfaces": ["api.py:stable_api"],
            },
        ],
    })
    assert admitted["delegated"] is True
    first = manager.get(admitted["nodes"]["first"])
    second = manager.get(admitted["nodes"]["second"])
    manager.wait_many([first.process_id, second.process_id], timeout=8)
    assert first.status == ProcessStatus.COMPLETED
    assert second.status == ProcessStatus.COMPLETED
    assert second.depends_on == [first.process_id]
    assert first.worktree["snapshot_commit"] == second.worktree["snapshot_commit"]
    assert any(item.get("event") == "agent_dag_admitted" for item in events)

    supervisor.child_reviews[first.process_id] = {"verdict": "approved"}
    supervisor.child_reviews[second.process_id] = {"verdict": "approved"}
    promoted = manager.promote_process_results(supervisor, [second.process_id])
    assert promoted["promoted"] is True
    assert (repo / "first.txt").read_text(encoding="utf-8") == "first\n"
    assert (repo / "second.txt").read_text(encoding="utf-8") == "second\n"
    manager.dispose_process_worktree(first)
    manager.dispose_process_worktree(second)
    storage.close()


def test_worktree_round_trips_ignored_test_evidence(tmp_path_factory: Path):
    repo = _repo(tmp_path_factory)
    (repo / ".gitignore").write_text(".gitgo/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore host runtime")
    root_manifest = TestManifest.load(repo)
    root_manifest.register(TestRecord(
        test_id="root:baseline", target="tests/test_root.py", seeds=[1],
        results=[SeedResult(1, 0, 1.0, "ok")], last_run_at="now",
    ))
    storage = StorageRuntime(repo, state_home=tmp_path_factory / "state")
    manager = AgentProcessManager(
        worktree_manager=AgentWorktreeManager(repo, storage)
    )
    root = _process(manager, task_id="evidence-root")
    child = _process(manager, parent_id=root.process_id, task_id="evidence-root:1")
    child_path = Path(manager.materialize_process_worktree(child))
    assert "root:baseline" in TestManifest.load(child_path).records
    child_manifest = TestManifest.load(child_path)
    child_manifest.register(TestRecord(
        test_id="child:proof", target="tests/test_child.py", seeds=[42],
        results=[SeedResult(42, 0, 2.0, "passed")], last_run_at="later",
    ))
    child.status = ProcessStatus.COMPLETED
    manager.seal_process_worktree(child)
    merged = TestManifest.load(repo)
    assert merged.records["root:baseline"].passed
    assert merged.records["child:proof"].passed
    manager.dispose_process_worktree(child)
    storage.close()


def test_missing_durable_worktree_is_never_silently_resumed(tmp_path_factory: Path):
    findings = inspect_worktree_recovery({
        "isolated": True,
        "state": "leased",
        "path": str(tmp_path_factory / "missing-worktree"),
    })
    assert findings == [{
        "code": "WORKTREE_PATH_MISSING",
        "path": str(tmp_path_factory / "missing-worktree"),
    }]


def test_declared_interface_contract_detects_signature_drift(tmp_path_factory: Path):
    repo = _repo(tmp_path_factory)
    api = repo / "api.py"
    api.write_text(
        "def stable_api(value: str) -> str:\n    return value\n",
        encoding="utf-8",
    )
    contract = capture_declared_contract(repo, [{
        "node_id": "owner",
        "output_interfaces": ["api.py:stable_api"],
        "input_interfaces": [],
    }])
    assert verify_declared_contract(
        contract, repo, ["api.py:stable_api"],
    ) == []
    api.write_text(
        "def stable_api(value: str, mode: str = 'new') -> str:\n    return value\n",
        encoding="utf-8",
    )
    violations = verify_declared_contract(
        contract, repo, ["api.py:stable_api"],
    )
    assert violations[0]["type"] == "signature_changed"


def test_child_terminal_event_reports_privacy_seal_failure_not_false_success(
    tmp_path_factory: Path, monkeypatch,
):
    from backend.core.loop import executor as executor_module

    repo = _repo(tmp_path_factory)
    storage = StorageRuntime(repo, state_home=tmp_path_factory / "state")
    manager = AgentProcessManager(
        worktree_manager=AgentWorktreeManager(repo, storage)
    )
    supervisor = manager.fork(
        parent_id=None, role="supervisor",
        tool_registry=ToolRegistry(
            CapabilityProfiles.resolve_tools("supervisor.control")
        ),
        max_steps=6, ring_level=RingLevel.RING_0,
        workspace_path=str(repo), task_id="privacy-production",
        task_kind="supervisor", actor_kind="supervisor",
        capability_profile_id="supervisor.control",
    )

    def fake_agent_step(*, process, workspace_path, **_kwargs):
        Path(workspace_path, "secret.txt").write_text(
            "sk-" + "z" * 24, encoding="utf-8",
        )
        process.status = ProcessStatus.COMPLETED
        process.result = {
            "status": "completed", "process_id": process.process_id,
            "response": "done", "metadata": {},
        }
        return process.result

    monkeypatch.setattr(executor_module, "agent_step", fake_agent_step)

    class Dispatcher:
        _executors = {}

    events = []
    tools = executor_module._build_internal_tools(
        supervisor, {}, str(repo), object(), Dispatcher(),
        on_stream_event=events.append,
    )
    delegated = tools["delegate_task"].execute({
        "task_description": "write secret fixture",
        "target_files": ["secret.txt"],
        "acceptance_criteria": ["file exists"],
    })
    child = manager.get(delegated["process_id"])
    manager.wait(child.process_id, timeout=6)
    assert child.status == ProcessStatus.FAILED
    assert child.result["code"] == "CHILD_LIFECYCLE_FAILED"
    assert child.worktree["state"] == "privacy_blocked"
    terminal = [item for item in events if item.get("event") == "agent_terminal"][-1]
    assert terminal["status"] == "failed"
    assert terminal["outcome"]["code"] == "CHILD_LIFECYCLE_FAILED"
    manager.dispose_process_worktree(child, keep_ref=False)
    storage.close()
