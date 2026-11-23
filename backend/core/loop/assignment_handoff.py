"""Return unfinished isolated workspace work to its governing supervisor."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import uuid

from .continuation_contract import _tool_requirements, _union
from .lifecycle import execution_closeout, is_consumable_terminal
from .task_contract import get_task_contract, semantic_contract
from .worktree import WorktreeError, WorktreeLease


class AssignmentHandoffError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details


def contract_digest(process) -> str:
    """Bind requirements and issued scope, excluding live delegation bookkeeping."""
    contract = get_task_contract(process)
    for key in ("delegation_attempts", "agent_required_delegation", "minimum_delegated_outcomes"):
        contract.pop(key, None)
    basis = [process.process_id, process.active_task_id, process.task_kind, process.task_constraints,
             process.required_test_ids, contract]
    return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def subagent_scope_digest(process) -> str:
    """Bind a temporary result to requirements, independent of plan bookkeeping."""
    contract = get_task_contract(process)
    basis = [process.process_id, process.active_task_id, process.task_constraints,
             process.required_test_ids, semantic_contract(contract)]
    if process.parent_id:
        basis.extend((contract.get("target_files"), contract.get("allowed_tools")))
    return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def inspect_assignment(manager, owner, event: dict, *, closeout: bool, versioned: bool) -> tuple:
    """Bind the current assignment before any snapshot or ownership change."""
    child_id = event.get("source_process_id", "")
    child = manager.get(child_id)
    contract = owner.delegated_contracts.get(child_id)
    if (owner.actor_kind != "supervisor" or owner.task_kind != "supervisor" or owner.parent_id
            or not manager.owns_child(owner, child) or child.parent_id != owner.process_id
            or getattr(child, "role", None) == "subagent"
            or not contract or contract.get("superseded_by") or contract.get("returned_assignment")
            or event.get("source_task_id") != (child.active_task_id or child_id)):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_STALE", "The event must identify the current direct assignment.")
    if (owner.cancellation_event.is_set() or (closeout and (
            child.status.value not in {"cancelled", "killed", "failed", "timed_out", "completed"}
            or not child.lifecycle_finalized or not is_consumable_terminal(child, manager)
            or (child.result or {}).get("code") == "CHILD_SANDBOX_CLEANUP_FAILED"
            or (child.budget_lease or {}).get("state") == "active"))):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_CLOSEOUT_REQUIRED",
            "Wait for the stopped worker's finalized closeout and budget release.")

    child_contract = get_task_contract(child)
    if event.get("kind") not in {"coordination_request", "blocked"}:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_OFFER_KIND_INVALID",
                                     "Offer or return a draft on a coordination_request or blocked event.")
    if ((not versioned and event.get("affected_process_ids"))
            or (event.get("details") or {}).get("requires_user_decision")
            or child.task_kind != "action" or child.capability_profile_id != "development.workspace"
            or child.depends_on or (not versioned and manager.downstream_of(child_id)) or child.child_ids
            or child_contract.get("engineering_workflow") or child_contract.get("mechanism")
            or any(contract.get(key) or child_contract.get(key) for key in
                   ("input_interfaces", "required_workbench_claims"))
            or (not versioned and (contract.get("output_interfaces") or child_contract.get("output_interfaces")))):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_SCOPE_UNSUPPORTED",
            "This handoff supports one independent workspace assignment with concrete file targets.")
    targets = _union(contract.get("target_files"), child_contract.get("target_files"))
    if not targets or any("*" in path for path in targets):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_SCOPE_UNSUPPORTED", "Inspect the assignment's concrete file targets.")

    pending = list(child.tool_receipts)
    while pending:
        receipt = pending.pop()
        if receipt.get("effect_state") == "ambiguous" or (
                receipt.get("effect") == "external" and receipt.get("committed") is True):
            raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_EFFECT_REVIEW_REQUIRED",
                "Reconcile the previous execution's external or uncertain effect before changing its owner.")
        pending.extend(receipt.get("child_receipts") or [])
    worktree = child.worktree or {}
    if manager.worktree_manager is None or not worktree.get("isolated") or not worktree.get("worktree_id"):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DRAFT_UNAVAILABLE",
            "The original isolated checkout is required to preserve its draft.")
    if worktree.get("promoted"):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_SCOPE_UNSUPPORTED",
            "The worker's published output remains in the owner workspace. Use the existing task contract to add any new work.")
    required_tests = _union(contract.get("required_test_ids"), child.required_test_ids)
    required_tools = _tool_requirements(contract.get("required_tool_calls"), child_contract.get("required_tool_calls"))
    if any(requirement.get("max_calls") is not None for requirement in required_tools):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_SCOPE_UNSUPPORTED",
            "A cross-execution tool limit needs explicit reconciliation before transfer.")
    return child, contract, {
        "coordination_event_id": event["coordination_event_id"],
        "owner_process_id": owner.process_id,
        "owner_task_id": owner.active_task_id or owner.process_id,
        "source_task_id": child.active_task_id or child_id,
        "source_status": child.status.value,
        "task_description": child.task_description,
        "acceptance_criteria": _union(contract.get("acceptance_criteria"), child_contract.get("acceptance_criteria")),
        "constraints": deepcopy(child.task_constraints),
        "target_files": targets,
        "required_test_ids": required_tests,
        "required_tool_calls": required_tools,
        "output_interfaces": _union(contract.get("output_interfaces"), child_contract.get("output_interfaces")),
        "interface_contract": deepcopy(child_contract.get("interface_contract") or contract.get("interface_contract") or {}),
        "deliverables": deepcopy(child_contract.get("deliverables") or []),
        "owner_receipt_start": len(owner.tool_receipts),
    }


def inspect_return(manager, owner, event: dict) -> tuple:
    return inspect_assignment(manager, owner, event, closeout=True, versioned=bool(event.get("draft")))


def assignment_basis(owner, child, contract: dict) -> dict:
    fields = ("target_files", "required_test_ids", "required_tool_calls", "acceptance_criteria",
              "allowed_tools", "tool_scope_mode", "required_for_parent_completion",
              "input_interfaces", "output_interfaces", "interface_contract", "depends_on")
    issued = {field: contract.get(field) for field in fields}
    return {
        "owner_contract": contract_digest(owner),
        "source_contract": contract_digest(child),
        "issued_contract": hashlib.sha256(json.dumps(issued, sort_keys=True).encode()).hexdigest(),
        "owner_task_id": owner.active_task_id or owner.process_id,
        "base_commit": child.worktree.get("accepted_commit") or child.worktree.get("snapshot_commit"),
    }


def _selection_ref(prefix, *basis) -> str:
    encoded = json.dumps(basis, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return prefix + "-" + hashlib.sha256(encoded).hexdigest()[:24]


def _assignment_ref(owner, child, issued):
    return _selection_ref("assignment", child.process_id, assignment_basis(owner, child, issued),
                          get_task_contract(owner).get("revision", 0))


def resolve_withdrawal_selection(owner, proposed):
    """Resolve choices against the same issued scope projected to the supervisor."""
    code = "ASSIGNMENT_HANDOFF_SCOPE_CHANGE_INVALID"
    if "source_process_id" in proposed or "draft_id" in proposed:
        raise AssignmentHandoffError(code, "Use assignment_ref on its own to select the assignment.")
    manager = getattr(owner, "_manager", None)
    selected = []
    child_ids, contracts, _ = owner.coordination_snapshot()
    for child_id in child_ids[-16:]:
        child = manager.get(child_id) if manager else None
        issued = contracts.get(child_id)
        if child is None or issued is None or _assignment_ref(owner, child, issued) != proposed["assignment_ref"]:
            continue
        try:
            _, issued, assignment = inspect_assignment(manager, owner, assignment_event(child),
                                                       closeout=False, versioned=True)
        except AssignmentHandoffError:
            continue
        selected.append((child, assignment))
    if len(selected) != 1:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_VERSION_CHANGED",
            "Select the current assignment_ref from issued_scope for this user change.")
    child, assignment = selected[0]
    criteria = proposed.get("retire_criterion_numbers", [])
    if (not isinstance(criteria, list) or len(criteria) > 64
            or any(type(number) is not int or not 1 <= number <= len(assignment["acceptance_criteria"])
                   for number in criteria)):
        raise AssignmentHandoffError(code, "Choose criterion numbers from this issued assignment.")
    resolved = {key: value for key, value in proposed.items()
                if key not in {"assignment_ref", "retire_criterion_numbers"}}
    if "retire_criterion_numbers" in proposed:
        numbered = [assignment["acceptance_criteria"][number - 1] for number in criteria]
        explicit = resolved.get("retire_acceptance_criteria", [])
        if (not isinstance(explicit, list) or any(not isinstance(value, str) for value in explicit)
                or (explicit and numbered and set(explicit) != set(numbered))):
            raise AssignmentHandoffError(code, "The criterion numbers and exact values select different obligations.")
        resolved["retire_acceptance_criteria"] = numbered or explicit
    if draft := latest_draft(owner, child.process_id):
        resolved["draft_id"] = draft["id"]
    else:
        resolved["source_process_id"] = child.process_id
    return resolved


def latest_draft(owner, process_id: str) -> dict | None:
    task_id = owner.active_task_id or owner.process_id
    for entry in reversed(owner.session.host_ledger):
        draft = entry.get("draft")
        if (entry.get("event") == "agent_coordination_event" and draft
                and entry.get("owner_process_id") == owner.process_id
                and entry.get("source_process_id") == process_id
                and draft["basis"]["owner_task_id"] == task_id):
            return draft
    return None


def draft_event(owner, draft_id: str, process_id: str = "") -> dict:
    from .coordination import current_coordination_events
    event = next((item for item in current_coordination_events(owner.session.host_ledger)
                  if item.get("coordination_event_id") == draft_id and item.get("draft")), None)
    if (event is None or event["owner_process_id"] != owner.process_id
            or (process_id and process_id != event["source_process_id"])):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DRAFT_NOT_FOUND",
                                     "Select an explicit draft ID from the owned assignment.")
    return event


def recorded_scope_change(owner, process_id: str) -> dict | None:
    context, _ = owner.read_context_snapshot()
    state = context.get("task_maintenance") or {}
    if state.get("task_id") == (owner.active_task_id or owner.process_id):
        return (state.get("assignment_scope_changes") or {}).get(process_id)
    return None


def scope_owner_digest(owner, contract: dict) -> str:
    """Keep an adopted scope pinned across review-only contract revisions."""
    requirements = semantic_contract(contract)
    requirements.pop("maintenance_intent", None)
    basis = [owner.active_task_id or owner.process_id, owner.task_constraints, owner.required_test_ids, requirements]
    return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def scope_owner_matches(owner, scope: dict) -> bool:
    contract = get_task_contract(owner)
    if expected := scope.get("owner_scope_digest"):
        return expected == scope_owner_digest(owner, contract)
    return scope["contract_revision"] == contract.get("revision")


def withdrawal_source(owner, change: dict) -> dict | None:
    """Require the real ingress receipt for this task, independent of draft timing."""
    for row in reversed(owner.session.host_ledger):
        if (row.get("event") == "task_user_instruction_received"
                and row.get("process_id") == owner.process_id
                and row.get("task_id") == (owner.active_task_id or owner.process_id)
                and row.get("source_ref") == change.get("source_ref") and row.get("content") == change.get("content")
                and 0 < row.get("authority_sequence", 0) < change.get("authority_sequence", 0)):
            return row
    return None


def assignment_event(child) -> dict:
    return {"source_process_id": child.process_id, "source_task_id": child.active_task_id or child.process_id,
            "kind": "coordination_request", "coordination_event_id": ""}


def withdrawn_assignments(owner, revised_contract: dict) -> dict:
    """Find concrete parent deliverables removed from still-required owned work."""
    def required_files(contract):
        return {row["path"] for row in contract.get("deliverables", [])
                if row.get("kind") == "workspace_file" and row.get("required", True)}

    removed = required_files(get_task_contract(owner)) - required_files(revised_contract)
    manager = getattr(owner, "_manager", None)
    if not removed or manager is None or owner.parent_id:
        return {}
    affected = {}
    for child_id, issued in owner.delegated_contracts.items():
        if not issued.get("required_for_parent_completion", True):
            continue
        child = manager.get(child_id)
        if child is None:
            continue
        try:
            _, _, assignment = inspect_assignment(manager, owner, assignment_event(child), closeout=False, versioned=True)
        except AssignmentHandoffError:
            continue
        targets = set(assignment["target_files"])
        if targets & removed and targets - removed:
            affected[child_id] = sorted(targets & removed)
    return affected


def prepare_scope_change(owner, revised_contract: dict, change: dict, proposed: dict) -> dict:
    """Compile an exact withdrawal within the existing user-change revision."""
    code = "ASSIGNMENT_HANDOFF_SCOPE_CHANGE_INVALID"
    fields = {"retire_target_files": "target_files", "retire_acceptance_criteria": "acceptance_criteria",
              "retire_test_ids": "required_test_ids"}
    if isinstance(proposed, dict) and "assignment_ref" in proposed:
        proposed = resolve_withdrawal_selection(owner, proposed)
    if not isinstance(proposed, dict) or set(proposed) - {"draft_id", "source_process_id", "close_source", *fields}:
        raise AssignmentHandoffError(code, "Name the owned assignment and exact original obligations to retire.")
    close_source = proposed.get("close_source", False)
    if not isinstance(close_source, bool):
        raise AssignmentHandoffError(code, "close_source must be a boolean.")
    prior_revision = get_task_contract(owner).get("revision", 0)
    source = withdrawal_source(owner, change)
    if source is None or change.get("base_contract_revision") != prior_revision:
        raise AssignmentHandoffError(code, "Use a real user change received for the current contract revision.")
    manager = owner._manager
    source_id = str(proposed.get("source_process_id") or "")
    draft_id = str(proposed.get("draft_id") or "")
    if draft_id:
        event = draft_event(owner, draft_id, source_id)
        child, issued, assignment = validate_draft(manager, owner, event, closeout=False)
    else:
        child = manager.get(source_id) if source_id else None
        if child is None:
            raise AssignmentHandoffError(code, "Use source_process_id to register the withdrawal before a draft exists.")
        existing = latest_draft(owner, source_id)
        if existing:
            raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_VERSION_CHANGED",
                f"Review current draft v{existing['version']} ({existing['id']}) and include its draft_id.")
        closed = is_consumable_terminal(child, manager)
        child, issued, assignment = inspect_assignment(manager, owner, assignment_event(child),
                                                       closeout=closed, versioned=True)
        if (not closed and child.status.value not in {"running", "waiting"}) or not child.worktree.get("snapshot_commit"):
            raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DRAFT_UNAVAILABLE",
                "An unsealed withdrawal requires a live or finalized worker with its original isolated checkout.")
    retired, remaining = {}, {}
    for name, field in fields.items():
        values = proposed.get(name, [])
        if (not isinstance(values, list) or len(values) > 64
                or any(not isinstance(value, str) or value not in assignment[field] for value in values)):
            raise AssignmentHandoffError(code, f"{name} must use the exact issued value. Issued values include "
                + json.dumps(assignment[field][:16], ensure_ascii=False) + ".")
        retired[field] = list(dict.fromkeys(values))
        remaining[field] = [value for value in assignment[field] if value not in retired[field]]
    paths = set(retired["target_files"])
    if not paths or not remaining["target_files"]:
        raise AssignmentHandoffError(code, "Withdraw a concrete subset of files with work still remaining.")
    # A literal path reference is a reason to request explicit reconciliation.
    # It is never evidence that the criterion itself should be removed.
    affected_criteria = [
        criterion for criterion in remaining["acceptance_criteria"]
        if any(re.search(r"(?<![A-Za-z0-9_./\\-])" + re.escape(path) + r"(?![A-Za-z0-9_./\\-])", criterion)
               for path in paths)
        and criterion not in revised_contract.get("acceptance_criteria", [])
    ]
    if affected_criteria:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_CRITERIA_UNRESOLVED",
            "These issued criteria still reference withdrawn files. Select exact values in retire_acceptance_criteria, "
            "or explicitly retain an unchanged criterion in the revised supervisor acceptance. "
            + json.dumps(affected_criteria[:16], ensure_ascii=False))
    if paths.intersection(ref.rsplit(":", 1)[0] for ref in assignment["output_interfaces"]):
        raise AssignmentHandoffError(code, "Resolve the declared output interface separately before withdrawing its file.")
    if (paths.intersection(row["path"] for row in revised_contract.get("deliverables", [])
                           if row.get("kind") == "workspace_file" and row.get("required", True))
            or set(retired["acceptance_criteria"]).intersection(revised_contract.get("acceptance_criteria", []))
            or set(retired["required_test_ids"]).intersection(owner.required_test_ids)):
        raise AssignmentHandoffError(code, "A withdrawn obligation is still required by the supervisor's revised task.")
    remaining["deliverables"] = [row for row in assignment["deliverables"]
                                 if row.get("kind") != "workspace_file" or row.get("path") not in paths]
    return {"change_id": change["id"], "source_process_id": child.process_id,
            "source_ref": source["source_ref"], "draft_id": draft_id,
            **({"close_source": True} if close_source else {}),
            "owner_scope_digest": scope_owner_digest(owner, revised_contract),
            **({"pending_draft_basis": assignment_basis(owner, child, issued)} if not draft_id else {}),
            "contract_revision": revised_contract["revision"], "retired": retired, "remaining": remaining}


def check_pending_draft_basis(owner, child, issued, scope):
    current = assignment_basis(owner, child, issued)
    expected = scope.get("pending_draft_basis") or {}
    changed = [key for key, value in expected.items() if key != "owner_contract" and current.get(key) != value]
    if not scope_owner_matches(owner, scope):
        changed.append("owner_scope")
    if not expected or scope["draft_id"] or changed:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DRAFT_BINDING_CHANGED",
            "The adopted scope changed before binding the draft. Reconcile "
            + (", ".join(changed) or "the prior binding") + " with the current scope.")


def bind_pending_scope(owner, child, issued, draft):
    scope = recorded_scope_change(owner, child.process_id)
    if not scope:
        return None
    if scope.get("draft_id"):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_SCOPE_RETIRED",
            "This scope already pins a draft. Close out the original worker and reassign the retained files.")
    check_pending_draft_basis(owner, child, issued, scope)

    def bind(context):
        state = context["task_maintenance"]
        scopes = state["assignment_scope_changes"]
        record = {**scope, "draft_id": draft["id"]}
        record.pop("pending_draft_basis")
        return {**context, "task_maintenance": {**state, "assignment_scope_changes": {**scopes, child.process_id: record}}}

    owner.update_context_snapshot(bind)
    return scope["change_id"]


def handoff_status(owner, state: dict) -> list[dict]:
    if state.get("task_id") != (owner.active_task_id or owner.process_id):
        return []
    result = []
    manager = getattr(owner, "_manager", None)
    for child_id, scope in (state.get("assignment_scope_changes") or {}).items():
        successor = (owner.delegated_contracts.get(child_id) or {}).get("superseded_by")
        child = manager.get(child_id) if manager else None
        closeout = execution_closeout(child, manager)["closeout_state"] if child else None
        draft_id = scope.get("draft_id")
        if successor:
            phase, action = "reassigned", ""
        elif not scope_owner_matches(owner, scope):
            phase, action = "scope_changed", "Reconcile the effective withdrawal with the changed requirements before reassigning."
        elif child is None:
            phase, action = "source_unavailable", "Inspect the saved assignment and recover its original checkout."
        elif closeout == "failed" or (child.result or {}).get("code") == "CHILD_SANDBOX_CLEANUP_FAILED":
            phase, action = "closeout_blocked", "Inspect the durable closeout or cleanup failure before consuming the draft."
        elif closeout == "finalized" and (child.budget_lease or {}).get("state") != "active":
            if draft_id:
                phase, action = "ready_to_reassign", "Call delegate_task with this draft_id and a short description of the next work. The retained scope is already recorded."
            else:
                phase, action = "ready_to_preserve", "Call delegate_task with this supersedes_process_id and the next work. It preserves the closed checkout before reassignment."
        elif child.status.value == "cancelling" or child.cancellation_event.is_set():
            phase, action = "waiting_for_closeout", "Use wait_agents for this source_process_id until its cleanup and budget release finish."
        elif child.status.value in {"running", "waiting", "awaiting_user"}:
            phase = "waiting_for_closeout" if draft_id else "waiting_for_draft"
            action = ("If the user requested replacement and the current files should be retained, call cancel_agent for "
                      "this source_process_id, then wait for finalized closeout. The adopted withdrawal already "
                      "allows the runtime to preserve its checkout. Ask for a draft first if the worker still needs to transfer context.")
        else:
            phase, action = "waiting_for_closeout", "Wait for the original worker's finalized closeout before reassigning."
        result.append({"phase": phase, "source_process_id": child_id, "successor_process_id": successor,
                       "draft_id": draft_id, "retained_files": scope["remaining"]["target_files"],
                       "next_action": action,
                       **({"preservation_error": child.runtime_preferences["draft_preservation_error"]}
                          if child and not draft_id and child.runtime_preferences.get("draft_preservation_error") else {})})
    return result


def withdrawal_context(owner, state: dict, child_ids: list[str], contracts: dict) -> tuple[list[str], dict]:
    """Show exact issued scope while genuine user feedback awaits reconciliation."""
    if not child_ids or state.get("task_id") != (owner.active_task_id or owner.process_id):
        return [], {}
    pending = [change["id"] for change in state.get("changes", {}).values()
               if change.get("status") == "pending" and withdrawal_source(owner, change)]
    manager = getattr(owner, "_manager", None)
    if not pending or manager is None:
        return [], {}
    issued = {}
    for child_id in reversed(child_ids):
        if len(issued) >= 4:
            break
        contract = contracts.get(child_id) or {}
        child = manager.get(child_id)
        if child is None or not contract.get("required_for_parent_completion", True):
            continue
        try:
            _, issued_contract, assignment = inspect_assignment(manager, owner, assignment_event(child),
                                                                closeout=False, versioned=True)
        except AssignmentHandoffError:
            continue
        draft = latest_draft(owner, child_id)
        issued[child_id] = {
            "assignment_ref": _assignment_ref(owner, child, issued_contract),
            "target_files": assignment["target_files"][:16],
            "acceptance_criteria": assignment["acceptance_criteria"][:12],
            "required_test_ids": assignment["required_test_ids"][:12],
            **({"draft_id": draft["id"], "draft_version": draft["version"]} if draft else {}),
        }
    return pending[-8:], issued


def draft_basis_changes(owner, child, contract: dict, draft: dict, scope: dict | None) -> list[str]:
    current = assignment_basis(owner, child, contract)
    expected = draft["basis"]
    changes = []
    if scope:
        if scope["draft_id"] != draft["id"]:
            changes.append("pinned_draft")
        if not scope_owner_matches(owner, scope):
            changes.append("owner_scope")
        # The committed revision authorizes exactly this newer owner scope.
        # The source execution, issued constraints and physical base stay pinned.
        current["owner_contract"] = expected["owner_contract"]
    return changes + sorted(key for key in expected.keys() | current.keys() if expected.get(key) != current.get(key))


def draft_basis_matches(owner, child, contract: dict, draft: dict, scope: dict | None) -> bool:
    return not draft_basis_changes(owner, child, contract, draft, scope)


def validate_draft(manager, owner, event: dict, *, closeout: bool, reassign: bool = False) -> tuple:
    draft = event["draft"]
    latest = latest_draft(owner, event["source_process_id"])
    if latest is None or latest["id"] != draft["id"]:
        hint = f" The current draft is v{latest['version']} ({latest['id']})." if latest else ""
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_VERSION_CHANGED",
                                     "The selected draft may still be read. Review the current version before acting." + hint)
    child, contract, assignment = inspect_assignment(manager, owner, event, closeout=closeout, versioned=True)
    scope = recorded_scope_change(owner, child.process_id)
    if scope and not reassign:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_SCOPE_RETIRED",
                                     "Reassign only the responsibility retained by the committed scope revision.")
    if changes := draft_basis_changes(owner, child, contract, draft, scope):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_BASIS_CHANGED",
            "This draft no longer matches " + ", ".join(changes)
            + ". Inspect task_mechanism and the current draft before attempting reassignment.")
    review = owner.child_reviews.get(child.process_id) or {}
    if closeout and review.get("draft_id") == draft["id"] and review.get("verdict") == "approved":
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_ALREADY_DELIVERED",
                                     "This version has an accepted delivery. Open a separate follow-up for more work.")
    if scope:
        assignment.update(scope["remaining"], scope_change=scope)
    return child, contract, assignment


def snapshot_matches(version: dict, files: dict) -> bool:
    from backend.core.dependency_graph import _IGNORED_PARTS
    if version.get("state") != "observed":
        return False
    if version.get("scope") == "workspace":
        paths = [path for path in files if not _IGNORED_PARTS.intersection(path.casefold().split("/"))]
    elif version.get("scope") == "declared_inputs":
        paths = version.get("input_files") or []
        if not set(paths).issubset(files):
            return False
    else:
        return False
    if not paths:
        return False
    hashes = {path: hashlib.sha256(files[path][3]).hexdigest() for path in paths}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest == version.get("digest")


def draft_write_evidence(child, assignment, files):
    names = {row["tool_name"] for row in assignment["required_tool_calls"]
             if not row.get("include_composite_steps")}
    if not names:
        return []
    targets = set(assignment["target_files"])
    digests = {}
    evidence = {}
    for receipt in reversed(child.tool_receipts):
        tool = receipt.get("tool_name")
        paths = receipt.get("files") or []
        identifier = receipt.get("receipt_id")
        if (tool not in names or not identifier or identifier in evidence or len(paths) != 1
                or receipt.get("task_id") != assignment["source_task_id"]
                or receipt.get("effect") != "workspace_write" or receipt.get("file_service_result") is not True
                or receipt.get("succeeded") is not True or receipt.get("committed") is not True):
            continue
        path = paths[0]
        if path in targets and path in files:
            if path not in digests:
                digests[path] = hashlib.sha256(files[path][3]).hexdigest()
            digest = digests[path]
            if receipt.get("after_sha256") == digest:
                evidence[identifier] = {"receipt_id": identifier, "tool_name": tool, "path": path, "sha256": digest}
                if len(evidence) == 64:
                    break
    return list(evidence.values())


def remaining_tool_calls(assignment, draft, scope):
    """Count actual predecessor writes pinned to the retained or withdrawn files."""
    paths = set(scope["remaining"]["target_files"]) | set(scope["retired"]["target_files"])
    counts = {}
    for evidence in draft.get("write_evidence", []):
        if evidence["path"] in paths:
            name = evidence["tool_name"]
            counts[name] = counts.get(name, 0) + 1
    remaining = []
    for row in assignment["required_tool_calls"]:
        count = counts.get(row["tool_name"], 0) if not row.get("include_composite_steps") else 0
        minimum = int(row.get("min_calls", 1))
        if not count or row.get("max_calls") is not None:
            remaining.append(row)
        elif minimum > count:
            remaining.append({**row, "min_calls": minimum - count})
    return remaining


def capture_draft(manager, owner, event: dict, *, preserve_closed=False) -> dict:
    child, contract, assignment = inspect_assignment(manager, owner, event, closeout=preserve_closed, versioned=True)
    scope = recorded_scope_change(owner, child.process_id)
    if preserve_closed and not scope:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_SCOPE_CHANGE_INVALID", "Only an adopted withdrawal can preserve a closed checkout.")
    if scope:
        if scope.get("draft_id"):
            raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_SCOPE_RETIRED", "Close out the old scope and reassign its retained work.")
        check_pending_draft_basis(owner, child, contract, scope)
    lease = WorktreeLease.from_dict(child.worktree)
    worktrees = manager.worktree_manager
    if ((not preserve_closed and child.status.value != "running")
            or lease.state not in ({"leased", "sealed"} if preserve_closed else {"leased"})
            or Path(lease.path) != worktrees._target_path(child.process_id)
            or lease.task_id != assignment["source_task_id"]):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DRAFT_UNAVAILABLE",
                                     "Preserve a draft from the original worker's managed workspace.")
    for downstream in manager.downstream_of(child.process_id):
        if downstream.status.value != "waiting" or downstream.worktree.get("isolated"):
            raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_SCOPE_UNSUPPORTED",
                                         "A downstream execution already started on this assignment.")
    basis = assignment_basis(owner, child, contract)
    draft_id = event["coordination_event_id"]
    try:
        with manager._worktree_lock:
            draft, files = worktrees.freeze_assignment_draft(
                lease.path, lease.input_snapshot, assignment["target_files"], draft_id)
    except (OSError, ValueError, WorktreeError) as exc:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DRAFT_UNAVAILABLE", str(exc)) from exc
    receipts = {}
    revision = get_task_contract(child).get("revision", 0)
    pending = list(child.tool_receipts)
    while pending:
        receipt = pending.pop()
        pending[0:0] = list(receipt.get("child_receipts") or [])
        if receipt.get("task_id") != assignment["source_task_id"] or not receipt.get("test_id"):
            continue
        test_id = receipt["test_id"]
        if test_id in receipts or not receipt.get("receipt_id"):
            continue
        current = (receipt.get("task_contract_revision", 0) == revision
                   and receipt.get("committed") is True and receipt.get("succeeded") is True
                   and snapshot_matches(receipt.get("validation_version") or {}, files))
        receipts[test_id] = {
            "test_id": test_id, "receipt_id": receipt["receipt_id"],
            **({"target": receipt["test_target"]} if receipt.get("test_target") else {}),
            "passed": receipt.get("test_passed"),
            "evidence": "matching_file_inputs" if current else "historical",
        }
    remaining = {key: assignment[key] for key in ("target_files", "acceptance_criteria", "constraints",
        "required_test_ids", "required_tool_calls", "deliverables", "output_interfaces")}
    return {**draft, "id": draft_id, "basis": basis,
            "write_evidence": draft_write_evidence(child, assignment, files),
            "test_evidence": list(receipts.values())[:64], "remaining_acceptance": remaining}


def preserve_closed_scope(manager, owner, child) -> str:
    """Freeze a quiescent checkout under its already authorized withdrawal."""
    from .coordination import CoordinationOwnerUnavailable, publish_coordination_event

    scope = recorded_scope_change(owner, child.process_id)
    if not scope:
        return ""
    if owner.session.mechanism_activation_blocked:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_COMMIT_UNKNOWN", "Inspect the owner checkpoint before retrying.")
    if scope["draft_id"]:
        return scope["draft_id"]
    try:
        event = publish_coordination_event(manager, child, kind="coordination_request",
            summary="Runtime preserved the closed executor's workspace for the adopted withdrawal.",
            offer_draft=True, preserve_closed=True)
    except CoordinationOwnerUnavailable as exc:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_STALE", str(exc)) from exc
    except AssignmentHandoffError as exc:
        # A supervisor and the closeout callback may reach this boundary together.
        # Reuse the draft only if that exact scope was already durably bound.
        current = recorded_scope_change(owner, child.process_id) or {}
        if (exc.code == "ASSIGNMENT_HANDOFF_SCOPE_RETIRED" and current.get("change_id") == scope["change_id"]
                and current.get("draft_id") and not owner.session.mechanism_activation_blocked):
            return current["draft_id"]
        raise
    return event["draft"]["id"]


def checkpoint(manager, owner, entry: dict, rollback=lambda: None) -> None:
    from backend.core.storage import StorageCommitUnknown
    from .mechanism_lifecycle import commit_activation
    store = getattr(manager, "session_store", None)
    if owner.session.mechanism_activation_blocked:
        rollback()
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_COMMIT_UNKNOWN", "Inspect the owner checkpoint before retrying.")
    if store is None:
        rollback()
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_CHECKPOINT_FAILED", "No durable owner checkpoint is available.")
    try:
        commit_activation(owner, entry, store, rollback)
    except (OSError, RuntimeError, ValueError) as exc:
        code = ("ASSIGNMENT_HANDOFF_COMMIT_UNKNOWN" if isinstance(exc, StorageCommitUnknown)
                else "ASSIGNMENT_HANDOFF_CHECKPOINT_FAILED")
        raise AssignmentHandoffError(code, str(exc)) from exc


def review_draft_id(owner, child) -> str:
    draft = latest_draft(owner, child.process_id)
    if draft:
        return draft["id"]
    contract = owner.delegated_contracts.get(child.process_id) or {}
    return (contract.get("continuation") or {}).get("draft_id", "")


def requires_versioned_review(owner, child) -> bool:
    return bool(review_draft_id(owner, child)
                or (getattr(child, "role", None) == "subagent" and child.task_kind == "action")
                or getattr(child, "runtime_preferences", {}).get("asset_execution"))


def sealed_delivery_target(owner, child, manager) -> dict | None:
    assignment = owner.delegated_contracts.get(child.process_id) or {}
    worktree = child.worktree or {}
    if (not manager.owns_child(owner, child) or not assignment or assignment.get("superseded_by")
            or assignment.get("returned_assignment") or child.status.value != "completed"
            or not is_consumable_terminal(child, manager) or worktree.get("state") not in {"sealed", "disposed"}
            or not worktree.get("result_commit") or recorded_scope_change(owner, child.process_id)):
        return None
    draft = review_draft_id(owner, child)
    temporary = getattr(child, "role", None) == "subagent"
    owner_version = subagent_scope_digest(owner) if temporary else contract_digest(owner)
    if temporary:
        if (assignment.get("subagent_owner_contract") != owner_version or worktree.get("promoted")
                or owner.cancellation_event.is_set()):
            return None
    elif not requires_versioned_review(owner, child):
        return None
    commit = worktree["result_commit"]
    return {
        "result_ref": _selection_ref("result-" + child.process_id[:8], owner.process_id, owner_version,
                                     child.process_id, contract_digest(child), draft, commit),
        "process_id": child.process_id, "result_commit": commit, "draft_id": draft,
    }


def resolve_delivery_ref(owner, manager, result_ref) -> dict:
    targets = []
    for child_id in owner.coordination_snapshot()[0]:
        if not isinstance(result_ref, str) or not result_ref.startswith("result-" + child_id[:8] + "-"):
            continue
        child = manager.get(child_id)
        target = sealed_delivery_target(owner, child, manager) if child is not None else None
        if target is not None and target["result_ref"] == result_ref:
            targets.append(target)
        elif target is None and getattr(child, "role", None) == "subagent" and manager.owns_child(owner, child):
            assignment = owner.delegated_contracts.get(child_id) or {}
            worktree = child.worktree or {}
            if (scope := assignment.get("subagent_owner_contract")) and worktree.get("result_commit"):
                historical = _selection_ref("result-" + child_id[:8], owner.process_id, scope,
                    child_id, contract_digest(child), "", worktree["result_commit"])
                if historical == result_ref:
                    if worktree.get("promoted"):
                        raise AssignmentHandoffError("SUBAGENT_RESULT_ALREADY_APPLIED", "Use the existing delivery receipt.")
                    if owner.cancellation_event.is_set():
                        raise AssignmentHandoffError("SUBAGENT_CANCELLED", "The parent task was cancelled.")
                    raise AssignmentHandoffError("SUBAGENT_RESULT_STALE",
                        "The parent requirements changed. Recheck the current scope and ask a fresh helper if a patch is still needed.")
    if len(targets) != 1:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_VERSION_CHANGED",
            "Select the current result_ref from wait_agents or list_agents for the intended sealed result.")
    return targets[0]


def current_versioned_review(owner, child, review) -> bool:
    commit = (child.worktree or {}).get("result_commit")
    draft = review_draft_id(owner, child)
    temporary = getattr(child, "role", None) == "subagent"
    owner_version = subagent_scope_digest(owner) if temporary else contract_digest(owner)
    return bool(requires_versioned_review(owner, child) and commit and review.get("verdict") == "approved"
                and review.get("draft_id") == draft and review.get("result_commit") == commit
                and review.get("result_contract") == contract_digest(child)
                and review.get("owner_contract") == owner_version)


def commit_reassignment(manager, owner, successor, event: dict | None, contract: dict, *, previous=None) -> None:
    from backend.core.storage import StorageCommitUnknown
    from .mechanism_lifecycle import fence
    store = getattr(manager, "session_store", None)
    if store is None:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_CHECKPOINT_FAILED", "No durable reassignment checkpoint is available.")
    try:
        store.save_process_checkpoint(successor)
    except StorageCommitUnknown as exc:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_COMMIT_UNKNOWN", str(exc)) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_CHECKPOINT_FAILED", str(exc)) from exc
    with fence(owner), owner._context_lock, owner._coordination_lock:
        scope = None
        if event:
            previous, prior_contract, assignment = validate_draft(manager, owner, event, closeout=True, reassign=True)
            scope = assignment.get("scope_change")
        else:
            prior_contract = owner.delegated_contracts.get(previous.process_id) or {}
            if (not manager.owns_child(owner, previous) or not is_consumable_terminal(previous, manager)
                    or prior_contract.get("superseded_by") or prior_contract.get("returned_assignment")
                    or previous.session is not successor.session or owner.cancellation_event.is_set()):
                raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_STALE", "The retained execution changed before continuation admission.")
        lineage = contract.get("continuation") or {}
        prior_lineage = prior_contract.get("continuation") or {}
        if lineage.get("scope_change_id") != ((scope or {}).get("change_id") or prior_lineage.get("scope_change_id")):
            raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_BASIS_CHANGED", "The effective scope changed during admission.")
        original = dict(prior_contract)
        owner.register_child_contract(successor.process_id, contract, supersedes_process_id=previous.process_id)

        def rollback():
            owner.delegated_contracts[previous.process_id] = original
            owner.delegated_contracts.pop(successor.process_id)
            owner.child_ids.remove(successor.process_id)

        entry = {
            "event": "agent_coordination_resolution", "coordination_event_id": event["draft"]["id"],
            "status": "reassigned", "disposition": "reassign_remaining_work",
            "successor_process_id": successor.process_id, "draft_commit": event["draft"]["commit"],
            "resolved_by_process_id": owner.process_id,
        } if event else {"event": "assignment_scope_continued", "source_process_id": previous.process_id,
                        "successor_process_id": successor.process_id, "draft_id": lineage["draft_id"]}
        if lineage.get("scope_change_id"):
            entry.update(scope_change_id=lineage["scope_change_id"], retired=lineage["retired"])
        checkpoint(manager, owner, entry, rollback)


def verify_reassignment(manager, owner, successor, draft: dict) -> None:
    contract = owner.delegated_contracts[successor.process_id]
    source_id = contract.get("supersedes_process_id")
    previous = manager.get(source_id)
    prior_contract = owner.delegated_contracts.get(source_id) or {}
    latest = latest_draft(owner, source_id)
    scope = recorded_scope_change(owner, source_id)
    scope_id = (scope or {}).get("change_id") or (prior_contract.get("continuation") or {}).get("scope_change_id")
    if (owner.session.mechanism_activation_blocked or not manager.owns_child(owner, previous)
            or prior_contract.get("superseded_by") != successor.process_id
            or not is_consumable_terminal(previous, manager) or (not latest or latest["id"] != draft["id"])
            or (contract.get("continuation") or {}).get("scope_change_id") != scope_id
            or not draft_basis_matches(owner, previous, prior_contract, draft, scope)
            or (scope_id and draft.get("retained_target_files") != contract["target_files"])):
        raise WorktreeError("ASSIGNMENT_HANDOFF_BASIS_CHANGED: retained reassignment authority changed before checkout")


def accept_returned_delivery(manager, owner, event: dict, entry: dict) -> dict:
    from .completion_gates import CompletionReasons
    from .completion_protocol import HostCompletionEvaluator
    from .mechanism_lifecycle import fence
    from .validation_evidence import test_receipt_assessment
    from .interface_contract import verify_declared_contract

    child_id = event["source_process_id"]
    contract = owner.delegated_contracts.get(child_id) or {}
    returned = contract.get("returned_assignment") or {}
    version = event.get("draft")
    if (not version or returned.get("coordination_event_id") != version["id"]
            or returned.get("owner_task_id") != (owner.active_task_id or owner.process_id)):
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_STALE", "Complete the current returned assignment with its exact draft ID.")
    if returned.get("accepted_result"):
        return returned["accepted_result"]
    basis = contract_digest(owner)
    reasons = CompletionReasons()
    HostCompletionEvaluator._evaluate_returned_work(owner, returned, reasons)
    if reasons:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DELIVERY_PENDING", "\n".join(reasons))
    source = Path(owner.worktree_path or owner.workspace_root)
    try:
        with manager._worktree_lock:
            result, files = manager.worktree_manager.freeze_assignment_draft(
                source, version["base_commit"], returned["target_files"], str(uuid.uuid4()))
    except (OSError, ValueError, WorktreeError) as exc:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DRAFT_UNAVAILABLE", str(exc)) from exc
    violations = verify_declared_contract(returned.get("interface_contract"), source,
        returned.get("output_interfaces") or [], snapshot_files={name: row[3] for name, row in files.items()})
    if violations:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_INTERFACE_REVIEW_REQUIRED",
            "The returned result does not satisfy its declared interface: "
            + ", ".join(f"{row['ref']} ({row['type']})" for row in violations))
    for test_id in returned["required_test_ids"]:
        receipts = owner.tool_receipts[returned["owner_receipt_start"]:]
        if not any(receipt.get("test_id") == test_id and receipt.get("test_passed") is True
                   and receipt.get("succeeded") is True and receipt.get("committed") is True
                   and test_receipt_assessment(owner, receipt)["state"] == "current"
                   and snapshot_matches(receipt.get("validation_version") or {}, files)
                   for receipt in receipts):
            raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DELIVERY_PENDING",
                f"Rerun {test_id} on the files captured by this result snapshot.")
    result["draft_id"] = version["id"]
    result["owner_contract"] = basis
    result["target_digests"] = {name: hashlib.sha256(files[name][3]).hexdigest() if name in files else None
                                for name in returned["target_files"]}
    with fence(owner), owner._context_lock, owner._coordination_lock:
        if owner.delegated_contracts.get(child_id) is not contract or contract_digest(owner) != basis:
            raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_BASIS_CHANGED", "The assignment changed while accepting its delivery.")
        updated = {**returned, "accepted_result": result}
        owner.delegated_contracts[child_id] = {**contract, "returned_assignment": updated}
        entry["accepted_result"] = result
        checkpoint(manager, owner, entry, lambda: owner.delegated_contracts.__setitem__(child_id, contract))
    return result


def dependency_handoff(manager, owner, process_id: str) -> dict | None:
    """Project only a durably accepted result for a versioned dependency."""
    if owner is None:
        return None
    with owner._context_lock, owner._coordination_lock:
        return _dependency_handoff(manager, owner, process_id)


def _dependency_handoff(manager, owner, process_id: str) -> dict | None:
    version = latest_draft(owner, process_id)
    contract = owner.delegated_contracts.get(process_id) or {}
    inherited = (contract.get("continuation") or {}).get("draft_id")
    if not version and not inherited:
        return None
    if owner.session.mechanism_activation_blocked:
        return {"pending": True}
    returned = contract.get("returned_assignment")
    if returned:
        result = returned.get("accepted_result")
        if version and result and result["draft_id"] == version["id"] and result["owner_contract"] == contract_digest(owner):
            return {"returned": result, "process_id": owner.process_id, "source_process_id": process_id}
        return {"pending": True}
    producer_id, = manager.resolve_retained_dependencies(owner, [process_id])
    producer = manager.get(producer_id)
    review = owner.child_reviews.get(producer_id) or {}
    if (producer.status.value == "completed" and is_consumable_terminal(producer, manager)
            and current_versioned_review(owner, producer, review)):
        return {"process_id": producer_id}
    return {"pending": True}


def verify_returned_inputs(manager, owner, process) -> None:
    from backend.core.sandbox_read import ReadRoot
    from backend.core.tools.workspace_projection import MAX_FILE_BYTES
    for dependency_id in process.depends_on:
        handoff = dependency_handoff(manager, owner, dependency_id) or {}
        if not (result := handoff.get("returned")):
            continue
        if result["commit"] not in (process.worktree.get("upstream_commits") or []):
            raise WorktreeError("ASSIGNMENT_HANDOFF_VERSION_CHANGED: downstream did not execute against the accepted delivery")
        with ReadRoot(manager.worktree_manager.repo_root) as root:
            for path, expected in result["target_digests"].items():
                try:
                    with root.open(path) as binding:
                        actual = hashlib.sha256(binding.read_bytes(max_bytes=MAX_FILE_BYTES)).hexdigest()
                except FileNotFoundError:
                    actual = None
                if actual != expected:
                    raise WorktreeError(f"ASSIGNMENT_HANDOFF_BASIS_CHANGED: accepted upstream file changed: {path}")


def retain_draft(manager, child, targets: list[str]) -> dict:
    try:
        with manager._worktree_lock:
            lease = WorktreeLease.from_dict(child.worktree)
            if lease.state == "sealed" and lease.has_promotable_result:
                draft = {"commit": lease.result_commit, "files": lease.changed_files}
            else:
                draft = manager.worktree_manager.retain_returned_work(lease)
                child.worktree = lease.to_dict()
    except (OSError, WorktreeError) as exc:
        raise AssignmentHandoffError("ASSIGNMENT_HANDOFF_DRAFT_UNAVAILABLE", str(exc)) from exc
    return {**draft, "files": [path for path in draft["files"] if path in targets]}
