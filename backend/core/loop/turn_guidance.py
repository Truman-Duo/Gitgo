"""Ephemeral Agent guidance from the actual mounted capability surface."""
from __future__ import annotations

from backend.core.prompt_templates import load_prompt, render_prompt
import json
from .models import AgentProcess

_BASE_CURRENT_TURN_ENVELOPE = load_prompt('runtime/current_turn')


def _build_current_turn_envelope(
    process: AgentProcess,
    tools: list[dict] | None,
    discovery: dict | None = None,
) -> str:
    """Describe the authoritative live tool surface in the ephemeral tail.

    Tool availability changes after contract compilation, lease issuance,
    provider switching and authored-tool mounting.  Historical assistant text
    can therefore contain a once-true claim that a capability was unavailable.
    The provider schemas are authoritative to the Host, but weaker models may
    continue a recent natural-language claim instead of re-reading that schema.
    Put a compact, versioned snapshot at the non-persisted tail so current truth
    wins without rewriting history or invalidating the cacheable prefix.
    """
    from backend.core.loop.task_contract import get_task_contract

    names: list[str] = []
    for item in tools or []:
        function = item.get("function") if isinstance(item, dict) else None
        name = str(
            (function or {}).get("name")
            if isinstance(function, dict)
            else item.get("name", "") if isinstance(item, dict) else ""
        ).strip()
        if name:
            names.append(name)
    names = sorted(set(names))

    contract = get_task_contract(process)
    compilation = dict(contract.get("routing_compilation") or {})
    execution_mode = str(contract.get("execution_mode") or "undeclared")
    next_action = str(compilation.get("next_action") or "continue_current_task")
    lease = getattr(process, "capability_lease", None)
    lease_profile = str(getattr(lease, "profile_id", "") or "")
    name_set = set(names)
    contract_declared = bool(
        contract.get("revision")
        or execution_mode in {"answer", "self_execute", "delegate", "either"}
    )
    if "declare_task_contract" in name_set and not contract_declared:
        surface_phase = "contract_admission"
        next_action = (
            load_prompt('runtime/contract_next_action')
        )
        phase_guidance = (
            load_prompt('runtime/contract_admission')
        )
    elif (execution_mode == "self_execute" and next_action == "request_self_execute"
          and "request_self_execute" in name_set and not lease_profile):
        surface_phase = "lease_admission"
        next_action = "request_self_execute"
        phase_guidance = (
            load_prompt('runtime/lease_admission')
        )
    else:
        surface_phase = "active_execution"
        if execution_mode == "self_execute" and lease_profile:
            next_action = "continue_current_task"
        elif execution_mode == "answer":
            next_action = "continue_answer"
        elif execution_mode == "delegate" and process.coordination_snapshot()[0]:
            next_action = "continue_supervision"
        elif next_action == "request_self_execute":
            # A stale admission hint is not a reason to call an absent tool.
            next_action = "continue_current_task"
        phase_guidance = (
            load_prompt('runtime/active_execution')
        )
    surface_version = ":".join((
        str(getattr(process, "context_version", 0) or 0),
        str(contract.get("revision", 0) or 0),
        str(process.task_kind or "answer"),
        lease_profile or "pre-lease",
    ))
    available = ", ".join(names) if names else "(none)"
    inactive = {profile['profile_id']: profile['inactive_installed_tools']
                for profile in (discovery or {}).get('requestable_profiles', [])}
    from .interaction_snapshot import interaction_snapshot
    interaction = interaction_snapshot(process)
    if interaction['state'] != 'consistent':
        from .engineering_workflow import EngineeringWorkflow
        # One durable notification per task/version; do not turn an observer
        # inconsistency into a fabricated card or a repeated semantic review.
        notices = process.session.host_ledger
        if not any(e.get('event') == 'engineering_notice' and e.get('code') == 'INTERACTION_STATE_CONFLICT'
                   and e.get('task_id') == (process.active_task_id or process.process_id) for e in notices):
            EngineeringWorkflow(process).notice('INTERACTION_STATE_CONFLICT',
                '提问状态与真实回执不一致；保留原始记录，不能据此宣称已显示问题卡。')
    sheet_tail = ""
    if "workbench" in name_set:
        from .protocol_tools.workbench import bound_sheets
        sheets = bound_sheets(process)
        if sheets is not None:
            snapshot = sheets.reference_snapshot()
            if snapshot["sheets"]:
                sheet_tail = "\n" + render_prompt("runtime/workbench_snapshot",
                    snapshot_json=json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    return (
        render_prompt(
            'runtime/capability_snapshot',
            current_turn=format(_BASE_CURRENT_TURN_ENVELOPE.rstrip(), ''),
            surface_version=format(surface_version, ''),
            task_kind=format(process.task_kind, ''),
            execution_mode=format(execution_mode, ''),
            surface_phase=format(surface_phase, ''),
            lease_profile=format(lease_profile or 'not-active', ''),
            next_action=format(next_action, ''),
            available=format(available, ''),
            inactive_capabilities=format(json.dumps(inactive, sort_keys=True), ''),
            phase_guidance=format(phase_guidance, ''),
            interaction_json=format(json.dumps(interaction, ensure_ascii=False, sort_keys=True), ''),
        ) + sheet_tail
    )
