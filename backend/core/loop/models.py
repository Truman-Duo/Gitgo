"""Agent Process data model."""

from __future__ import annotations
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.session import AgentSession


class ProcessStatus(Enum):
    RUNNING = "running"
    WAITING = "waiting"
    AWAITING_USER = "awaiting_user"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    KILLED = "killed"
    ORPHANED = "orphaned"
    RESUME_AVAILABLE = "resume_available"
    RECOVERY_REVIEW_REQUIRED = "recovery_review_required"
    RECOVERING = "recovering"


class RingLevel(Enum):
    RING_0 = 0  # 治理权限：sync/push/accept/promote_lesson
    RING_3 = 3  # 执行权限：只能调本进程 tool_registry 里的工具


@dataclass
class AgentProcess:
    process_id: str          # UUID
    role: str                # "planner" | "executor" | "reviewer" | "reporter"
    ring_level: RingLevel
    tool_registry: "ToolRegistry | None" = None
    max_steps: int = 50
    steps_used: int = 0               # ToolDispatcher 每步 +1
    status: ProcessStatus = ProcessStatus.RUNNING
    cancel_requested: bool = False      # kill() 置位，agent_step 每步检查以真停线程
    cancellation_event: Any = field(default_factory=threading.Event, repr=False)
    cancellation_reason: str = ""
    parent_id: str | None = None      # 谁 fork 的
    depends_on: list[str] = field(default_factory=list)
        # DAG execution edges. Ownership remains parent_id; dependencies may
        # point only to tasks owned by the same supervisor/root task tree.
    result: dict | None = None        # wait 之后的产出
    context_snapshot: dict | None = None  # C3: A 级 Agent 冻结的治理简报
    task_description: str = ""            # 当前 task 描述
    active_task_id: str = ""              # 当前提交 turn 的稳定 correlation id
    actor_kind: str = "worker"            # supervisor | worker | reviewer
    capability_profile_id: str = "text.only"
    capability_lease: Any = field(default=None, repr=False)
    task_kind: str = "answer"              # answer | action | plan | review
    required_test_ids: list[str] = field(default_factory=list)
    completion_claim: Any = field(default=None, repr=False)
    successful_actions: int = 0
    tool_receipts: list[dict] = field(default_factory=list, repr=False)
    dynamic_tools: dict[str, Any] = field(default_factory=dict, repr=False)
    child_ids: list[str] = field(default_factory=list, repr=False)
    delegated_contracts: dict[str, dict] = field(default_factory=dict, repr=False)
    child_reviews: dict[str, dict] = field(default_factory=dict, repr=False)
    completion_rejections: list[dict] = field(default_factory=list, repr=False)
    governance_resolutions: dict[str, dict] = field(default_factory=dict, repr=False)
    review_target_id: str = ""
    review_claim: Any = field(default=None, repr=False)
    review_approvals: list[str] = field(default_factory=list, repr=False)
    review_required: bool = False
    task_constraints: list[str] = field(default_factory=list)  # v0.36: 中途约束
    created_at: str = ""
    worktree_path: str = ""            # 挂载的工作区路径（B 级 Agent）
    workspace_root: str = ""            # 用户工作仓；与 Agent worktree 分离
    worktree: dict = field(default_factory=dict, repr=False)
        # Host-owned isolation lease/snapshot metadata. Models never infer
        # isolation merely from worktree_path being non-empty.
    provider_id: str = ""              # LLM provider id
    model_id: str = ""                 # LLM model id
    runtime_preferences: dict[str, Any] = field(default_factory=dict, repr=False)
    session: Any = field(default=None, repr=False)   # AgentSession, 避免循环 import
    mailbox: Any = field(default=None, repr=False)   # AgentMailbox, 进程内控制消息
    task_budget: Any = field(default=None, repr=False)
    budget_lease: dict[str, Any] = field(default_factory=dict, repr=False)
    context_version: int = 1
    _context_lock: Any = field(default_factory=threading.RLock, repr=False)
    _coordination_lock: Any = field(default_factory=threading.RLock, repr=False)
    _child_sequence: int = field(default=0, repr=False)
    _run_lock: Any = field(default_factory=threading.Lock, repr=False)
    _step_history: list[dict] = field(default_factory=list, repr=False)
    _nudge_counters: dict[str, int] = field(default_factory=dict, repr=False)
    _transcript_builder: Any = field(default=None, repr=False)  # v0.39: TaskTranscriptBuilder
    _dependency_observed_files: set[str] = field(default_factory=set, repr=False)
    pending_decision: dict | None = field(default=None, repr=False)
    # Provider function calls suspended before execution while the Host waits
    # for an exact user permission. The batch is resumed by the Host; the
    # model is never asked to reconstruct or retry the call.
    pending_tool_batch: dict | None = field(default=None, repr=False)
    approval_grants: list[dict] = field(default_factory=list, repr=False)
    recovery: dict | None = field(default=None, repr=False)
    lifecycle_finalized: bool = False
        # Set after Host post-processing (for example worktree sealing). A
        # model-visible COMPLETED status alone is not a scheduler barrier.

    def read_context_snapshot(self) -> tuple[dict, int]:
        """Read one coherent governance snapshot and its monotonic version."""
        with self._context_lock:
            context = dict(self.context_snapshot or {})
            if isinstance(context.get("signals"), list):
                context["signals"] = list(context["signals"])
            return context, self.context_version

    def replace_context_snapshot(self, context: dict | None) -> int:
        """Atomically publish a new governance snapshot for safe turn refresh."""
        with self._context_lock:
            self.context_snapshot = dict(context or {})
            self.context_version += 1
            return self.context_version

    def update_context_snapshot(self, transform) -> int:
        """Atomically update one domain without overwriting concurrent fields."""
        with self._context_lock:
            current = dict(self.context_snapshot or {})
            updated = transform(current)
            if updated is not None and updated != current:
                self.context_snapshot = dict(updated)
                self.context_version += 1
            return self.context_version

    def allocate_child_task_id(self) -> str:
        """Allocate a collision-free, readable child task id.

        Tool calls in one provider turn may execute concurrently, so deriving an
        id from ``len(child_ids)`` creates a real race.  The sequence belongs to
        the parent process and is protected by the same coordination lock used
        for child contracts.
        """
        with self._coordination_lock:
            self._child_sequence += 1
            root = self.active_task_id or self.process_id
            return f"{root}:{self._child_sequence}"

    def register_child_contract(
        self, process_id: str, contract: dict, *, supersedes_process_id: str = "",
    ) -> None:
        """Atomically publish a delegated child and its host-owned contract."""
        with self._coordination_lock:
            if process_id not in self.child_ids:
                self.child_ids.append(process_id)
            self.delegated_contracts[process_id] = dict(contract)
            if supersedes_process_id:
                previous = self.delegated_contracts.get(supersedes_process_id)
                if previous is not None:
                    previous["superseded_by"] = process_id

    def coordination_snapshot(self) -> tuple[list[str], dict[str, dict], dict[str, dict]]:
        """Return one coherent snapshot for completion and Dashboard projections."""
        with self._coordination_lock:
            return (
                list(self.child_ids),
                {key: dict(value) for key, value in self.delegated_contracts.items()},
                {key: dict(value) for key, value in self.child_reviews.items()},
            )
