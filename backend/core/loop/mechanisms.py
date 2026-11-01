"""Advancement consumer of the shared mechanism lifecycle, independent of sandbox policy."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import uuid
from datetime import datetime, timezone

from .operation_policy import is_effectful_mutation
from . import mechanism_lifecycle as lifecycle
from .mechanism_lifecycle import owner

ADVANCEMENT = "task.advancement"
DEFINITION_VERSION = 1


def _validate(parameters):
    if type(parameters) is not dict or set(parameters) != {"mode"} or parameters["mode"] not in {"auto", "manual"}:
        raise ValueError("ADVANCEMENT_MODE_INVALID")


DEFINITION = lifecycle.Definition(
    ADVANCEMENT, DEFINITION_VERSION, (("mode", "auto"),),
    ("tool_admission", "manual_unit_continuation", "dashboard"),
    (("tool_operation_policy", 1), ("permission_broker", 1)),
    _validate, "invocation_admission",
)


def _view(record):
    return {**record, "mode": record["parameters"]["mode"]}


def snapshot(process):
    return _view(lifecycle.snapshot(process, DEFINITION))


def compile_revision(current, mode, expected_revision):
    return _view(lifecycle.compile_revision(current, DEFINITION, {"mode": mode}, expected_revision))


def activate(process, mode, expected_revision, store):
    return _view(lifecycle.activate(process, DEFINITION, {"mode": mode}, expected_revision, store))


def reconcile_definition(process, contract):
    """Compile stale mode preferences against the existing user control fact.

    A plan revision must not restore an older mode or fail unrelated scope
    maintenance. Preserve the authored value as provenance and project the
    newer selection into the compiled definition. Newer task-user evidence
    still goes through semantic review before it can change the mode.
    """
    from .mechanism_catalog import enabled, compile_definition
    from .task_maintenance import source_receipts
    definition = contract.get('mechanism')
    proposed = enabled(definition, ADVANCEMENT)
    if proposed is None:
        return
    current = snapshot(process)
    control_sequence = current.get('control_sequence', -1)
    receipts = source_receipts(process)
    if (control_sequence < 0 or proposed == current['parameters']
            or any(receipts.get(ref, {}).get('authority_sequence', -1) > control_sequence
                   for ref in definition['source_refs'])):
        return
    draft = {key: deepcopy(definition[key]) for key in
             ('id', 'intent', 'source_refs', 'catalog_version', 'elements', 'bindings')}
    for element in draft['elements']:
        if element['use'] == ADVANCEMENT:
            element['parameters'] = deepcopy(current['parameters'])
    contract['mechanism'] = compile_definition(draft)
    contract['mechanism_parameter_resolution'] = {
        'element': ADVANCEMENT, 'proposed': deepcopy(proposed),
        'effective': deepcopy(current['parameters']), 'source': 'newer_user_control',
        'authority_sequence': control_sequence, 'advancement_revision': current['revision'],
        'proposed_definition_digest': definition['digest']}


def unit_identity(process, tool, arguments, execution_id, call_index, call_id=""):
    from .permission_broker import tool_contract_digest
    from .completion_gates import contract_digest
    raw = {"process_id": process.process_id, "task_id": process.active_task_id,
           "execution_id": execution_id, "call_key": call_id or str(call_index),
           "task_contract": contract_digest(process),
           "tool": tool.name, "tool_contract": tool_contract_digest(tool), "arguments": arguments}
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def requires_manual_unit(tool):
    spec = getattr(tool, "composite_spec", None) or {}
    # Updating a task's interpretation/plan is preparation, not permission to
    # perform its business effects. The canonical broker still runs Grill and CAS.
    contract = getattr(tool, "execution_contract", None)
    if contract is not None and getattr(contract, "broker", "") in {
            "host.runtime.declare_task_contract", "host.runtime.request_self_execute"}:
        return False
    # Structured composites admit their individual leaves. Opaque programs are
    # reviewable as one invocation under their declared capability contract.
    return is_effectful_mutation(tool.effect) and spec.get("execution_mode") != "host_pipeline"


def admit(process, tool, arguments, execution_id, call_index, call_id=""):
    """Called under owner's lock immediately before permission consumption.

    A non-null return suspends an unstarted invocation. Approval is an exact,
    single-use advancement ticket, never a capability or policy grant.
    """
    current = snapshot(process)
    if owner(process).session.mechanism_activation_blocked:
        raise ValueError("MECHANISM_ACTIVATION_BLOCKED")
    if current["mode"] == "auto" or not requires_manual_unit(tool):
        return None
    unit = {"unit_id": unit_identity(process, tool, arguments, execution_id, call_index, call_id),
            "revision": current["revision"], "scope_id": current["scope_id"],
            "tool_name": tool.name, "arguments": deepcopy(arguments)}
    ticket = process.runtime_preferences.get("manual_unit_ticket")
    if ticket == {"unit_id": unit["unit_id"], "revision": unit["revision"]}:
        process.runtime_preferences.pop("manual_unit_ticket", None)
        return None
    return unit


def manual_decision(process, unit):
    from .decision_support import _decision_position
    return {"decision_id": str(uuid.uuid4()), "process_id": process.process_id,
            "task_id": process.active_task_id, "kind": "manual_execution",
            "created_at": datetime.now(timezone.utc).isoformat(), **_decision_position(process),
            "question": "Run the next work unit?",
            "why_user_must_decide": "Manual advancement; this does not grant tool or sandbox permissions.",
            "options": [{"label": "Run once", "action": "run_once"},
                        {"label": "Skip", "action": "skip"}],
            "allow_free_form": False, "manual_unit": deepcopy(unit)}


def publish_manual_decision(process, unit):
    decision = manual_decision(process, unit)
    process.pending_decision = decision
    if process.task_budget is not None:
        process.task_budget.pause_for_user()
    process.session.host_ledger.append({"event": "user_decision_requested", **decision})
    return decision


def approve_unit(process, pending, action):
    if action not in {"run_once", "skip"}:
        raise ValueError("MANUAL_UNIT_SELECTION_REQUIRED")
    unit = pending.get("manual_unit") or {}
    current = snapshot(process)
    if current["phase"] != "effective" or unit.get("revision") != current["revision"]:
        raise ValueError("MANUAL_UNIT_REVISION_STALE")
    if not process.pending_tool_batch:
        raise ValueError("MANUAL_UNIT_CONTINUATION_MISSING")
    if action == "run_once":
        process.runtime_preferences["manual_unit_ticket"] = {
            "unit_id": unit["unit_id"], "revision": unit["revision"]}
    process.pending_tool_batch["decision_action"] = "deny" if action == "skip" else "run_once"


def projection(process):
    try:
        current = snapshot(process)
        if owner(process).session.mechanism_activation_blocked:
            return {**current, "phase": "blocked", "error": "MECHANISM_ACTIVATION_BLOCKED"}
        return {**current, "continuation_error": process.runtime_preferences.get("mechanism_resume_blocked", "")}
    except (ValueError, AttributeError) as exc:
        return {"phase": "blocked", "mode": None, "error": str(exc)}
