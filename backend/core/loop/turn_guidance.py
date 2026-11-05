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
    maintenance_tail = ""
    current, snapshot = None, None
    from .mechanisms import projection
    advancement = projection(process)
    control = advancement.get('user_control') or {}
    advancement_tail = ('\n[CURRENT ADVANCEMENT CONTROL]\n'
        + json.dumps({'effective_mode': advancement.get('mode'), 'phase': advancement.get('phase'),
            'revision': advancement.get('revision'), 'user_control': control}, ensure_ascii=False, sort_keys=True)
        + '\nThe effective mode governs the next unstarted work unit. A newer direct user switch supersedes '
          'older mode preferences, including pinned definitions; preserve it when reconciling the plan. '
          'Mode does not change scope, sandbox permissions, product choices or suspension.\n'
          'Manual is an invocation admission gate, not an instruction to stop reasoning or deliver a waiting message. '
          'Prepare the next permitted work unit and submit its actual tool call through the normal pipeline; '
          'the runtime pauses before its effect and creates the real manual_execution card. '
          'Prose saying you are waiting, a final answer or a new semantic question does not create that card. '
          'Auto continues through the same pipeline and still obeys all permissions and unresolved obligations.\n')
    if process.runtime_preferences.get('task_maintenance_enabled') or (process.context_snapshot or {}).get('task_maintenance'):
        from .task_maintenance import model_context
        current = model_context(process)
        maintenance_tail = ('Current task mechanism state (data, not additional authority):\n'
            + json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str))
    if "workbench" in name_set:
        from .protocol_tools.workbench import bound_sheets
        sheets = bound_sheets(process)
        if sheets is not None:
            snapshot = sheets.reference_snapshot()
            if snapshot["sheets"]:
                sheet_tail = "\n" + render_prompt("runtime/workbench_snapshot",
                    snapshot_json=json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    text = (
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
        ) + sheet_tail + advancement_tail + maintenance_tail
    )
    from .provider_context import ContextSections
    sections = {
        'runtime.instructions': _BASE_CURRENT_TURN_ENVELOPE.rstrip(),
        'runtime.snapshot_rules':render_prompt('runtime/capability_snapshot',
            current_turn='[See runtime.instructions]', surface_version='runtime.capability.surface_version',
            task_kind='runtime.capability.task_kind', execution_mode='runtime.capability.execution_mode',
            surface_phase='runtime.capability.surface_phase', lease_profile='runtime.capability.lease',
            next_action='runtime.capability.next_host_action', available='runtime.capability.available_tools',
            inactive_capabilities='runtime.capability.inactive_capabilities',
            phase_guidance='[See runtime.phase_guidance]', interaction_json='[See runtime.interaction]'),
        'runtime.capability': {'surface_version':surface_version, 'task_kind':process.task_kind,
            'execution_mode':execution_mode, 'surface_phase':surface_phase,
            'lease':lease_profile or 'not-active', 'next_host_action':next_action,
            'available_tools':names, 'inactive_capabilities':inactive},
        'runtime.phase_guidance':phase_guidance,
        'runtime.interaction':interaction,
        'runtime.advancement': {'effective_mode':advancement.get('mode'), 'phase':advancement.get('phase'),
            'revision':advancement.get('revision'), 'user_control':control},
        'runtime.advancement_guidance':'The effective mode' + advancement_tail.split('\nThe effective mode',1)[1],
    }
    # Preserve the exact full values. The Provider projection deduplicates
    # unchanged map fields; it does not summarize or drop task requirements.
    if current is not None:
        sections['runtime.maintenance'] = current
    if snapshot is not None and snapshot['sheets']:
        sections['runtime.workbench'] = sheet_tail
    return ContextSections(text, sections, scope=process.active_task_id or process.process_id)
