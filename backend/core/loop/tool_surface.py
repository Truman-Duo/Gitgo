"""Separate known definitions, current capability, and exact leaf approval."""
from .capabilities import CapabilityProfiles
from .models import AgentProcess
from .agent_tool import AgentTool


def admission_snapshot(process, active_tools, known_tools):
    """Discover installed but inactive capabilities without issuing a lease.

    Missing from this turn is different from absent from the installation.
    Selecting an appropriate scope remains the Agent's semantic decision.
    """
    from .task_contract import get_task_contract
    active, known = set(active_tools), set(known_tools)
    contract = get_task_contract(process)
    lease = getattr(process, 'capability_lease', None)
    profiles = []
    if process.actor_kind == 'supervisor':
        for profile_id in CapabilityProfiles.ids():
            profile = CapabilityProfiles.get(profile_id)
            inactive = sorted((set(profile.tool_names) & known) - active)
            if profile.allows_self_execution and inactive:
                profiles.append({'profile_id': profile_id, 'inactive_installed_tools': inactive})
    options = []
    if 'declare_task_contract' in active:
        options.append({'tool': 'declare_task_contract',
                        'effect': 'Declare or revise the task scope before protected work.',
                        'authority_granted': False})
    if 'request_self_execute' in active:
        options.append({'tool': 'request_self_execute',
                        'effect': 'Request a task-scoped capability lease under existing policy.',
                        'leaf_approval_still_required': True})
    return {'scope': 'current_process_current_contract',
            'inactive_catalog_scope': 'self_execution_capability_profiles_only',
            'control_protocols_require_task_routing': True,
            'active_tools': sorted(active), 'execution_mode': contract.get('execution_mode', 'undeclared'),
            'lease_profile': getattr(lease, 'profile_id', '') if lease else '',
            'requestable_profiles': profiles, 'admission_options': options,
            'authority_granted': False}


def required_components(spec):
    names = spec.get('host_tools', []) if spec.get('execution_mode') == 'authored_host_python' else [
        step.get('tool') for step in spec.get('steps', [])]
    return tuple(dict.fromkeys(name for name in names if name))


def component_blocker(process, spec, catalog):
    required = required_components(spec)
    missing = [name for name in required if name not in catalog or process.tool_registry is None
               or not process.tool_registry.has(name)]
    if not missing:
        return None
    profiles = [CapabilityProfiles.get(name) for name in CapabilityProfiles.ids()]
    profiles = sorted((p for p in profiles if p.allows_self_execution and set(required) <= set(p.tool_names)),
                      key=lambda p: (len(p.tool_names), p.profile_id))
    actions = []
    if 'calculate' in missing:
        actions.append({'action': 'revise_tool_definition', 'name': spec.get('name', ''),
            'reason': 'The legacy calculate leaf is not issued by current profiles. Publish an explicit new definition using the mounted workbench schema; do not request a broader lease or rewrite an approved pending call.',
            'replacement': {'tool': 'workbench', 'operation': 'calculate',
                            'expression': 'preserve the original expression binding'}})
    elif process.actor_kind == 'supervisor' and profiles:
        actions.append({'action': 'request_self_execute', 'arguments': {
            'profile_id': profiles[0].profile_id,
            'reason': 'Execute the declared leaf capabilities of saved tool ' + spec.get('name', ''),
            'intended_actions': ['Invoke ' + spec.get('name', '') + ' through the Host leaf pipeline; wait for exact approval']}})
    else:
        actions.append({'action': 'escalate_to_supervisor', 'required_tools': missing})
    if spec.get('execution_mode') == 'authored_host_python' and 'calculate' not in missing:
        actions.append({'action': 'author_tool', 'arguments': {'operation': 'use', 'name': spec.get('name', '')}})
    from backend.core.errors import error_payload
    message = 'Declared leaf capabilities are not active: ' + ', '.join(missing)
    return {**error_payload('CUSTOM_TOOL_CAPABILITY_REQUIRED', message=message,
        details={'required_tools': list(required), 'missing_tools': missing}, next_actions=actions),
        'detail': message, 'required_tools': list(required), 'missing_tools': missing, 'next_actions': actions}


