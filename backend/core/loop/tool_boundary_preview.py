"""Deterministic compilation before scheduling semantic tool-boundary work."""
from dataclasses import dataclass
from .operation_policy import is_effectful_mutation


@dataclass(frozen=True)
class ToolBoundaryPreview:
    contracts: list
    effectful: bool
    observation_only: bool = False


def initial_execution_calls(calls, tools):
    """Review only the first serial boundary against the current state.

    Later control calls must be compiled after their predecessors commit.
    Compiling a whole batch now can bind them to an obsolete revision even
    before execution begins. Questions and delivery claims remain batch-wide.
    """
    for index, call in enumerate(calls):
        tool = tools.get(call['name'])
        if tool is not None and (tool.alignment_contract is not None
                or is_effectful_mutation(tool.effect)
                or call['name'] in {'request_self_execute', 'delegate_task', 'decompose_task', 'prepare_task_bundle'}):
            return calls[:index + 1]
    return calls


def preview_tool_boundaries(calls, tools, process=None):
    contracts = []
    effectful = False
    from .operation_policy import is_observation_effect
    observation_only = bool(calls)
    for call in calls:
        tool = tools.get(call['name'])
        observation_only &= bool(tool and tool.read_only and is_observation_effect(tool.effect))
        if tool is None:
            continue
        if process is not None and is_effectful_mutation(tool.effect):
            from .task_maintenance import guard
            blocked = guard(process, tool.name)
            if blocked and blocked[0] in {'TASK_MAINTENANCE_REQUIRED', 'TASK_MECHANISM_SUSPENDED'}:
                # The actual pipeline still reports the current exact gate.
                # A known unready plan is not another paid semantic proposal.
                # Inspect every call so an independent valid boundary in this
                # batch is still reviewed; never synthesize a ready certificate.
                continue
        compiler = tool.alignment_contract
        if compiler is not None:
            try:
                if tool.prepare_args is None:
                    from .tool_pipeline import _validate_args
                    _validate_args(call.get('args') or {}, tool.parameters)
                contract = compiler(call.get('args') or {})
                if contract is not None:
                    contracts.append(contract)
                    observation_only = False
            except (ValueError, TypeError, KeyError):
                # The canonical pipeline returns this same compiler's argument
                # error before effects/grant consumption. An invalid proposal
                # is not a semantic effect boundary to spend a model call on.
                continue
        if call['name'] not in {'complete_task', 'complete_supervision', 'complete_review'}:
            effectful |= (call['name'] in {'request_self_execute', 'delegate_task', 'decompose_task', 'prepare_task_bundle'}
                          or is_effectful_mutation(tool.effect))
            from .alignment_routing import requires_business_readiness
            if compiler is None and requires_business_readiness(tool):
                import json
                import hashlib
                arguments = json.dumps(call.get('args') or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                contracts.append({'kind': 'business_readiness', 'tool_name': tool.name,
                    'effect': str(getattr(tool.effect, 'value', tool.effect)),
                    'arguments_preview': arguments[:4000], 'arguments_truncated': len(arguments) > 4000,
                    'arguments_digest': hashlib.sha256(arguments.encode()).hexdigest(),
                    'meaning': 'Assess current task prerequisites before business execution. This readiness assessment grants no tool permission and certifies no result.'})
    return ToolBoundaryPreview(contracts, effectful, observation_only)
