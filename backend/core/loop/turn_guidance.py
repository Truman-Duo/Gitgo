"""Ephemeral Agent guidance from the actual mounted capability surface."""
from __future__ import annotations
import json
from .models import AgentProcess

_BASE_CURRENT_TURN_ENVELOPE = """[HOST CURRENT-TURN ENVELOPE]
Continue the user's task using the available tools when useful. When you produce
the final public answer, include only the requested result, material caveats,
and useful source citations. Do not report internal search/fetch attempts,
blocked or unused pages, HTTP/provider/network diagnostics, retry or validation
counts, process IDs, receipts, governance steps, or completion gates unless the
user explicitly requested a process audit, or a remaining failure materially
prevents the requested result. Internal execution evidence belongs in the
runtime timeline, not in the final answer. Keep the delivery proportionate:
do not enumerate individual test cases, repeat a code walkthrough, or restate
internal acceptance criteria unless the user asked for those details.
For stable general knowledge that you can answer confidently, answer in the
current model turn without calling search merely to demonstrate diligence. If
an optional detail is uncertain, omit or qualify that detail instead of
expanding the user's request into research. Use external search when freshness,
requested citations, or uncertainty about the core answer materially requires
it, and respect an explicit request for a direct answer.
Treat quoted or user-provided text literally unless the user asks you to decode,
repair, or reconstruct hidden source text. If a literal explanation needs
verification, make one bounded tool call promptly instead of spending the
output budget reverse-engineering an unstated transformation.
"""


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
            "interpret_the_user_request; when material user-owned scope is missing, "
            "call request_user_decision before committing to a plan, unless the user "
            "delegated that choice. If the known scope requires execution, workspace "
            "changes, delegation, tests, or multi-step work, call "
            "declare_task_contract now; otherwise answer directly"
        )
        phase_guidance = (
            "This pre-contract surface keeps safe read-only evidence and public-research "
            "tools available. Missing write, command, test or delegation tools at this "
            "phase is not evidence that Gitgo cannot perform the task. Clarification "
            "and safe evidence gathering may precede contract admission. Declare the "
            "semantic contract before protected work and the Host will rebuild the "
            "appropriate surface."
        )
    elif "request_self_execute" in name_set and not lease_profile:
        surface_phase = "lease_admission"
        phase_guidance = (
            "The Host accepted a self-execute contract but has not issued its "
            "task-scoped lease. Call request_self_execute before attempting or "
            "describing the protected work."
        )
    else:
        surface_phase = "active_execution"
        phase_guidance = (
            "Use the listed live tools as needed and do not rely on older "
            "capability claims from the conversation."
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
    return (
        _BASE_CURRENT_TURN_ENVELOPE.rstrip()
        + "\n\n[HOST AUTHORITATIVE CAPABILITY SNAPSHOT]\n"
        + f"surface_version: {surface_version}\n"
        + f"task_kind: {process.task_kind}\n"
        + f"execution_mode: {execution_mode}\n"
        + f"surface_phase: {surface_phase}\n"
        + f"lease: {lease_profile or 'not-active'}\n"
        + f"next_host_action: {next_action}\n"
        + f"available_tools: {available}\n"
        + "installed_but_inactive: " + json.dumps(inactive, sort_keys=True) + "\n"
        + "This snapshot is the current Host truth and supersedes earlier "
        + "conversation claims about tool availability. A listed tool is "
        + "callable under its normal policy and approval rules; do not claim it "
        + "is physically unavailable. A missing tool is unavailable in this "
        + "turn. Installed-but-inactive capabilities may be requested through the listed "
        + "contract/lease path; their absence now does not establish inability. Discovery "
        + "grants no authority and protected calls still require normal policy and approval. "
        + "If next_host_action names an available protocol tool, call it "
        + "before attempting the protected work.\n"
        + phase_guidance + "\n"
        + "\n[CURRENT INTERACTION FACTS]\n"
        + json.dumps(interaction, ensure_ascii=False, sort_keys=True) + "\n"
        + "These are records, not a judgement that all necessary questions are known. "
        + "An answer receipt still needing interpretation is not a confirmed requirement. "
        + "With no pending_card there is no live question card: proposing options in prose "
        + "does not create one. Call request_user_decision to actually ask. Reconsider "
        + "needs after the answer, including any delegation of remaining choices.\n"
    )