def _select_authorized_tools(
    process: AgentProcess,
    dispatcher_tools: dict[str, AgentTool],
    *, issue_protocol_scopes: bool = True,
) -> tuple[dict[str, AgentTool], list[str]]:
    """Resolve one capability set for every model/execution surface.

    Every actor, including Ring 0, is restricted by the server-issued profile.
    Host protocol tools are added according to actor/task semantics.
    """
    if process.tool_registry is None:
        return {}, ["<missing ToolRegistry>"]

    declared = process.tool_registry.list_all()
    if (
        process.actor_kind == "supervisor"
        and process.capability_lease is None
        and _supervisor_is_coordination_only(process)
    ):
        # Enforce coordination-only mode from the *current task contract*.
        # Historical B contracts remain available for audit/owner routing, but
        # must never remove ordinary tools from a later unrelated user turn.
        # Explicit self-execution remains available via a scoped lease.
        delegated_mode = {
            "context_open", "context_search", "artifact_read", "read_child_artifact", "workbench",
            "decision_evidence", "declare_task_contract", "prepare_task_bundle", "delegate_task",
            "delegate_task_dag", "delegate_task_bundle", "list_agents", "wait_agents",
            "send_feedback", "cancel_agent", "request_review",
            "list_coordination_events", "resolve_coordination_event",
            "review_child_outcome", "request_user_decision", "request_permission",
            "acknowledge_governance_signal",
            "define_tool", "author_tool", "engineering_workflow",
            "promote_agent_changes", "complete_supervision",
        }
        declared = [name for name in declared if name in delegated_mode]
    unavailable = sorted(name for name in declared if name not in dispatcher_tools)
    authorized = {
        name: dispatcher_tools[name]
        for name in declared
        if name in dispatcher_tools
    }
    # The model has already supplied the semantic routing facts.  Once the
    # compiled contract selects bounded single-owner execution, omit the three
    # delegation entry points from the current provider surface.  A may revise
    # the contract if the work genuinely grows; the next surface rebuild then
    # exposes delegation again.  This avoids a reject/retry turn without using
    # keyword intent rules or granting execution authority implicitly.
    if process.actor_kind == "supervisor":
        from backend.core.loop.task_contract import get_task_contract
        contract = get_task_contract(process)
        host_requirements = dict(contract.get("host_requirements") or {})
        execution_mode = str(
            contract.get("execution_mode") or ""
        ).strip().lower()
        contract_declared = bool(
            contract.get("revision")
            or execution_mode in {"answer", "self_execute", "delegate", "either"}
        )
        active_supervision_contract = any(
            bool(dict(item or {}).get("required_for_parent_completion", True))
            and not str(dict(item or {}).get("superseded_by") or "")
            for item in process.delegated_contracts.values()
        )
        restored_delegate_contract = bool(
            execution_mode in {"delegate", "either"}
            and (
                contract.get("delegation_required")
                or contract.get("agent_required_delegation")
                or contract.get("user_requested_delegation")
            )
        ) or active_supervision_contract
        if (
            process.task_kind == "answer"
            and not contract_declared
            and not restored_delegate_contract
        ):
            # The adaptive public turn must have one unambiguous workflow
            # entry point.  A first declares the semantic facts; the Host then
            # compiles those facts into either self execution or delegation.
            # Advertising both authority-expansion and delegation before that
            # compilation made weaker providers guess between mutually
            # exclusive paths, often obtaining a useless governance lease or
            # attempting a B before a contract existed.  Explicit manual B
            # creation is already a Host fact, so that path remains available.
            authorized.pop("request_self_execute", None)
            manual_b_creation = bool(host_requirements.get("manual_B_creation"))
            if not manual_b_creation:
                for name in (
                    "delegate_task", "delegate_task_dag", "delegate_task_bundle",
                ):
                    authorized.pop(name, None)
                # This is an execution boundary, not merely prompt advice.
                # Before the semantic contract exists, a supervisor may answer
                # directly, gather read-only evidence, declare the contract, or
                # ask the user to resolve genuine ambiguity.  Protected write,
                # command, test and delegation tools stay hidden until the Host
                # compiles one route; public research does not need an artificial
                # workflow merely to discover whether current evidence exists.
                pre_contract_tools = set(CapabilityProfiles.pre_contract_tools())
                authorized = {
                    name: tool for name, tool in authorized.items()
                    if name in pre_contract_tools
                }
        if str(contract.get("execution_mode") or "").strip().lower() == "self_execute":
            for name in (
                "delegate_task", "delegate_task_dag", "delegate_task_bundle",
            ):
                authorized.pop(name, None)
    # Completion protocols are mutually exclusive.  Public-answer turns close
    # naturally; self-executed mutations submit a structured action claim;
    # delegated supervisors synthesize reviewed child outcomes.  Leaving all
    # three visible made models choose by name rather than current Host state.
    if process.task_kind != "supervisor":
        authorized.pop("complete_supervision", None)
    if process.task_kind != "action":
        authorized.pop("complete_task", None)
    if process.task_kind != "review":
        authorized.pop("complete_review", None)
    if (
        process.actor_kind == "supervisor"
        and process.capability_profile_id == "supervisor.control"
        and bool(
            dict(
                (process.context_snapshot or {}).get("task_contract") or {}
            ).get("revision")
        )
        and "request_self_execute" in dispatcher_tools
    ):
        authorized["request_self_execute"] = dispatcher_tools["request_self_execute"]
    if process.task_kind == "action" and "complete_task" in dispatcher_tools:
        # Host protocol tools are issued by task semantics rather than the
        # caller-selected capability profile.  Keep the provider surface and
        # RingGate's execution surface identical by recording that scoped grant.
        if issue_protocol_scopes:
            process.tool_registry.add_scoped("complete_task")
        authorized["complete_task"] = dispatcher_tools["complete_task"]
    if process.task_kind == "review" and "complete_review" in dispatcher_tools:
        if issue_protocol_scopes:
            process.tool_registry.add_scoped("complete_review")
        authorized["complete_review"] = dispatcher_tools["complete_review"]
    return authorized, unavailable


def _supervisor_is_coordination_only(process: AgentProcess) -> bool:
    """Return whether the current task epoch committed A to delegation."""
    if process.actor_kind != "supervisor":
        return False
    from backend.core.loop.task_contract import get_task_contract

    contract = get_task_contract(process)
    mode = str(contract.get("execution_mode") or "").strip().lower()
    if mode == "delegate":
        return True
    return bool(
        contract.get("delegation_required")
        or contract.get("agent_required_delegation")
    ) and mode in {"", "either"}
