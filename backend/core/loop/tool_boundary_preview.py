"""Deterministic compilation before scheduling semantic tool-boundary work."""
from dataclasses import dataclass
from .operation_policy import is_effectful_mutation


@dataclass(frozen=True)
class ToolBoundaryPreview:
    contracts: list
    effectful: bool
    observation_only: bool = False


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
                contracts.append(compiler(call.get('args') or {}))
            except (ValueError, TypeError, KeyError):
                # The canonical pipeline returns this same compiler's argument
                # error before effects/grant consumption. An invalid proposal
                # is not a semantic effect boundary to spend a model call on.
                continue
        if call['name'] not in {'complete_task', 'complete_supervision', 'complete_review'}:
            effectful |= (call['name'] in {'request_self_execute', 'delegate_task', 'decompose_task', 'prepare_task_bundle'}
                          or is_effectful_mutation(tool.effect))
    return ToolBoundaryPreview(contracts, effectful, observation_only)
