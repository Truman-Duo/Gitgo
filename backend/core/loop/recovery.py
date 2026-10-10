"""Deterministic cross-daemon Agent runtime recovery.

Recovery never replays an effectful tool automatically.  The Host rebuilds the
durable process tree, classifies post-checkpoint invocation evidence, revokes
ephemeral self-execution leases, and waits for an explicit user action.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from datetime import datetime
from pathlib import Path

from backend.core.loop.budget import TaskTreeBudget
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.completion_protocol import CompletionClaim
from backend.core.loop.mailbox import AgentMailbox
from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry
from backend.core.storage import StorageReferenceMissing


_TERMINAL = {
    "completed", "failed", "cancelled", "timed_out", "killed", "orphaned",
}


def _timestamp(value: str) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Historical checkpoints were written with datetime.now().isoformat(),
        # so a naive value denotes host-local wall time. Treating it as UTC
        # creates an eight-hour false-negative window on an Asia/Shanghai host.
        # datetime.timestamp() deliberately applies the host timezone for such
        # legacy values; new checkpoints are timezone-aware UTC.
        return parsed.timestamp()
    except (TypeError, ValueError):
        return 0.0


def inspect_post_checkpoint_invocations(
    workspace: str | Path, process_id: str, checkpoint_at: str, *, paths=None, state_home=None,
) -> list[dict]:
    """Find effectful invocation truth written after the durable checkpoint."""
    from backend.core.storage.invocation_journal import invocation_journal_root
    checkpoint_ts = _timestamp(checkpoint_at)
    findings: list[dict] = []

    def unavailable(journal, detail):
        return {
            "code": "INVOCATION_EVIDENCE_UNAVAILABLE", "detail": detail,
            "tool_name": "unknown", "state": "unknown", "effect_state": "ambiguous",
            "execution_id": "", "journal": str(journal),
            "owner_scope": "host", "updated_at": checkpoint_ts,
        }

    try:
        root = invocation_journal_root(workspace, paths=paths, state_home=state_home)
    except (OSError, RuntimeError, ValueError) as exc:
        return [unavailable("", str(exc))]
    legacy = Path(workspace) / ".gitgo" / "tool_invocations"
    # Workspace records have no authenticity proof. Preserve them for review,
    # but never import a tool-writable "committed" claim as Host truth.
    if legacy.exists() or legacy.is_symlink():
        findings.append({
            "code": "LEGACY_INVOCATION_JOURNAL_UNTRUSTED",
            "tool_name": "unknown", "state": "unknown", "effect_state": "ambiguous",
            "execution_id": "", "journal": str(legacy),
            "owner_scope": "untrusted_workspace", "updated_at": checkpoint_ts,
        })
    try:
        with os.scandir(root) as entries:
            records = [Path(entry.path) for entry in entries if entry.name.endswith(".json")]
    except FileNotFoundError:
        records = []  # A project without effectful invocations has no journal yet.
    except OSError as exc:
        return findings + [unavailable(root, str(exc))]
    for path in records:
        try:
            if path.resolve(strict=True).parent != root:
                raise ValueError("Invocation record is redirected outside its Host journal")
            item = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(item, dict):
                raise ValueError("Invocation record must be an object")
            updated_at = float(item.get("updated_at", 0.0) or 0.0)
            if not math.isfinite(updated_at):
                raise ValueError("Invocation timestamp is not finite")
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            findings.append(unavailable(path, str(exc)))
            continue
        owner = str(item.get("process_id") or "")
        if owner and owner != process_id:
            continue
        if updated_at <= checkpoint_ts:
            continue
        effect = str(item.get("effect") or "external")
        from backend.core.loop.operation_policy import is_observation_effect
        if is_observation_effect(effect):
            continue
        effect_state = str(item.get("effect_state") or "ambiguous")
        state = str(item.get("state") or "unknown")
        if state == "committed" and effect_state == "committed":
            code = "EFFECT_COMMITTED_AFTER_CHECKPOINT"
        else:
            code = "EFFECT_STATE_AMBIGUOUS_AFTER_CHECKPOINT"
        findings.append({
            "code": code,
            "tool_name": str(item.get("tool_name") or "unknown"),
            "state": state,
            "effect_state": effect_state,
            "execution_id": str(item.get("execution_id") or ""),
            "journal": str(path),
            "owner_scope": "process" if owner else "legacy_unscoped",
            "updated_at": updated_at,
        })
    return sorted(findings, key=lambda item: item["updated_at"])


def inspect_worktree_recovery(record: dict | None) -> list[dict]:
    """Refuse resume when a durable isolated checkout cannot be verified."""
    if not record or not record.get("isolated"):
        return []
    state = str(record.get("state") or "")
    path = Path(str(record.get("path") or ""))
    if state in {"failed", "privacy_blocked", "disposed", "creating"}:
        return [{"code": "WORKTREE_NOT_RESUMABLE", "state": state}]
    if not path.is_dir():
        return [{"code": "WORKTREE_PATH_MISSING", "path": str(path)}]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=path,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [{"code": "WORKTREE_GIT_UNAVAILABLE", "path": str(path)}]
    if result.returncode != 0:
        return [{"code": "WORKTREE_INVALID", "path": str(path)}]
    expected = str(record.get("result_commit") or "") if state == "sealed" else ""
    if expected and result.stdout.strip() != expected:
        return [{"code": "WORKTREE_HEAD_DRIFT", "path": str(path)}]
    return []


def _connected_process_ids(incomplete: set[str], links: list[dict]) -> set[str]:
    parents = {
        str(item.get("process_id")): str(item.get("parent_process_id") or "")
        for item in links
    }
    connected = set(incomplete)
    changed = True
    while changed:
        changed = False
        for process_id, parent_id in parents.items():
            if process_id in connected and parent_id and parent_id not in connected:
                connected.add(parent_id)
                changed = True
            if parent_id in connected and process_id not in connected:
                connected.add(process_id)
                changed = True
    return connected


def _depth(process_id: str, states: dict[str, dict]) -> int:
    depth = 0
    seen = set()
    current = process_id
    while current in states and current not in seen:
        seen.add(current)
        parent = str(states[current]["process"].get("parent_process_id") or "")
        if not parent:
            break
        depth += 1
        current = parent
    return depth


def _root_id(process_id: str, states: dict[str, dict]) -> str:
    current = process_id
    seen = set()
    while current in states and current not in seen:
        seen.add(current)
        parent = str(states[current]["process"].get("parent_process_id") or "")
        if not parent or parent not in states:
            return current
        current = parent
    return process_id


def restore_incomplete_processes(session_store, manager, workspace: str | Path,
                                *, include_process_ids: list[str] | None = None) -> list[dict]:
    """Rebuild incomplete trees and return structured user-facing candidates."""
    deleted_ids = session_store.deleted_process_ids()
    incomplete = set(session_store.list_incomplete()) - deleted_ids
    selected = set(include_process_ids or [])
    if not incomplete and not selected:
        return []
    links = session_store.list_process_links()
    related_ids = _connected_process_ids(incomplete | selected, links)
    states: dict[str, dict] = {}
    state_errors: dict[str, StorageReferenceMissing] = {}
    for process_id in related_ids:
        try:
            state = session_store.load_process_state(process_id)
        except StorageReferenceMissing as exc:
            state_errors[process_id] = exc
            continue
        if state is not None:
            states[process_id] = state
    # Same-session root successors may own children whose immutable parent_id
    # still points at an older root. Follow explicit coordination references,
    # not every historic session, and never infer execution authority from text.
    pending = list(states)
    while pending:
        current = states[pending.pop()]
        for child_id in (current.get("runtime_state") or {}).get("child_ids", []):
            if child_id in states or child_id in deleted_ids:
                continue
            try:
                child_state = session_store.load_process_state(child_id)
            except StorageReferenceMissing as exc:
                state_errors[child_id] = exc
                incomplete.add(child_id)
                continue
            if child_state is None:
                raise ValueError(f"CONTINUATION_STATE_UNAVAILABLE: child checkpoint missing: {child_id}")
            states[child_id] = child_state
            pending.append(child_id)

    # A terminal checkpoint with a missing barrier is complete data, not a task
    # to replay. Repair only the deterministic barrier and remove it from the
    # explicit recovery set.
    for process_id in list(incomplete):
        state = states.get(process_id)
        status = str((state or {}).get("process", {}).get("status") or "")
        if status in _TERMINAL:
            session_store.append_event(process_id, "agent_complete", {
                "status": status,
                "steps_used": int((state or {}).get("process", {}).get("steps_used", 0) or 0),
                "repaired_terminal_barrier": True,
            })
            incomplete.discard(process_id)
    if not incomplete and not selected:
        return []

    budget_by_root: dict[str, TaskTreeBudget | None] = {}
    # An event may survive while the first process checkpoint transaction never
    # committed. Do not silently omit that incomplete id and do not invent
    # execution authority. Rebuild a discard-only diagnostic process so the
    # user can see and resolve the durable orphan explicitly.
    for process_id in sorted(incomplete - set(states)):
        storage_error = state_errors.get(process_id)
        try:
            session_state = session_store.load_session_state(process_id) or {}
        except StorageReferenceMissing as exc:
            storage_error = storage_error or exc
            session_state = {}
        process = AgentProcess(
            process_id=process_id,
            role="recovery-diagnostic",
            ring_level=RingLevel.RING_3,
            tool_registry=ToolRegistry([]),
            max_steps=1,
            status=ProcessStatus.RECOVERY_REVIEW_REQUIRED,
            actor_kind="worker",
            capability_profile_id="text.only",
            task_kind="answer",
            session=AgentSession.from_durable_state(session_state),
            mailbox=AgentMailbox(),
            runtime_preferences={},
        )
        reason = (
            {
                "code": StorageReferenceMissing.code,
                "detail": str(storage_error),
                "ref": storage_error.ref,
            }
            if storage_error else {
                "code": "PROCESS_CHECKPOINT_MISSING",
                "detail": "Session events exist but no process checkpoint committed.",
            }
        )
        process.recovery = {
            "state": ProcessStatus.RECOVERY_REVIEW_REQUIRED.value,
            "checkpoint_at": "",
            "reasons": [reason],
            "self_execute_lease_revoked": True,
            "dynamic_tools_revoked": [],
            "requires_manual_verification": True,
            "resume_forbidden": True,
        }
        manager.register_restored(process)

    for process_id in sorted(states, key=lambda item: _depth(item, states)):
        if manager.get(process_id) is not None:
            continue
        state = states[process_id]
        raw_process = dict(state.get("process") or {})
        runtime = dict(state.get("runtime_state") or {})
        runtime["child_ids"] = [pid for pid in runtime.get("child_ids", []) if pid not in deleted_ids]
        # Keep required contracts: an explicitly deleted outcome cannot satisfy
        # completion. Only remove runnable ownership/revival references.
        session_state = dict(state.get("session") or {})
        contract = dict((state.get("task") or {}).get("contract") or {})
        actor_kind = str(raw_process.get("actor_kind") or "worker")
        profile_id = str(
            raw_process.get("capability_profile_id")
            or contract.get("capability_profile_id")
            or "text.only"
        )
        profile_error = ""
        try:
            profile = CapabilityProfiles.get(profile_id)
            if profile.actor_kind.value != actor_kind and not (
                actor_kind == "supervisor" and profile_id == "supervisor.control"
            ):
                raise ValueError("actor/profile mismatch")
            tool_registry = ToolRegistry(CapabilityProfiles.resolve_tools(profile_id))
        except ValueError as exc:
            profile_error = str(exc)
            tool_registry = ToolRegistry([])

        session = AgentSession.from_durable_state(session_state)
        root_id = _root_id(process_id, states)
        if root_id not in budget_by_root:
            root_runtime = dict(states[root_id].get("runtime_state") or {})
            budget_by_root[root_id] = TaskTreeBudget.from_snapshot(
                root_runtime.get("task_budget") or runtime.get("task_budget")
            )
        budget = budget_by_root[root_id]
        persisted_status = str(raw_process.get("status") or "running")
        pending_decision = runtime.get("pending_decision")
        checkpoint_at = str(
            (state.get("session_checkpoint") or {}).get("checkpoint_at") or ""
        )
        durable_worktree = (
            session_store.load_worktree_state(process_id)
            or dict(runtime.get("worktree") or {})
        )
        process_workspace = str(
            durable_worktree.get("path") or runtime.get("worktree_path") or workspace
        )
        findings = inspect_post_checkpoint_invocations(
            process_workspace, process_id, checkpoint_at,
            paths=session_store._storage.paths,
        ) if process_id in incomplete else []
        if process_id in incomplete:
            findings.extend(inspect_worktree_recovery(durable_worktree))

        if process_id not in incomplete:
            try:
                restored_status = ProcessStatus(persisted_status)
            except ValueError:
                restored_status = ProcessStatus.FAILED
        elif pending_decision:
            restored_status = ProcessStatus.AWAITING_USER
        elif profile_error:
            # A missing/changed server-owned profile cannot be authorized by a
            # user attestation. Keep the process visible for discard and
            # diagnosis, but never turn it into a runnable text-only agent.
            restored_status = ProcessStatus.RECOVERY_REVIEW_REQUIRED
        elif findings:
            restored_status = ProcessStatus.RECOVERY_REVIEW_REQUIRED
        else:
            restored_status = ProcessStatus.RESUME_AVAILABLE

        try:
            ring = RingLevel(int(runtime.get("ring_level", 0 if actor_kind == "supervisor" else 3)))
        except ValueError:
            ring = RingLevel.RING_0 if actor_kind == "supervisor" else RingLevel.RING_3
        completion_claim = CompletionClaim.from_dict(runtime.get("completion_claim"))
        restored_context = dict(runtime.get("context_snapshot") or {})
        if isinstance(restored_context.get("signals"), list):
            from backend.core.loop.signals import normalize_governance_signals
            restored_context["signals"] = normalize_governance_signals(
                restored_context["signals"]
            )
        process = AgentProcess(
            process_id=process_id,
            role=str(raw_process.get("role") or actor_kind),
            ring_level=ring,
            tool_registry=tool_registry,
            max_steps=max(1, int(raw_process.get("max_steps", 50) or 50)),
            steps_used=max(0, int(raw_process.get("steps_used", 0) or 0)),
            status=restored_status,
            parent_id=str(raw_process.get("parent_process_id") or "") or None,
            depends_on=[
                str(item.get("depends_on_process_id"))
                for item in (state.get("dependencies") or [])
                if str(item.get("depends_on_process_id") or "")
            ] or list(runtime.get("depends_on") or []),
            result=(state.get("task") or {}).get("outcome"),
            context_snapshot=restored_context,
            task_description=str(contract.get("task_description") or ""),
            active_task_id=str(raw_process.get("task_id") or ""),
            actor_kind=actor_kind,
            capability_profile_id=profile_id,
            # A self-execution lease is deliberately not restored. The model
            # must explicitly request a new task-scoped lease after recovery.
            capability_lease=None,
            task_kind=str(contract.get("task_kind") or "answer"),
            required_test_ids=list(contract.get("required_test_ids") or []),
            completion_claim=completion_claim,
            successful_actions=max(0, int(runtime.get("successful_actions", 0) or 0)),
            tool_receipts=list(state.get("receipts") or []),
            child_ids=list(runtime.get("child_ids") or []),
            delegated_contracts=dict(runtime.get("delegated_contracts") or {}),
            child_reviews=dict(runtime.get("child_reviews") or {}),
            completion_rejections=list(runtime.get("completion_rejections") or []),
            governance_resolutions=dict(runtime.get("governance_resolutions") or {}),
            review_target_id=str(runtime.get("review_target_id") or ""),
            review_claim=runtime.get("review_claim"),
            review_approvals=list(runtime.get("review_approvals") or []),
            review_required=bool(runtime.get("review_required", False)),
            task_constraints=list(runtime.get("task_constraints") or []),
            created_at=str(raw_process.get("created_at") or ""),
            worktree_path=process_workspace,
            workspace_root=str(runtime.get("workspace_root") or workspace),
            worktree=dict(durable_worktree or {}),
            provider_id=str(runtime.get("provider_id") or ""),
            model_id=str(runtime.get("model_id") or ""),
            runtime_preferences=dict(runtime.get("runtime_preferences") or {}),
            session=session,
            mailbox=AgentMailbox.from_durable_snapshot(runtime.get("mailbox")),
            task_budget=budget,
            context_version=max(1, int(runtime.get("context_version", 1) or 1)),
            pending_decision=(dict(pending_decision) if isinstance(pending_decision, dict) else None),
            pending_tool_batch=(
                dict(runtime.get("pending_tool_batch"))
                if isinstance(runtime.get("pending_tool_batch"), dict) else None
            ),
            approval_grants=list(runtime.get("approval_grants") or []),
        )
        process._child_sequence = max(
            int(runtime.get("child_sequence", 0) or 0), len(process.child_ids)
        )
        process.estimated_tokens = session.estimate_tokens()
        if process_id in incomplete:
            reasons = []
            if profile_error:
                reasons.append({"code": "CAPABILITY_PROFILE_UNAVAILABLE", "detail": profile_error})
            reasons.extend(findings)
            worktree_forbidden = any(
                str(item.get("code") or "").startswith("WORKTREE_")
                for item in findings
            )
            evidence_unavailable = any(
                item.get("code") == "INVOCATION_EVIDENCE_UNAVAILABLE" for item in findings
            )
            process.recovery = {
                "state": restored_status.value,
                "checkpoint_at": checkpoint_at,
                "reasons": reasons,
                "self_execute_lease_revoked": actor_kind == "supervisor",
                "dynamic_tools_revoked": sorted(
                    dict(runtime.get("dynamic_tool_specs") or {}).keys()
                ),
                "requires_manual_verification": bool(findings or profile_error),
                "resume_forbidden": bool(profile_error or worktree_forbidden or evidence_unavailable),
            }
        manager.register_restored(process)

    # The child row is authoritative evidence that delegation existed even if
    # the daemon died before the parent checkpoint captured child_ids. Rebuild
    # that edge deterministically so A cannot complete while silently ignoring
    # an already-admitted B process.
    for process_id, state in states.items():
        child = manager.get(process_id)
        if child is None or not child.parent_id:
            continue
        parent = manager.get(child.parent_id)
        if parent is None:
            continue
        if child.process_id not in parent.child_ids:
            parent.child_ids.append(child.process_id)
        if child.process_id not in parent.delegated_contracts:
            recovered_contract = dict(
                (state.get("task") or {}).get("contract") or {}
            )
            recovered_contract.setdefault("task_id", child.active_task_id)
            recovered_contract.setdefault("role", child.role)
            recovered_contract.setdefault("actor_kind", child.actor_kind)
            parent.delegated_contracts[child.process_id] = recovered_contract

    candidates = []
    for process_id in sorted(incomplete):
        process = manager.get(process_id)
        if process is None:
            continue
        candidates.append({
            "process_id": process_id,
            "task_id": process.active_task_id,
            "session_id": process.session.session_id,
            "role": process.role,
            "status": process.status.value,
            "parent_id": process.parent_id,
            **dict(process.recovery or {}),
        })
    return candidates


def recovery_instruction(process: AgentProcess, user_note: str, *, verified: bool) -> str:
    attestation = (
        "The user explicitly attested that the workspace was manually inspected "
        "and chose to continue despite post-checkpoint side-effect evidence."
        if verified else
        "The Host found no effectful invocation evidence newer than the checkpoint."
    )
    revoked = " Any previous A self-execution lease and dynamic tool definitions were revoked."
    note = f"\nUser recovery note: {user_note.strip()}" if user_note.strip() else ""
    return (
        "[HOST RECOVERY CHECKPOINT]\n"
        + attestation
        + revoked
        + " Do not assume an unfinished tool call succeeded or failed, and do not "
          "repeat it automatically. Inspect current project state before any new "
          "effectful operation. Continue the same task from durable evidence."
        + note
    )
