"""Shared hard budgets for one supervisor task tree.

The budget object is created for the root A process and shared by every
delegated B/reviewer process.  Counts are monotonic for the lifetime of the
task; a failed child does not refund provider calls or generated output.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass


class TaskBudgetExceeded(RuntimeError):
    """Raised before a task tree can consume work beyond an admitted limit."""

    def __init__(self, code: str, message: str, *, timed_out: bool = False):
        super().__init__(message)
        self.code = code
        self.timed_out = timed_out


@dataclass(frozen=True)
class TaskTreeBudgetLimits:
    max_agents: int = 8
    max_provider_calls: int = 100
    max_output_tokens: int = 131_072
    max_seconds: float = 300.0
    initial_seconds: float = 0.0  # zero keeps legacy fixed-deadline semantics
    verification_reserve_ratio: float = 0.15
    default_child_share: float = 0.20

    @classmethod
    def from_mapping(cls, raw: dict | None) -> "TaskTreeBudgetLimits":
        values = raw or {}
        return cls(
            max_agents=max(1, min(int(values.get("max_agents", 8)), 64)),
            max_provider_calls=max(
                1, min(int(values.get("max_provider_calls", 100)), 10_000)
            ),
            max_output_tokens=max(
                256, min(int(values.get("max_output_tokens", 131_072)), 10_000_000)
            ),
            max_seconds=max(
                1.0, min(float(values.get("max_seconds", 300.0)), 86_400.0)
            ),
            initial_seconds=max(0.0, min(float(values.get("initial_seconds", 0.0)), 86_400.0)),
            verification_reserve_ratio=max(
                0.05, min(float(values.get("verification_reserve_ratio", 0.15)), 0.40)
            ),
            default_child_share=max(
                0.05, min(float(values.get("default_child_share", 0.20)), 0.75)
            ),
        )


class TaskTreeBudget:
    """Thread-safe, task-tree-scoped budget ledger."""

    def __init__(self, task_id: str, limits: TaskTreeBudgetLimits):
        self.task_id = task_id
        self.limits = limits
        self.started_monotonic = time.monotonic()
        initial = min(limits.initial_seconds or limits.max_seconds, limits.max_seconds)
        self.deadline_monotonic = self.started_monotonic + initial
        self.hard_deadline_monotonic = self.started_monotonic + limits.max_seconds
        self._deadline_version = 0
        self._extensions: list[dict] = []
        self._lock = threading.Lock()
        self._agents = 0
        self._provider_calls = 0
        self._output_tokens = 0
        self._reported_input_tokens = 0
        self._reported_output_tokens = 0
        self._reported_cache_read_tokens = 0
        self._reported_cache_write_tokens = 0
        self._provider_calls_by_purpose = {"work": 0, "verification": 0}
        self._output_tokens_by_purpose = {"work": 0, "verification": 0}
        # Streaming adapters can emit a token as several tiny character
        # fragments.  Estimating each fragment independently turns four ASCII
        # characters into four tokens instead of one.  Keep only the running
        # ASCII length per live provider channel and charge the delta of the
        # cumulative estimate.
        self._output_fragment_ascii: dict[str, int] = {}
        # Child allocations are real escrows inside the tree's vector limits.
        # A root cannot spend capacity promised to a live B, and a B cannot
        # exceed its own allocation.  Terminal children refund only unused
        # capacity; already consumed calls/tokens remain monotonic.
        self._process_leases: dict[str, dict] = {}
        self._paused_at: float | None = None
        self._paused_seconds = 0.0

    @classmethod
    def create(cls, task_id: str, raw_limits: dict | None = None) -> "TaskTreeBudget":
        budget = cls(task_id, TaskTreeBudgetLimits.from_mapping(raw_limits))
        budget.reserve_agent()
        return budget

    @classmethod
    def from_snapshot(cls, snapshot: dict | None) -> "TaskTreeBudget | None":
        """Restore consumed budget while excluding daemon downtime."""
        raw = dict(snapshot or {})
        if not raw:
            return None
        limits = TaskTreeBudgetLimits.from_mapping(dict(raw.get("limits") or {}))
        budget = cls(str(raw.get("task_id") or "recovered-task"), limits)
        used = dict(raw.get("used") or {})
        now = time.monotonic()
        elapsed = max(0.0, float(used.get("elapsed_seconds", 0.0) or 0.0))
        budget.started_monotonic = now - elapsed
        budget.deadline_monotonic = now + max(0.0, limits.max_seconds - elapsed)
        budget.hard_deadline_monotonic = budget.deadline_monotonic
        deadline = dict(raw.get("deadline") or {})
        allowance = float(deadline.get("allowance_seconds", limits.max_seconds))
        budget.deadline_monotonic = now + max(0.0, min(limits.max_seconds, allowance) - elapsed)
        budget._deadline_version = int(deadline.get("version", 0))
        budget._agents = max(0, int(used.get("agents", 0) or 0))
        budget._provider_calls = max(0, int(used.get("provider_calls", 0) or 0))
        budget._output_tokens = max(
            0, int(used.get("estimated_output_tokens", 0) or 0)
        )
        budget._reported_input_tokens = max(
            0, int(used.get("reported_input_tokens", 0) or 0)
        )
        budget._reported_output_tokens = max(
            0, int(used.get("reported_output_tokens", 0) or 0)
        )
        budget._reported_cache_read_tokens = max(
            0, int(used.get("reported_cache_read_tokens", 0) or 0)
        )
        budget._reported_cache_write_tokens = max(
            0, int(used.get("reported_cache_write_tokens", 0) or 0)
        )
        by_purpose = dict(used.get("by_purpose") or {})
        budget._provider_calls_by_purpose = {
            "work": max(0, int(dict(by_purpose.get("provider_calls") or {}).get("work", budget._provider_calls) or 0)),
            "verification": max(0, int(dict(by_purpose.get("provider_calls") or {}).get("verification", 0) or 0)),
        }
        budget._output_tokens_by_purpose = {
            "work": max(0, int(dict(by_purpose.get("estimated_output_tokens") or {}).get("work", budget._output_tokens) or 0)),
            "verification": max(0, int(dict(by_purpose.get("estimated_output_tokens") or {}).get("verification", 0) or 0)),
        }
        budget._process_leases = {
            str(process_id): dict(lease or {})
            for process_id, lease in dict(raw.get("process_leases") or {}).items()
            if str(process_id)
        }
        if bool(used.get("awaiting_user", False)):
            budget._paused_at = now
        return budget

    def check_deadline(self) -> None:
        with self._lock:
            self._check_deadline_locked()

    def reserve_time(self, seconds: float, *, reason: str) -> float:
        """Host-only bounded operation allowance; never increases the admitted hard cap.

        Called for a provider/tool phase or actual provider output, not timers or
        model assertions. Token/call/agent budgets remain independent hard gates.
        """
        with self._lock:
            now = time.monotonic()
            if self.limits.initial_seconds and now < self.hard_deadline_monotonic:
                target = min(self.hard_deadline_monotonic, now + max(0.0, seconds))
                if target > self.deadline_monotonic:
                    self.deadline_monotonic = target
                    self._deadline_version += 1
                    self._extensions.append({"event": "deadline_extended",
                        "version": self._deadline_version, "reason": reason,
                        "remaining_seconds": round(target - now, 1),
                        "hard_remaining_seconds": round(self.hard_deadline_monotonic - now, 1)})
            self._check_deadline_locked()
            return max(0.0, self.deadline_monotonic - now)

    def drain_extensions(self) -> list[dict]:
        with self._lock:
            events, self._extensions = self._extensions, []
            return events

    def hard_remaining_seconds(self) -> float:
        with self._lock:
            now = self._paused_at if self._paused_at is not None else time.monotonic()
            return max(0.0, self.hard_deadline_monotonic - now)

    def reserve_agent(self) -> None:
        with self._lock:
            self._check_deadline_locked()
            if self._agents >= self.limits.max_agents:
                raise TaskBudgetExceeded(
                    "TASK_TREE_AGENT_BUDGET_EXHAUSTED",
                    f"task tree agent limit {self.limits.max_agents} exhausted",
                )
            self._agents += 1

    def cancel_agent_reservation(self) -> None:
        """Undo an admission slot when construction fails before publication."""
        with self._lock:
            self._agents = max(0, self._agents - 1)

    def register_process(
        self,
        process_id: str,
        *,
        parent_id: str | None,
        actor_kind: str,
        max_steps: int,
        request: dict | None = None,
    ) -> dict:
        """Bind one admitted process to a task-tree budget account.

        Root A keeps the unescrowed work pool.  Every B/reviewer receives a
        bounded allocation that is returned when it becomes terminal.  This is
        resource accounting, not a semantic routing classifier.
        """
        request = dict(request or {})
        with self._lock:
            if process_id in self._process_leases:
                return dict(self._process_leases[process_id])
            purpose = "verification" if actor_kind == "reviewer" else "work"
            if parent_id is None:
                lease = {
                    "process_id": process_id,
                    "parent_id": "",
                    "purpose": purpose,
                    "kind": "root",
                    "state": "active",
                    "reserved_provider_calls": 0,
                    "reserved_output_tokens": 0,
                    "reserved_steps": max(1, int(max_steps)),
                    "used_provider_calls": 0,
                    "used_output_tokens": 0,
                    "refunded_provider_calls": 0,
                    "refunded_output_tokens": 0,
                }
                self._process_leases[process_id] = lease
                return dict(lease)

            # The default is a share of this process purpose's pool, rather
            # than a share of the whole tree.  In particular, a reviewer must
            # fit inside the protected verification reserve by default.
            provider_default = max(
                1,
                math.ceil(
                    self._purpose_ceiling_locked("provider_calls", purpose)
                    * self.limits.default_child_share
                ),
            )
            output_default = max(
                1,
                math.ceil(
                    self._purpose_ceiling_locked("output_tokens", purpose)
                    * self.limits.default_child_share
                ),
            )
            # A reviewer commonly needs to inspect several files, execute one
            # verification command, recover from one bounded tool/provider
            # failure, and still submit the structured verdict.  Six calls
            # proved sufficient only for tiny reviews: a three-file real E2E
            # reached the sixth call before ``complete_review`` and made A
            # spawn replacement reviewers.  Keep one production review large
            # enough to finish, but clip it to the protected verification pool
            # so a failed review cannot consume work capacity or loop forever.
            if purpose == "verification":
                provider_default = min(
                    self._purpose_ceiling_locked("provider_calls", purpose),
                    max(10, provider_default),
                )
                output_default = min(
                    self._purpose_ceiling_locked("output_tokens", purpose),
                    max(16_384, output_default),
                )
            requested_calls = max(1, int(request.get("provider_calls", provider_default) or provider_default))
            requested_output = max(1, int(request.get("output_tokens", output_default) or output_default))
            available_calls = self._available_for_new_lease_locked("provider_calls", purpose)
            available_output = self._available_for_new_lease_locked("output_tokens", purpose)
            if requested_calls > available_calls or requested_output > available_output:
                raise TaskBudgetExceeded(
                    "TASK_TREE_CHILD_ESCROW_UNAVAILABLE",
                    "requested child allocation exceeds the uncommitted task-tree budget",
                )
            lease = {
                "process_id": process_id,
                "parent_id": str(parent_id),
                "purpose": purpose,
                "kind": "child",
                "state": "active",
                "reserved_provider_calls": requested_calls,
                "reserved_output_tokens": requested_output,
                "reserved_steps": max(1, int(request.get("steps", max_steps) or max_steps)),
                "used_provider_calls": 0,
                "used_output_tokens": 0,
                "refunded_provider_calls": 0,
                "refunded_output_tokens": 0,
            }
            self._process_leases[process_id] = lease
            return dict(lease)

    def release_process(self, process_id: str, *, status: str = "terminal") -> dict | None:
        with self._lock:
            lease = self._process_leases.get(process_id)
            if lease is None or lease.get("state") != "active":
                return dict(lease) if lease else None
            lease["state"] = "released"
            lease["terminal_status"] = str(status)
            lease["refunded_provider_calls"] = max(
                0, int(lease.get("reserved_provider_calls", 0))
                - int(lease.get("used_provider_calls", 0))
            )
            lease["refunded_output_tokens"] = max(
                0, int(lease.get("reserved_output_tokens", 0))
                - int(lease.get("used_output_tokens", 0))
            )
            return dict(lease)

    def _purpose_ceiling_locked(self, resource: str, purpose: str) -> int:
        maximum = (
            self.limits.max_provider_calls
            if resource == "provider_calls" else self.limits.max_output_tokens
        )
        verification = max(1, math.ceil(maximum * self.limits.verification_reserve_ratio))
        return verification if purpose == "verification" else max(1, maximum - verification)

    def _outstanding_locked(self, resource: str, purpose: str, *, exclude: str = "") -> int:
        reserved_key = f"reserved_{resource}"
        used_key = f"used_{resource}"
        return sum(
            max(0, int(lease.get(reserved_key, 0)) - int(lease.get(used_key, 0)))
            for process_id, lease in self._process_leases.items()
            if process_id != exclude
            and lease.get("kind") == "child"
            and lease.get("state") == "active"
            and lease.get("purpose") == purpose
        )

    def _available_for_new_lease_locked(self, resource: str, purpose: str) -> int:
        used = (
            self._provider_calls_by_purpose[purpose]
            if resource == "provider_calls" else self._output_tokens_by_purpose[purpose]
        )
        return max(
            0,
            self._purpose_ceiling_locked(resource, purpose)
            - used - self._outstanding_locked(resource, purpose),
        )

    def begin_provider_call(self, process_id: str = "") -> int:
        self.reserve_time(360, reason="provider_phase")
        with self._lock:
            self._check_deadline_locked()
            if self._provider_calls >= self.limits.max_provider_calls:
                raise TaskBudgetExceeded(
                    "TASK_TREE_PROVIDER_BUDGET_EXHAUSTED",
                    "task tree provider-call limit "
                    f"{self.limits.max_provider_calls} exhausted",
                )
            lease = self._process_leases.get(process_id)
            purpose = str((lease or {}).get("purpose") or "work")
            if lease and lease.get("kind") == "child":
                if lease.get("state") != "active" or int(lease.get("used_provider_calls", 0)) >= int(lease.get("reserved_provider_calls", 0)):
                    raise TaskBudgetExceeded(
                        "TASK_TREE_CHILD_PROVIDER_ESCROW_EXHAUSTED",
                        "child provider-call allocation exhausted; return to A for an evidence-based continuation decision",
                    )
            ceiling = self._purpose_ceiling_locked("provider_calls", purpose)
            outstanding = self._outstanding_locked("provider_calls", purpose, exclude=process_id)
            if self._provider_calls_by_purpose[purpose] + 1 + outstanding > ceiling:
                raise TaskBudgetExceeded(
                    "TASK_TREE_VERIFICATION_RESERVE_PROTECTED" if purpose == "work" else "TASK_TREE_VERIFICATION_BUDGET_EXHAUSTED",
                    f"task tree {purpose} provider-call allocation exhausted",
                )
            self._provider_calls += 1
            self._provider_calls_by_purpose[purpose] += 1
            if lease:
                lease["used_provider_calls"] = int(lease.get("used_provider_calls", 0)) + 1
            return self._provider_calls

    def consume_output(self, text: str, process_id: str = "") -> None:
        estimate = estimate_output_tokens(text)
        if estimate <= 0:
            return
        self.consume_output_estimate(estimate, process_id)

    def consume_output_fragment(
        self, text: str, process_id: str = "", *, channel: str,
    ) -> None:
        """Charge a streamed fragment as part of one cumulative channel."""
        if not text:
            return
        non_ascii = sum(1 for char in text if ord(char) > 127)
        ascii_chars = len(text) - non_ascii
        key = f"{process_id}:{channel}"
        with self._lock:
            previous = self._output_fragment_ascii.get(key, 0)
            current = previous + ascii_chars
            self._output_fragment_ascii[key] = current
            estimate = non_ascii + math.ceil(current / 4) - math.ceil(previous / 4)
        if estimate > 0:
            self.consume_output_estimate(estimate, process_id)

    def finish_output_fragments(self, process_id: str, *, prefix: str) -> None:
        """Release transient fragment counters after one provider call."""
        key_prefix = f"{process_id}:{prefix}"
        with self._lock:
            for key in [item for item in self._output_fragment_ascii if item.startswith(key_prefix)]:
                self._output_fragment_ascii.pop(key, None)

    def consume_output_estimate(self, estimate: int, process_id: str = "") -> None:
        estimate = max(0, int(estimate))
        if estimate <= 0:
            return
        if self.remaining_seconds() < 30:
            self.reserve_time(300, reason="provider_output_progress")
        with self._lock:
            self._check_deadline_locked()
            if self._output_tokens + estimate > self.limits.max_output_tokens:
                raise TaskBudgetExceeded(
                    "TASK_TREE_OUTPUT_BUDGET_EXHAUSTED",
                    "task tree estimated output-token limit "
                    f"{self.limits.max_output_tokens} exhausted",
                )
            lease = self._process_leases.get(process_id)
            purpose = str((lease or {}).get("purpose") or "work")
            if lease and lease.get("kind") == "child":
                if lease.get("state") != "active" or int(lease.get("used_output_tokens", 0)) + estimate > int(lease.get("reserved_output_tokens", 0)):
                    raise TaskBudgetExceeded(
                        "TASK_TREE_CHILD_OUTPUT_ESCROW_EXHAUSTED",
                        "child output allocation exhausted; return to A for an evidence-based continuation decision",
                    )
            ceiling = self._purpose_ceiling_locked("output_tokens", purpose)
            outstanding = self._outstanding_locked("output_tokens", purpose, exclude=process_id)
            if self._output_tokens_by_purpose[purpose] + estimate + outstanding > ceiling:
                raise TaskBudgetExceeded(
                    "TASK_TREE_VERIFICATION_RESERVE_PROTECTED" if purpose == "work" else "TASK_TREE_VERIFICATION_BUDGET_EXHAUSTED",
                    f"task tree {purpose} output allocation exhausted",
                )
            self._output_tokens += estimate
            self._output_tokens_by_purpose[purpose] += estimate
            if lease:
                lease["used_output_tokens"] = int(lease.get("used_output_tokens", 0)) + estimate

    def remaining_seconds(self) -> float:
        with self._lock:
            now = self._paused_at if self._paused_at is not None else time.monotonic()
            return max(0.0, self.deadline_monotonic - now)

    def pause_for_user(self) -> None:
        """Stop charging wall-clock budget while the task awaits human input."""
        with self._lock:
            self._check_deadline_locked()
            if self._paused_at is None:
                self._paused_at = time.monotonic()

    def resume_from_user(self) -> None:
        with self._lock:
            if self._paused_at is None:
                return
            paused = max(0.0, time.monotonic() - self._paused_at)
            self._paused_seconds += paused
            self.deadline_monotonic += paused
            self.hard_deadline_monotonic += paused
            self._paused_at = None

    def record_provider_usage(self, usage: dict) -> None:
        """Record exact provider billing telemetry without double-charging estimates."""
        with self._lock:
            self._reported_input_tokens += int(usage.get("input_tokens", 0) or 0)
            self._reported_output_tokens += int(usage.get("output_tokens", 0) or 0)
            self._reported_cache_read_tokens += int(
                usage.get("cache_read_tokens", 0) or 0
            )
            self._reported_cache_write_tokens += int(
                usage.get("cache_write_tokens", 0) or 0
            )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "task_id": self.task_id,
                "limits": {
                    "max_agents": self.limits.max_agents,
                    "max_provider_calls": self.limits.max_provider_calls,
                    "max_output_tokens": self.limits.max_output_tokens,
                    "max_seconds": self.limits.max_seconds,
                    "initial_seconds": self.limits.initial_seconds,
                    "verification_reserve_ratio": self.limits.verification_reserve_ratio,
                    "default_child_share": self.limits.default_child_share,
                },
                "deadline": {"version": self._deadline_version,
                    "allowance_seconds": max(0.0, self.deadline_monotonic - self.started_monotonic - self._paused_seconds)},
                "used": {
                    "agents": self._agents,
                    "provider_calls": self._provider_calls,
                    "estimated_output_tokens": self._output_tokens,
                    "reported_input_tokens": self._reported_input_tokens,
                    "reported_output_tokens": self._reported_output_tokens,
                    "reported_cache_read_tokens": self._reported_cache_read_tokens,
                    "reported_cache_write_tokens": self._reported_cache_write_tokens,
                    "elapsed_seconds": max(0.0, (
                        (self._paused_at if self._paused_at is not None else time.monotonic())
                        - self.started_monotonic - self._paused_seconds
                    )),
                    "awaiting_user": self._paused_at is not None,
                    "by_purpose": {
                        "provider_calls": dict(self._provider_calls_by_purpose),
                        "estimated_output_tokens": dict(self._output_tokens_by_purpose),
                    },
                },
                "process_leases": {
                    process_id: dict(lease)
                    for process_id, lease in self._process_leases.items()
                },
            }

    def decision_card(self, process_id: str = "") -> dict:
        """Small, deterministic routing card for decision boundaries only."""
        with self._lock:
            call_reserve = max(1, math.ceil(
                self.limits.max_provider_calls * self.limits.verification_reserve_ratio
            ))
            output_reserve = max(1, math.ceil(
                self.limits.max_output_tokens * self.limits.verification_reserve_ratio
            ))
            active_call_escrow = sum(
                max(0, int(item.get("reserved_provider_calls", 0))
                    - int(item.get("used_provider_calls", 0)))
                for item in self._process_leases.values()
                if item.get("kind") == "child" and item.get("state") == "active"
            )
            active_output_escrow = sum(
                max(0, int(item.get("reserved_output_tokens", 0))
                    - int(item.get("used_output_tokens", 0)))
                for item in self._process_leases.values()
                if item.get("kind") == "child" and item.get("state") == "active"
            )
            pressure = max(
                self._agents / max(1, self.limits.max_agents),
                (self._provider_calls + active_call_escrow)
                / max(1, self.limits.max_provider_calls),
                (self._output_tokens + active_output_escrow)
                / max(1, self.limits.max_output_tokens),
            )
            state = "critical" if pressure >= 0.90 else (
                "constrained" if pressure >= 0.70 else "normal"
            )
            return {
                "state": state,
                "agents": {
                    "used": self._agents,
                    "maximum": self.limits.max_agents,
                    "remaining": max(0, self.limits.max_agents - self._agents),
                },
                "provider_calls": {
                    "used": self._provider_calls,
                    "maximum": self.limits.max_provider_calls,
                    "verification_reserve": call_reserve,
                    "active_escrow": active_call_escrow,
                    "remaining_uncommitted": max(
                        0, self.limits.max_provider_calls
                        - self._provider_calls - active_call_escrow,
                    ),
                },
                "output_tokens": {
                    "estimated_used": self._output_tokens,
                    "maximum": self.limits.max_output_tokens,
                    "verification_reserve": output_reserve,
                    "active_escrow": active_output_escrow,
                    "remaining_uncommitted": max(
                        0, self.limits.max_output_tokens
                        - self._output_tokens - active_output_escrow,
                    ),
                },
                "next_child_default_escrow": {
                    "provider_calls": max(1, math.ceil(
                        (self.limits.max_provider_calls - call_reserve)
                        * self.limits.default_child_share
                    )),
                    "output_tokens": max(1, math.ceil(
                        (self.limits.max_output_tokens - output_reserve)
                        * self.limits.default_child_share
                    )),
                },
                "current_process": dict(self._process_leases.get(process_id) or {}),
                "policy": (
                    "Delegation has setup and coordination cost. Use B when its marginal "
                    "value comes from genuine parallel work, specialist context, independent "
                    "review, or a workflow that has grown beyond A. Routine persistence/testing "
                    "of work A already derived is not a separate workstream. Reuse the existing "
                    "owner for iterative changes. Unused budget is not penalized; "
                    "finish whenever the completion evidence is sufficient."
                ),
            }

    def _check_deadline_locked(self) -> None:
        if self._paused_at is None and time.monotonic() >= self.deadline_monotonic:
            raise TaskBudgetExceeded(
                "TASK_TREE_DEADLINE_EXCEEDED",
                f"task tree exceeded {self.limits.max_seconds:.1f}s deadline",
                timed_out=True,
            )


def estimate_output_tokens(text: str) -> int:
    """Conservative provider-neutral estimate until adapters expose usage.

    Non-ASCII characters are counted individually; ASCII is estimated at four
    characters per token.  Exact provider usage replaces this estimate in the
    protocol-adapter phase, but this bound is enforceable today.
    """

    if not text:
        return 0
    non_ascii = sum(1 for char in text if ord(char) > 127)
    ascii_chars = len(text) - non_ascii
    return non_ascii + math.ceil(ascii_chars / 4)
