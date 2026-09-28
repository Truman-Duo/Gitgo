"""AgentProcessManager — 进程树生命周期管理。

fork → wait → kill → reap。daemon 调用 fork；daemon loop 调 reap 回收孤儿。

SessionStore persists authoritative session state through SQLite + CAS.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel

if TYPE_CHECKING:
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.session import AgentSession


def _serialized_admission(method):
    @wraps(method)
    def admit(self, *args, **kwargs):
        with self._state_condition:
            return method(self, *args, **kwargs)
    return admit


class AgentProcessManager:
    """进程树管理器。daemon 之上的调度层。"""

    # 集成点：daemon 在启动时创建实例，workspace_dirty 处理前调 fork，
    # 定期调 reap 回收孤儿
    MAX_FORK_DEPTH = 2

    def __init__(self, max_concurrency: int = 4, *, worktree_manager=None, presentation=None):
        self.presentation = presentation
        self._processes: dict[str, AgentProcess] = {}
        self._sessions: dict[str, "AgentSession"] = {}
        self._slots = threading.BoundedSemaphore(max(1, max_concurrency))
        self._resource_condition = threading.Condition()
        # process_id -> (resources, mode).  Read-only workers may share the
        # same source paths; writers and snapshot reviewers remain exclusive.
        self._resource_claims: dict[str, tuple[set[str], str]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._state_condition = threading.Condition()
        self._state_version = 0
        self.worktree_manager = worktree_manager
        self._worktree_lock = threading.RLock()

    @_serialized_admission
    def fork(self, parent_id: str | None, role: str,
             tool_registry: "ToolRegistry", max_steps: int,
             ring_level: RingLevel,
             context_snapshot: dict | None = None,
             task_description: str = "",
             task_id: str = "",
             workspace_path: str = "",
             provider_id: str = "",
             model_id: str = "",
             actor_kind: str = "worker",
             capability_profile_id: str = "text.only",
             capability_lease=None,
             task_kind: str = "answer",
             required_test_ids: list[str] | None = None,
             depends_on: list[str] | None = None,
             task_budget_limits: dict | None = None,
             runtime_preferences: dict | None = None,
             child_budget_request: dict | None = None,
             session_id: str = "",
             session: "AgentSession | None" = None) -> AgentProcess:
        """创建子进程。parent_id=None 时为 A 级 Agent (ring 0)。

        B 级（parent_id 非 None）初始为 WAITING，agent_step 真正启动时转 RUNNING，
        让 dashboard 能显示真实的 "pending" 状态。"""
        # Fork depth ≤ 2 检查
        if parent_id is not None:
            parent = self._processes.get(parent_id)
            if parent and parent.parent_id is not None:
                raise ValueError(
                    f"Fork depth exceeds {self.MAX_FORK_DEPTH}. "
                    f"B-level agents cannot fork."
                )

        dependencies = list(dict.fromkeys(
            str(item) for item in (depends_on or []) if str(item)
        ))
        self._validate_dependencies(parent_id, dependencies)

        # B 级 Agent 的 context_snapshot 不能包含 contract 原文或完整 lessons
        if ring_level == RingLevel.RING_3 and context_snapshot:
            forbidden = {"contract_yaml", "lessons_full", "contract_raw"}
            if forbidden & set(context_snapshot.keys()):
                raise ValueError(
                    "B-level agent context must not contain raw contract "
                    "or full lessons. Use governance_brief summaries instead."
                )

        from backend.core.loop.runtime import AgentRuntimeFactory, RuntimeSpec

        if session is None and session_id:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError(f"Session not found: {session_id}")

        if session is not None:
            terminal = {ProcessStatus.COMPLETED, ProcessStatus.FAILED,
                        ProcessStatus.TIMED_OUT, ProcessStatus.CANCELLED, ProcessStatus.KILLED,
                        ProcessStatus.ORPHANED}
            if any(p.session.session_id == session.session_id and
                   (p.status not in terminal or (p.process_id in self._threads
                    and self._threads[p.process_id].is_alive()))
                   for p in self._processes.values()):
                raise ValueError("SESSION_EXECUTION_ACTIVE: wait for the previous execution to finish before continuing this session")

        parent = self._processes.get(parent_id) if parent_id else None
        if parent is not None and child_budget_request and child_budget_request.get("steps") is not None:
            max_steps = max(1, min(max_steps, int(child_budget_request["steps"])))
        context_snapshot = dict(context_snapshot or {})
        if parent is not None and "relationship_policy" not in context_snapshot:
            from backend.core.loop.coordination import relationship_policy
            relation = relationship_policy(
                owner_process_id=parent.process_id,
                depends_on=dependencies,
                continuation_process_id=(parent_id if session is not None else ""),
                required_for_parent_completion=bool(
                    dict(context_snapshot.get("task_contract") or {}).get(
                        "required_for_parent_completion", True,
                    )
                ),
                capability_profile_id=capability_profile_id,
                task_kind=task_kind,
            )
            context_snapshot["relationship_policy"] = relation
            contract_snapshot = dict(context_snapshot.get("task_contract") or {})
            contract_snapshot.setdefault("relationship_policy", relation)
            context_snapshot["task_contract"] = contract_snapshot
        if parent is not None and parent.task_budget is not None:
            task_budget = parent.task_budget
            task_budget.reserve_agent()
        else:
            from backend.core.loop.budget import TaskTreeBudget
            task_budget = TaskTreeBudget.create(
                task_id or parent_id or "unscoped-task", task_budget_limits,
            )

        try:
            process = AgentRuntimeFactory.create(
                RuntimeSpec(
                    role=role,
                    ring_level=ring_level,
                    tool_registry=tool_registry,
                    max_steps=max_steps,
                    parent_id=parent_id,
                    context_snapshot=context_snapshot,
                    task_description=task_description,
                    task_id=task_id,
                    workspace_path=workspace_path,
                    provider_id=provider_id,
                    model_id=model_id,
                    actor_kind=actor_kind,
                    capability_profile_id=capability_profile_id,
                    capability_lease=capability_lease,
                    task_kind=task_kind,
                    required_test_ids=tuple(required_test_ids or []),
                    task_budget=task_budget,
                    runtime_preferences=(
                        runtime_preferences
                        if runtime_preferences is not None
                        else getattr(parent, "runtime_preferences", {})
                    ),
                ),
                session=session,
            )
            process.budget_lease = task_budget.register_process(
                process.process_id,
                parent_id=parent_id,
                actor_kind=actor_kind,
                max_steps=max_steps,
                request=child_budget_request,
            )
        except Exception:
            task_budget.cancel_agent_reservation()
            raise
        process.depends_on = dependencies
        process.workspace_root = (
            str(Path(workspace_path).resolve()) if workspace_path else ""
        )
        self._processes[process.process_id] = process
        self._sessions[process.session.session_id] = process.session
        process._manager = self

        if parent is not None and not process.session.display_name:
            with parent.session._context_memo_lock:
                parent.session.worker_sequence += 1
                process.session.display_name = f"B{parent.session.worker_sequence}"

        from backend.core.history import HistoryManager
        if workspace_path:
            HistoryManager.set_workspace(workspace_path)
            HistoryManager.add_operation(
                "system", "agent_forked", "success",
                {"process_id": process.process_id, "role": role,
                 "ring_level": ring_level.value, "parent_id": parent_id},
                correlation_id=process.process_id,
            )
        return process

    def start(self, process_id: str, target, *, resources: list[str] | None = None,
              resource_mode: str = "exclusive") -> None:
        """Run a child asynchronously under concurrency and resource bounds.

        ``shared`` claims can overlap each other.  ``exclusive`` claims conflict
        with either mode, giving development workers and reviewers stable
        workspace semantics without serialising independent read-only audits.
        """
        process = self._processes.get(process_id)
        if process is None:
            raise ValueError(f"Process not found: {process_id}")
        if process_id in self._threads and self._threads[process_id].is_alive():
            raise ValueError(f"Process already started: {process_id}")
        claims = set(resources or [])
        resource_mode = str(resource_mode).strip().lower()
        if resource_mode not in {"shared", "exclusive"}:
            raise ValueError("resource_mode must be shared or exclusive")

        def run() -> None:
            process.lifecycle_finalized = False
            def release_budget() -> None:
                if process.task_budget is not None:
                    released = process.task_budget.release_process(
                        process.process_id, status=process.status.value,
                    )
                    if released is not None:
                        process.budget_lease = released
            dependency_error = self._wait_for_dependencies(process)
            if dependency_error:
                process.status = ProcessStatus.FAILED
                process.result = {
                    "process_id": process.process_id,
                    "status": "failed",
                    "error": dependency_error,
                    "code": "UPSTREAM_DEPENDENCY_FAILED",
                }
                process.lifecycle_finalized = True
                release_budget()
                self.notify_state_changed(process.process_id)
                return
            with self._slots:
                if not self._acquire_resources(process, claims, resource_mode):
                    process.status = ProcessStatus.CANCELLED
                    process.result = {
                        "process_id": process.process_id,
                        "status": "cancelled",
                        "error": "cancelled while waiting for resources",
                    }
                    process.lifecycle_finalized = True
                    release_budget()
                    self.notify_state_changed(process.process_id)
                    return
                try:
                    result = target()
                    if isinstance(result, dict):
                        process.result = result
                except Exception as exc:
                    process.status = ProcessStatus.FAILED
                    process.result = {
                        "process_id": process.process_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                finally:
                    self._release_resources(process.process_id)
                    release_budget()
                    process.lifecycle_finalized = True
                    self.notify_state_changed(process.process_id)

        thread = threading.Thread(
            target=run, name=f"gitgo-agent-{process_id[:8]}", daemon=True,
        )
        self._threads[process_id] = thread
        thread.start()

    def register_thread(self, process_id: str, thread: threading.Thread) -> None:
        """Register a root task thread so recursive cancellation can await it."""
        self._threads[process_id] = thread

    def register_restored(self, process: AgentProcess) -> None:
        """Publish a checkpoint-rebuilt process without creating new authority."""
        if process.process_id in self._processes:
            raise ValueError(f"Process already registered: {process.process_id}")
        from backend.core.loop.runtime import AgentRuntimeFactory
        AgentRuntimeFactory.assert_valid(process)
        if process.task_budget is not None:
            process.budget_lease = dict(
                process.task_budget.snapshot()
                .get("process_leases", {})
                .get(process.process_id, {})
            )
        self._processes[process.process_id] = process
        self._sessions[process.session.session_id] = process.session
        process._manager = self
        self.notify_state_changed(process.process_id)

    def children_of(self, parent_id: str) -> list[AgentProcess]:
        return sorted(
            (item for item in self._processes.values() if item.parent_id == parent_id),
            key=lambda item: item.created_at,
        )

    def list_processes(self) -> list[AgentProcess]:
        with self._state_condition:
            return list(self._processes.values())

    @staticmethod
    def owns_child(supervisor: AgentProcess, child: AgentProcess | None) -> bool:
        """Accept direct or explicitly inherited same-session child authority."""
        if child is None:
            return False
        if child.parent_id == supervisor.process_id:
            return True
        child_ids, contracts, _reviews = supervisor.coordination_snapshot()
        return child.process_id in child_ids and child.process_id in contracts

    def inherit_terminal_coordination(
        self, successor: AgentProcess, predecessor: AgentProcess,
    ) -> None:
        """Carry the predecessor's direct B outcomes into a same-session root.

        Historical children remain discoverable through the project process index
        and may be adopted explicitly by contract.  Re-copying already inherited
        children on every public turn makes ownership grow transitively without
        bound and pollutes the new task's completion and presentation state.
        """
        if successor.parent_id is not None or predecessor.parent_id is not None:
            raise ValueError("coordination inheritance is root-to-root only")
        if successor.session.session_id != predecessor.session.session_id:
            raise ValueError("coordination inheritance requires the same session")
        child_ids, contracts, reviews = predecessor.coordination_snapshot()
        direct_child_ids = [
            child_id for child_id in child_ids
            if (
                (self.get(child_id) is not None
                 and self.get(child_id).parent_id == predecessor.process_id)
                or not dict(contracts.get(child_id) or {}).get("inherited_from_process_id")
            )
        ]
        with successor._coordination_lock:
            successor.child_ids = list(dict.fromkeys(
                [*successor.child_ids, *direct_child_ids]
            ))
            successor.delegated_contracts.update({
                child_id: {**contract,
                    "inherited_from_process_id": predecessor.process_id,
                    "historical_required": bool(contract.get("required_for_parent_completion", True)),
                    "required_for_parent_completion": False}
                for child_id, contract in contracts.items()
                if child_id in direct_child_ids
            })
            successor.child_reviews.update({
                child_id: review for child_id, review in reviews.items()
                if child_id in direct_child_ids
            })
        successor.runtime_preferences["predecessor_process_id"] = predecessor.process_id

    def downstream_of(self, process_id: str) -> list[AgentProcess]:
        return sorted(
            (item for item in self._processes.values() if process_id in item.depends_on),
            key=lambda item: item.created_at,
        )

    def _validate_dependencies(
        self, parent_id: str | None, dependencies: list[str],
    ) -> None:
        if dependencies and parent_id is None:
            raise ValueError("root supervisors cannot declare sibling dependencies")
        for upstream_id in dependencies:
            upstream = self._processes.get(upstream_id)
            if upstream is None:
                raise ValueError(f"dependency process not found: {upstream_id}")
            if upstream.parent_id != parent_id:
                raise ValueError("DAG dependencies must share one supervisor owner")

    def _wait_for_dependencies(self, process: AgentProcess) -> str:
        """Wait at a Host boundary without consuming an Agent concurrency slot."""
        if not process.depends_on:
            return ""
        active = {
            ProcessStatus.RUNNING, ProcessStatus.WAITING,
            ProcessStatus.AWAITING_USER, ProcessStatus.CANCELLING,
            ProcessStatus.RESUME_AVAILABLE,
            ProcessStatus.RECOVERY_REVIEW_REQUIRED, ProcessStatus.RECOVERING,
        }
        with self._state_condition:
            while True:
                if process.cancellation_event.is_set():
                    return "cancelled while waiting for upstream dependencies"
                upstream = [self._processes.get(item) for item in process.depends_on]
                missing = [
                    process.depends_on[index]
                    for index, item in enumerate(upstream) if item is None
                ]
                if missing:
                    return "upstream dependency disappeared: " + ", ".join(missing)
                if any(
                    item.status in active or (
                        item.process_id in self._threads
                        and not item.lifecycle_finalized
                    )
                    for item in upstream if item is not None
                ):
                    self._state_condition.wait(timeout=0.5)
                    continue
                failed = [
                    item.process_id for item in upstream
                    if item is not None and item.status != ProcessStatus.COMPLETED
                ]
                if failed:
                    return "upstream dependency did not complete: " + ", ".join(failed)
                # An upstream may have changed a declared interface after DAG
                # admission.  The Host has already routed the versioned proposal
                # to A and this worker; do not start on a stale contract while A
                # is still deciding whether to accept, reject, or ask the user.
                from backend.core.loop.coordination import coordination_block_reason
                if coordination_block_reason(process):
                    self._state_condition.wait(timeout=0.5)
                    continue
                return ""

    def dependency_order(self, process_ids: list[str]) -> list[AgentProcess]:
        """Return deterministic topological order or reject an invalid graph."""
        selected = set(process_ids)
        nodes = {item: self._processes.get(item) for item in selected}
        missing = sorted(item for item, value in nodes.items() if value is None)
        if missing:
            raise ValueError("process not found: " + ", ".join(missing))
        indegree = {
            item: sum(1 for dep in nodes[item].depends_on if dep in selected)
            for item in selected
        }
        ready = sorted(item for item, degree in indegree.items() if degree == 0)
        ordered: list[AgentProcess] = []
        while ready:
            current = ready.pop(0)
            ordered.append(nodes[current])
            for downstream in sorted(selected):
                if current not in nodes[downstream].depends_on:
                    continue
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    ready.append(downstream)
                    ready.sort()
        if len(ordered) != len(selected):
            raise ValueError("DAG contains a cycle")
        return ordered

    def dependency_closure(self, process: AgentProcess) -> list[AgentProcess]:
        selected: set[str] = set()
        stack = list(process.depends_on)
        while stack:
            process_id = stack.pop()
            if process_id in selected:
                continue
            upstream = self._processes.get(process_id)
            if upstream is None:
                raise ValueError(f"dependency process not found: {process_id}")
            selected.add(process_id)
            stack.extend(upstream.depends_on)
        return self.dependency_order(list(selected)) if selected else []

    def materialize_process_worktree(
        self, process: AgentProcess, *, review_target_id: str = "",
    ) -> str:
        """Attach a deterministic isolated checkout after DAG admission."""
        if self.worktree_manager is None:
            return process.worktree_path or process.workspace_root
        from backend.core.loop.worktree import WorktreeError

        with self._worktree_lock:
            owner = self._processes.get(process.parent_id or "")
            if owner is None:
                raise WorktreeError("isolated child has no supervisor owner")
            if review_target_id:
                target = self._processes.get(review_target_id)
                commit = str((target.worktree if target else {}).get("result_commit") or "")
                if not commit:
                    raise WorktreeError("review target has no sealed worktree result")
                lease = self.worktree_manager.create(
                    process_id=process.process_id,
                    task_id=process.active_task_id,
                    mode="review",
                    review_commit=commit,
                    context_refs=dict(
                        process.read_context_snapshot()[0].get("context_refs", {}) or {}
                    ),
                )
            else:
                snapshot = str((owner.worktree or {}).get("snapshot_commit") or "")
                upstream = self.dependency_closure(process)
                commits = [
                    str((item.worktree or {}).get("own_commit") or "")
                    for item in upstream
                ]
                lease = self.worktree_manager.create(
                    process_id=process.process_id,
                    task_id=process.active_task_id,
                    snapshot_commit=snapshot,
                    upstream_commits=[item for item in commits if item],
                    mode="write",
                    context_refs=dict(
                        process.read_context_snapshot()[0].get("context_refs", {}) or {}
                    ),
                )
                if not snapshot:
                    owner.worktree = {
                        **dict(owner.worktree or {}),
                        "snapshot_commit": lease.snapshot_commit,
                        "base_commit": lease.base_commit,
                        "state": "task_snapshot",
                        "isolated": False,
                    }
            process.worktree = lease.to_dict()
            process.worktree_path = lease.path
            return lease.path

    def seal_process_worktree(self, process: AgentProcess) -> dict:
        if self.worktree_manager is None or not process.worktree.get("isolated"):
            return dict(process.worktree or {})
        if process.worktree.get("mode") != "write":
            return dict(process.worktree)
        from backend.core.loop.worktree import WorktreeLease

        with self._worktree_lock:
            try:
                lease = self.worktree_manager.seal(
                    WorktreeLease.from_dict(process.worktree)
                )
            except Exception:
                # seal() persists privacy_blocked/failed before raising.  Keep
                # the live DTO aligned with that authoritative row so the
                # terminal event and Dashboard cannot continue to show leased.
                durable = self.worktree_manager.storage.load_worktree_state(
                    process.process_id
                )
                if durable:
                    process.worktree = dict(durable)
                raise
            process.worktree = lease.to_dict()
            return dict(process.worktree)

    def read_sealed_artifact(
        self, supervisor: AgentProcess, process_id: str, path: str, *,
        sha256: str = "", offset: int = 0, max_chars: int = 24000,
    ) -> dict:
        """Read an owned child's immutable result without exposing worktree paths."""
        if self.worktree_manager is None:
            raise ValueError("isolated worktree runtime is unavailable")
        child = self._processes.get(process_id)
        if not self.owns_child(supervisor, child):
            raise PermissionError("artifact process must be an owned child task")
        worktree = dict(child.worktree or {})
        if worktree.get("state") not in {"sealed", "disposed"}:
            raise ValueError("child result is not sealed")
        commit = str(worktree.get("result_commit") or "")
        if not commit:
            raise ValueError("child result commit is missing")
        return {
            "process_id": child.process_id,
            **self.worktree_manager.read_artifact(
                commit, path, sha256=sha256, offset=offset,
                max_chars=max_chars,
            ),
        }

    def dispose_process_worktree(
        self, process: AgentProcess, *, keep_ref: bool = True,
    ) -> dict:
        if self.worktree_manager is None or not process.worktree.get("path"):
            return dict(process.worktree or {})
        from backend.core.loop.worktree import WorktreeLease

        with self._worktree_lock:
            lease = self.worktree_manager.dispose(
                WorktreeLease.from_dict(process.worktree), keep_ref=keep_ref,
            )
            process.worktree = lease.to_dict()
            return dict(process.worktree)

    def promote_process_results(
        self, supervisor: AgentProcess, process_ids: list[str],
    ) -> dict:
        """Promote an approved DAG closure into the user's working repository."""
        if self.worktree_manager is None:
            raise ValueError("isolated worktree runtime is unavailable")
        selected: set[str] = set()
        for process_id in process_ids:
            child = self._processes.get(process_id)
            if not self.owns_child(supervisor, child):
                raise ValueError("promotion may reference only owned child tasks")
            selected.add(process_id)
            selected.update(item.process_id for item in self.dependency_closure(child))
        ordered = self.dependency_order(list(selected))
        snapshots = {
            str((item.worktree or {}).get("snapshot_commit") or "") for item in ordered
        }
        snapshots.discard("")
        if len(snapshots) != 1:
            raise ValueError("promotion nodes do not share one task snapshot")
        for item in ordered:
            if item.status != ProcessStatus.COMPLETED:
                raise ValueError(f"process is not completed: {item.process_id}")
            if (item.worktree or {}).get("state") != "sealed":
                raise ValueError(f"process has no sealed result: {item.process_id}")
            review = (supervisor.child_reviews or {}).get(item.process_id, {})
            if review.get("verdict") != "approved":
                raise ValueError(f"process lacks supervisor approval: {item.process_id}")
        commits = [
            str((item.worktree or {}).get("own_commit") or "") for item in ordered
        ]
        with self._worktree_lock:
            result = self.worktree_manager.promote(
                snapshot_commit=next(iter(snapshots)),
                ordered_commits=[item for item in commits if item],
                promotion_id=supervisor.active_task_id or supervisor.process_id,
            )
            try:
                from backend.core.dependency_graph import (
                    build_dependency_graph, dependency_graph_affected_by,
                )
                if dependency_graph_affected_by(result.get("changed_files", [])):
                    graph = build_dependency_graph(self.worktree_manager.repo_root)
                    result["dependency_graph"] = {
                        "updated": True,
                        "nodes": len(getattr(graph, "file_fingerprints", {}) or {}),
                        "edges": len(getattr(graph, "edges", {}) or {}),
                    }
                else:
                    result["dependency_graph"] = {
                        "updated": True,
                        "rebuild_required": False,
                        "reason": "no_graph_relevant_changes",
                    }
            except Exception as exc:
                # Code promotion is already atomic and complete. Keep this
                # separate, explicit governance health signal instead of
                # pretending the dependency graph was refreshed.
                result["dependency_graph"] = {
                    "updated": False,
                    "error": str(exc),
                }
            from backend.core.loop.worktree import WorktreeLease
            for item in ordered:
                lease = self.worktree_manager.mark_promoted(
                    WorktreeLease.from_dict(item.worktree)
                )
                item.worktree = lease.to_dict()
            return {**result, "process_ids": [item.process_id for item in ordered]}

    def send_instruction(self, process_id: str, content: str) -> dict:
        process = self._processes.get(process_id)
        if process is None or process.mailbox is None:
            return {"accepted": False, "error": "process not found"}
        try:
            message = process.mailbox.enqueue_instruction(content)
        except Exception as exc:
            return {"accepted": False, "error": str(exc)}
        return {"accepted": True, **message.to_dict()}

    def _acquire_resources(self, process: AgentProcess, claims: set[str],
                           mode: str = "exclusive") -> bool:
        with self._resource_condition:
            while self._claims_conflict(claims, mode):
                if process.cancellation_event.wait(0.1):
                    return False
                self._resource_condition.wait(timeout=0.1)
            self._resource_claims[process.process_id] = (claims, mode)
            return True

    def _release_resources(self, process_id: str) -> None:
        with self._resource_condition:
            self._resource_claims.pop(process_id, None)
            self._resource_condition.notify_all()

    def _claims_conflict(self, claims: set[str], mode: str = "exclusive") -> bool:
        if not claims:
            return False
        from backend.core.loop.tool_execution import _resources_conflict
        return any(
            _resources_conflict(claims, active_claims)
            and (mode == "exclusive" or active_mode == "exclusive")
            for active_claims, active_mode in self._resource_claims.values()
        )

    def wait(self, process_id: str,
             timeout: float | None = None,
             cancel_event=None) -> dict | None:
        """Block on a state notification until one process becomes terminal."""
        process = self._processes.get(process_id)
        if not process:
            return None
        self.wait_many(
            [process_id], timeout=timeout, cancel_event=cancel_event,
            return_when="all_terminal",
        )
        return process.result

    def notify_state_changed(self, process_id: str = "") -> int:
        """Publish a scheduler state transition and wake all interested waiters."""
        with self._state_condition:
            self._state_version += 1
            version = self._state_version
            self._state_condition.notify_all()
            return version

    def wait_many(
        self,
        process_ids: list[str],
        *,
        timeout: float | None = None,
        cancel_event=None,
        return_when: str = "all_terminal",
    ) -> dict:
        """Wait concurrently for a set of processes without polling each child.

        ``return_when`` is either ``all_terminal`` or ``any_terminal``.  The
        returned state version lets callers distinguish a real wake-up from a
        timeout and build an event-driven projection.
        """
        if return_when not in {"all_terminal", "any_terminal"}:
            raise ValueError("return_when must be all_terminal or any_terminal")
        ids = list(dict.fromkeys(str(item) for item in process_ids if str(item)))
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)

        def snapshot() -> dict[str, dict]:
            result: dict[str, dict] = {}
            for process_id in ids:
                process = self._processes.get(process_id)
                result[process_id] = {
                    "status": process.status.value if process else "not_found",
                    "result": process.result if process else None,
                    "lifecycle_finalized": (
                        (process.lifecycle_finalized or process_id not in self._threads)
                        if process else True
                    ),
                }
            return result

        def ready(states: dict[str, dict]) -> bool:
            terminal = [
                item["status"] not in {
                    "running", "waiting", "cancelling", "awaiting_user",
                    "resume_available", "recovery_review_required", "recovering",
                } and item.get("lifecycle_finalized", True)
                for item in states.values()
            ]
            return (all(terminal) if return_when == "all_terminal" else any(terminal))

        with self._state_condition:
            initial_version = self._state_version
            states = snapshot()
            while ids and not ready(states):
                if cancel_event is not None and cancel_event.is_set():
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._state_condition.wait(
                    timeout=min(remaining, 0.5) if remaining is not None else 0.5,
                )
                states = snapshot()
            return {
                "processes": states,
                "all_terminal": bool(states) and all(
                    item["status"] not in {
                        "running", "waiting", "cancelling", "awaiting_user",
                        "resume_available", "recovery_review_required", "recovering",
                    } and item.get("lifecycle_finalized", True)
                    for item in states.values()
                ),
                "state_version": self._state_version,
                "state_changed": self._state_version != initial_version,
            }

    def kill(self, process_id: str, *, reason: str = "user_cancelled",
             wait_timeout: float = 0.0) -> dict:
        """Cancel a process and every live descendant in its task subtree."""
        process = self._processes.get(process_id)
        if process is None:
            return {"requested": False, "status": "not_found", "process_ids": []}

        targets = self._cancellation_closure(process_id)
        requested: list[str] = []
        cancelled_parked: list[str] = []
        already_cancelling: list[str] = []
        terminal_before: list[str] = []
        for target in reversed(targets):
            if target.status in (
                ProcessStatus.RUNNING, ProcessStatus.WAITING,
                ProcessStatus.AWAITING_USER,
                ProcessStatus.RESUME_AVAILABLE,
                ProcessStatus.RECOVERY_REVIEW_REQUIRED,
                ProcessStatus.RECOVERING,
            ):
                previous_status = target.status
                target.cancel_requested = True
                target.cancellation_reason = reason
                target.cancellation_event.set()
                pending = target.pending_decision
                if isinstance(pending, dict) and target.session is not None:
                    target.session.host_ledger.append({
                        "event": "user_decision_cancelled",
                        "decision_id": str(pending.get("decision_id") or ""),
                        "process_id": target.process_id,
                        "task_id": target.active_task_id,
                        "reason": reason,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    target.pending_decision = None
                thread = self._threads.get(target.process_id)
                parked = previous_status in {
                    ProcessStatus.AWAITING_USER, ProcessStatus.RESUME_AVAILABLE,
                    ProcessStatus.RECOVERY_REVIEW_REQUIRED,
                } and (thread is None or not thread.is_alive())
                if parked:
                    target.status = ProcessStatus.CANCELLED
                    target.result = {
                        "process_id": target.process_id,
                        "task_id": target.active_task_id,
                        "status": "cancelled", "error": reason,
                    }
                    target.lifecycle_finalized = True
                    cancelled_parked.append(target.process_id)
                else:
                    target.status = ProcessStatus.CANCELLING
                requested.append(target.process_id)
                self.notify_state_changed(target.process_id)
            elif target.status == ProcessStatus.CANCELLING:
                already_cancelling.append(target.process_id)
            else:
                terminal_before.append(target.process_id)

        if requested:
            from backend.core.history import HistoryManager
            HistoryManager.add_operation(
                "system", "agent_tree_cancel_requested", "pending",
                {"root_process_id": process_id, "process_ids": requested,
                 "reason": reason}, correlation_id=process_id,
            )

        deadline = time.time() + max(0.0, wait_timeout)
        if wait_timeout > 0:
            for target in targets:
                thread = self._threads.get(target.process_id)
                if thread is None or thread is threading.current_thread():
                    continue
                thread.join(timeout=max(0.0, deadline - time.time()))

        still_running = [
            target.process_id for target in targets
            if target.status in (
                ProcessStatus.RUNNING, ProcessStatus.WAITING, ProcessStatus.CANCELLING,
                ProcessStatus.AWAITING_USER,
                ProcessStatus.RESUME_AVAILABLE,
                ProcessStatus.RECOVERY_REVIEW_REQUIRED,
                ProcessStatus.RECOVERING,
            )
        ]
        return {
            "requested": bool(requested),
            "status": process.status.value,
            "process_ids": requested,
            "already_cancelling": already_cancelling,
            "cancelled_parked": cancelled_parked,
            "terminal_before_request": terminal_before,
            "still_running": still_running,
            "tree_terminal": not still_running,
        }

    def _subtree(self, process_id: str) -> list[AgentProcess]:
        root = self._processes.get(process_id)
        if root is None:
            return []
        ordered: list[AgentProcess] = []
        stack = [root]
        while stack:
            item = stack.pop()
            ordered.append(item)
            stack.extend(self.children_of(item.process_id))
        return ordered

    def _cancellation_closure(self, process_id: str) -> list[AgentProcess]:
        """Ownership descendants plus every DAG consumer of a cancelled node."""
        root = self._processes.get(process_id)
        if root is None:
            return []
        ordered: list[AgentProcess] = []
        seen: set[str] = set()
        stack = [root]
        while stack:
            item = stack.pop()
            if item.process_id in seen:
                continue
            seen.add(item.process_id)
            ordered.append(item)
            stack.extend(self.children_of(item.process_id))
            stack.extend(self.downstream_of(item.process_id))
        return ordered

    def reap(self) -> list[AgentProcess]:
        """回收孤儿进程——父进程已死但本身还在的进程。"""
        active_ids = {p.process_id for p in self._processes.values()
                      if p.status in (
                          ProcessStatus.RUNNING,
                          ProcessStatus.WAITING,
                          ProcessStatus.CANCELLING,
                          ProcessStatus.AWAITING_USER,
                          ProcessStatus.RESUME_AVAILABLE,
                          ProcessStatus.RECOVERY_REVIEW_REQUIRED,
                          ProcessStatus.RECOVERING,
                      )}
        orphans = []
        for p in self._processes.values():
            if (
                p.parent_id and p.parent_id not in active_ids
                and not bool(p.runtime_preferences.get("user_direct_continuation"))
                and p.status in (
                    ProcessStatus.RUNNING, ProcessStatus.WAITING,
                    ProcessStatus.CANCELLING, ProcessStatus.AWAITING_USER,
                    ProcessStatus.RESUME_AVAILABLE,
                    ProcessStatus.RECOVERY_REVIEW_REQUIRED,
                    ProcessStatus.RECOVERING,
                )
            ):
                orphans.append(p)
                self.kill(p.process_id, reason="parent_terminal")
                from backend.core.history import HistoryManager
                HistoryManager.add_operation(
                    "system", "orphan_cancel_requested", "pending",
                    {"process_id": p.process_id, "role": p.role,
                     "parent_id": p.parent_id},
                    correlation_id=p.process_id,
                )
        return orphans

    def get(self, process_id: str) -> AgentProcess | None:
        return self._processes.get(process_id)

    def get_session(self, session_id: str):
        return self._sessions.get(session_id)


# ── Durable Session Persistence (SQLite + CAS) ────────────────

class SessionStore:
    """Typed facade for the authoritative SQLite session store.

    Legacy JSONL/checkpoint files are imported once and then moved to a dated,
    read-only recovery archive.  New writes never go to both backends.
    """

    LEGACY_SESSIONS_DIR = ".gitgo/sessions"
    MAX_EVENTS_WITHOUT_CHECKPOINT = 500
    MAX_JSONL_LINES = MAX_EVENTS_WITHOUT_CHECKPOINT  # compatibility constant
    _METADATA_FIELDS = (
        "context_epoch", "model_context_limit", "context_memo", "host_ledger", "display_name", "worker_sequence",
        "provider_usage", "cache_telemetry", "context_inventory", "epoch_archive",
        "steering_sequence", "compiled_prompt_hash", "compiled_prompt_sections", "stable_prefix_hash",
        "last_cache_intent_key", "last_cache_context_epoch",
        "active_provider_route",
        "manual_compact_requested",
        "force_compact_requested", "context_abort_requested",
        "compaction_failure_count", "last_compaction_error",
        "pending_compaction_decision",
    )

    def __init__(
        self,
        workspace_path: str,
        *,
        storage=None,
        state_home: str | Path | None = None,
        migrate_legacy: bool = True,
    ):
        from backend.core.storage import StorageRuntime

        self._ws = Path(workspace_path).resolve()
        self._legacy_dir = self._ws / self.LEGACY_SESSIONS_DIR
        self._owns_storage = storage is None
        self._storage = storage or StorageRuntime(self._ws, state_home=state_home)
        if migrate_legacy:
            self._migrate_legacy_once()

    @staticmethod
    def _session_metadata(session: "AgentSession") -> dict:
        return {
            key: getattr(session, key)
            for key in SessionStore._METADATA_FIELDS
        }

    def save_checkpoint(self, process_id: str, session: "AgentSession") -> str:
        """Persist a compatibility checkpoint without process/task metadata."""
        return self._storage.save_agent_checkpoint({
            "process_id": process_id,
            "session_id": session.session_id,
            "checkpoint_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "messages": list(session.messages),
            "provider_state": dict(session.provider_state),
            "session_metadata": self._session_metadata(session),
        })

    def save_process_checkpoint(self, process: AgentProcess) -> str:
        """Persist process, task, messages and receipts in one SQLite commit."""
        session = process.session
        if session is None:
            raise ValueError("process has no session")
        child_ids, delegated_contracts, child_reviews = process.coordination_snapshot()
        mailbox_state = (
            process.mailbox.durable_snapshot()
            if process.mailbox is not None else None
        )
        budget_state = (
            process.task_budget.snapshot()
            if process.task_budget is not None else None
        )
        return self._storage.save_agent_checkpoint({
            "process_id": process.process_id,
            "session_id": session.session_id,
            "checkpoint_at": datetime.now(timezone.utc).isoformat(),
            "status": process.status.value,
            "messages": list(session.messages),
            "provider_state": dict(session.provider_state),
            "session_metadata": self._session_metadata(session),
            "task_id": process.active_task_id,
            "task_description": process.task_description,
            "task_kind": process.task_kind,
            "required_test_ids": list(process.required_test_ids),
            "role": process.role,
            "actor_kind": process.actor_kind,
            "capability_profile_id": process.capability_profile_id,
            "parent_id": process.parent_id,
            "depends_on": list(getattr(process, "depends_on", []) or []),
            "steps_used": process.steps_used,
            "max_steps": process.max_steps,
            "created_at": process.created_at,
            "tool_receipts": list(process.tool_receipts),
            "result": process.result,
            "runtime_state": {
                "estimated_tokens": session.estimate_tokens(),
                "context": session.context_view(auto_compact=bool(
                    getattr(process, "runtime_preferences", {}).get("auto_compact", True)
                )),
                "cache_summary": session.cache_summary(),
                "pending_decision": process.pending_decision,
                # Checkpoint writers also accept legacy/recovered process-like
                # objects created before permission suspension existed.
                "pending_tool_batch": getattr(process, "pending_tool_batch", None),
                "approval_grants": list(getattr(process, "approval_grants", []) or []),
                "mailbox": mailbox_state,
                "task_budget": budget_state,
                "context_snapshot": process.context_snapshot,
                "context_version": process.context_version,
                "child_ids": child_ids,
                "delegated_contracts": delegated_contracts,
                "child_reviews": child_reviews,
                "completion_rejections": list(process.completion_rejections),
                "governance_resolutions": dict(
                    getattr(process, "governance_resolutions", {}) or {}
                ),
                "completion_claim": process.completion_claim,
                "review_claim": process.review_claim,
                "review_approvals": list(process.review_approvals),
                "review_required": bool(getattr(process, "review_required", False)),
                "review_target_id": getattr(process, "review_target_id", ""),
                "successful_actions": process.successful_actions,
                "task_constraints": list(process.task_constraints),
                "worktree_path": process.worktree_path,
                "workspace_root": getattr(process, "workspace_root", ""),
                "worktree": dict(getattr(process, "worktree", {}) or {}),
                "provider_id": process.provider_id,
                "model_id": process.model_id,
                "runtime_preferences": dict(
                    getattr(process, "runtime_preferences", {}) or {}
                ),
                "ring_level": getattr(
                    getattr(process, "ring_level", None), "value", 0,
                ),
                "child_sequence": getattr(process, "_child_sequence", 0),
                "cancel_requested": bool(getattr(process, "cancel_requested", False)),
                "cancellation_reason": getattr(process, "cancellation_reason", ""),
                "dynamic_tool_specs": {
                    name: dict(tool.composite_spec or {})
                    for name, tool in getattr(process, "dynamic_tools", {}).items()
                    if getattr(tool, "composite_spec", None)
                },
                "recovery": getattr(process, "recovery", None),
            },
        })

    def checkpoint_before_user_turn(
        self, process: AgentProcess, *, turn_id: str, turn_preview: str,
    ) -> dict:
        """Retain the exact provider-valid state before one canonical user turn."""
        self.save_process_checkpoint(process)
        state = self.load_process_state(process.process_id)
        if state is None:
            raise RuntimeError("SESSION_UNDO_CHECKPOINT_SOURCE_MISSING")
        state = dict(state)
        state["current_process_id"] = process.process_id
        state["turn_preview"] = str(turn_preview or "")[:500]
        child_ids, _contracts, _reviews = process.coordination_snapshot()
        state["effects_before_turn"] = {
            "receipt_count": len(process.tool_receipts),
            "child_process_ids": list(child_ids),
            "worktree_state": str((process.worktree or {}).get("state") or ""),
        }
        return self._storage.create_session_lineage_checkpoint(
            state,
            session_id=process.session.session_id,
            process_id=process.process_id,
            turn_id=turn_id,
        )

    def preview_undo(self, process_id: str) -> dict:
        state = self.load_process_state(process_id)
        if state is None:
            raise ValueError(f"PROCESS_NOT_FOUND:{process_id}")
        session_id = str((state.get("session") or {}).get("session_id") or "")
        return self._storage.preview_session_undo(session_id)

    def undo(self, process_id: str, checkpoint_id: str) -> tuple[dict, "AgentSession", dict]:
        """Restore only the conversation branch; never claim side-effect rollback."""
        state = self.load_process_state(process_id)
        if state is None:
            raise ValueError(f"PROCESS_NOT_FOUND:{process_id}")
        session_id = str((state.get("session") or {}).get("session_id") or "")
        committed = self._storage.commit_session_undo(session_id, checkpoint_id)
        target = dict(committed.pop("snapshot"))
        from backend.core.loop.session import AgentSession
        restored = AgentSession.from_durable_state(dict(target.get("session") or {}))
        # The latest root remains the continuation anchor.  Reusing the old
        # checkpoint's process row would resurrect a terminal execution object
        # and make daemon recovery confuse a history rewind with task resume.
        target_process_id = process_id
        target_process_state = state
        self.save_restored_session_checkpoint(
            target_process_id, restored, target_process_state,
        )
        return {**committed, "process_id": target_process_id}, restored, target

    def append_event(
        self, process_id: str, event_type: str, data: dict | None = None,
    ) -> None:
        self._storage.append_session_event(process_id, event_type, data or {})

    def load_session(self, process_id: str) -> list[dict] | None:
        state = self.load_session_state(process_id)
        if state is None:
            return None
        messages = list(state.get("messages", []))
        return messages if messages else None

    def load_session_state(self, process_id: str) -> dict | None:
        return self._storage.load_agent_session_state(process_id)

    def load_process_state(self, process_id: str) -> dict | None:
        return self._storage.load_agent_process_state(process_id)

    def deleted_process_ids(self) -> set[str]:
        return self._storage.read_deleted_process_ids()

    def load_latest_root_process_state(self) -> dict | None:
        return self._storage.load_latest_root_process_state()

    def save_restored_session_checkpoint(
        self, process_id: str, session: "AgentSession", state: dict,
    ) -> str:
        """Replace only a restored session payload while preserving task truth."""
        raw = dict(state.get("process") or {})
        task = dict(state.get("task") or {})
        contract = dict(task.get("contract") or {})
        return self._storage.save_agent_checkpoint({
            "process_id": process_id,
            "session_id": session.session_id,
            "checkpoint_at": datetime.now(timezone.utc).isoformat(),
            "status": str(raw.get("status") or "completed"),
            "messages": list(session.messages),
            "provider_state": dict(session.provider_state),
            "session_metadata": self._session_metadata(session),
            "task_id": str(raw.get("task_id") or task.get("task_id") or ""),
            "task_description": str(contract.get("task_description") or ""),
            "task_kind": str(contract.get("task_kind") or "answer"),
            "required_test_ids": list(contract.get("required_test_ids") or []),
            "role": str(raw.get("role") or "supervisor"),
            "actor_kind": str(raw.get("actor_kind") or "supervisor"),
            "capability_profile_id": str(raw.get("capability_profile_id") or "supervisor.control"),
            "parent_id": raw.get("parent_process_id"),
            "depends_on": [
                str(item.get("depends_on_process_id"))
                for item in (state.get("dependencies") or [])
                if item.get("depends_on_process_id")
            ],
            "steps_used": int(raw.get("steps_used", 0) or 0),
            "max_steps": int(raw.get("max_steps", 0) or 0),
            "created_at": str(raw.get("created_at") or ""),
            "tool_receipts": list(state.get("receipts") or []),
            "result": task.get("outcome"),
            "runtime_state": dict(state.get("runtime_state") or {}),
        })

    def load_worktree_state(self, process_id: str) -> dict | None:
        return self._storage.load_worktree_state(process_id)

    def list_worktree_states(self, *, active_only: bool = False) -> list[dict]:
        return self._storage.list_worktree_states(active_only=active_only)

    def delete_session(self, process_id: str) -> None:
        self._storage.delete_process_session(process_id)

    def list_incomplete(self) -> list[str]:
        return self._storage.list_incomplete_processes()

    def list_process_links(self) -> list[dict]:
        return self._storage.list_agent_process_links()

    def should_checkpoint(self, process_id: str) -> bool:
        return (
            self._storage.session_events_since_checkpoint(process_id)
            >= self.MAX_EVENTS_WITHOUT_CHECKPOINT
        )

    def storage_counts(self) -> dict[str, int]:
        return self._storage.session_storage_counts()

    @property
    def storage_paths(self):
        return self._storage.paths

    def close(self) -> None:
        if self._owns_storage:
            self._storage.close()

    def _legacy_process_ids(self) -> list[str]:
        if not self._legacy_dir.is_dir():
            return []
        ids: set[str] = set()
        for entry in self._legacy_dir.iterdir():
            if entry.name.endswith(".jsonl"):
                ids.add(entry.name[:-6])
            elif entry.name.endswith(".checkpoint.json"):
                ids.add(entry.name[:-16])
        return sorted(ids)

    def _read_legacy_events(self, process_id: str) -> list[dict]:
        path = self._legacy_dir / f"{process_id}.jsonl"
        events: list[dict] = []
        if not path.exists():
            return events
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("event"):
                events.append(entry)
        return events

    def _read_legacy_checkpoint(self, process_id: str) -> dict | None:
        path = self._legacy_dir / f"{process_id}.checkpoint.json"
        if not path.exists():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return dict(loaded) if isinstance(loaded, dict) else None

    @staticmethod
    def _replay_legacy_state(checkpoint: dict | None, events: list[dict]) -> dict | None:
        checkpoint = dict(checkpoint or {})
        messages = list(checkpoint.get("messages") or [])
        for entry in events:
            event_type = str(entry.get("event", ""))
            data = dict(entry.get("data") or {})
            if event_type == "message_append" and isinstance(data.get("message"), dict):
                messages.append(dict(data["message"]))
            elif event_type == "message_pop":
                count = max(0, int(data.get("count", 1) or 1))
                if count:
                    del messages[max(0, len(messages) - count):]
        provider_state = dict(checkpoint.get("provider_state") or {})
        if not messages and not provider_state:
            return None
        return {
            "session_id": str(checkpoint.get("session_id") or ""),
            "messages": messages,
            "provider_state": provider_state,
            "session_metadata": {
                key: checkpoint.get(key)
                for key in SessionStore._METADATA_FIELDS
                if key in checkpoint
            },
            "checkpoint_at": str(checkpoint.get("checkpoint_at") or ""),
        }

    def _migrate_legacy_once(self) -> None:
        marker = self._storage.get_state_ref("migration", "session_jsonl_v1")
        if marker:
            return
        process_ids = self._legacy_process_ids()
        for process_id in process_ids:
            events = self._read_legacy_events(process_id)
            for sequence, entry in enumerate(events, start=1):
                canonical = json.dumps(
                    entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                import_id = "legacy:" + hashlib.sha256(
                    f"{process_id}:{sequence}:{canonical}".encode("utf-8")
                ).hexdigest()
                self._storage.append_session_event(
                    process_id,
                    str(entry.get("event")),
                    dict(entry.get("data") or {}),
                    occurred_at=str(entry.get("ts") or "") or None,
                    sequence=sequence,
                    event_id=import_id,
                )
            state = self._replay_legacy_state(
                self._read_legacy_checkpoint(process_id), events
            )
            if state is not None:
                session_id = state["session_id"] or f"legacy-session:{process_id}"
                self._storage.save_agent_checkpoint({
                    "process_id": process_id,
                    "session_id": session_id,
                    "checkpoint_at": state["checkpoint_at"] or datetime.now().isoformat(),
                    "status": "imported",
                    "messages": state["messages"],
                    "provider_state": state["provider_state"],
                    "session_metadata": state["session_metadata"],
                })

        if process_ids and self._legacy_dir.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            archive = self._legacy_dir.with_name(f"sessions.legacy-imported-{stamp}")
            suffix = 0
            while archive.exists():
                suffix += 1
                archive = self._legacy_dir.with_name(
                    f"sessions.legacy-imported-{stamp}-{suffix}"
                )
            os.replace(self._legacy_dir, archive)
        self._storage.put_state_ref(
            "migration", "session_jsonl_v1", "completed:" + datetime.now().isoformat()
        )
