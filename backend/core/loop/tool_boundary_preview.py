"""Deterministic compilation before scheduling semantic tool-boundary work."""
from dataclasses import dataclass
from .operation_policy import is_effectful_mutation


@dataclass(frozen=True)
class ToolBoundaryPreview:
    contracts: list
    effectful: bool


def preview_tool_boundaries(calls, tools):
    contracts = []
    effectful = False
    for call in calls:
        tool = tools.get(call['name'])
        if tool is None:
            continue
        compiler = tool.alignment_contract
        if compiler is not None:
            try:
                contracts.append(compiler(call.get('args') or {}))
            except (ValueError, TypeError, KeyError):
                # The canonical pipeline returns this same compiler's argument
                # error before effects/grant consumption. An invalid proposal
                # is not a semantic effect boundary to spend a model call on.
                continue
        if call['name'] not in {'complete_task', 'complete_supervision', 'complete_review'}:
            effectful |= (call['name'] in {'request_self_execute', 'delegate_task', 'decompose_task', 'prepare_task_bundle'}
                          or is_effectful_mutation(tool.effect))
    return ToolBoundaryPreview(contracts, effectful)
