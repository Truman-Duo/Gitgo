"""Select instruction modules from authoritative typed review context.

Unknown/unavailable state keeps the complete policy. This is input assembly,
not a business classifier, readiness verdict, permission or cached state.
"""
from backend.core.prompt_templates import load_prompt, PromptTemplateError

COMMON = 'alignment/assessor'
MECHANISMS = 'alignment/assessor_mechanisms'


class AlignmentPolicyUnavailable(RuntimeError):
    def __init__(self, resource):
        self.resource = resource
        super().__init__('Requirement review instruction resource unavailable: ' + resource)


def selected_modules(payload):
    state = payload.get('harness_semantics')
    known_empty = (isinstance(state, dict) and state.get('state_available') is True
        and isinstance(state.get('task_id'), str) and bool(state['task_id'])
        and state.get('instances') == [] and state.get('authority_granted') is False
        and state.get('definitions_present') is False)
    proposals = payload.get('proposed_contracts')
    if proposals is None:
        proposals = []
    ordinary = isinstance(proposals, list) and all(isinstance(p, dict)
        and (p.get('kind') is None or p.get('kind') == 'business_readiness') for p in proposals)
    return [COMMON] if known_empty and ordinary else [COMMON, MECHANISMS]


def policy_text(payload):
    parts = []
    for resource in selected_modules(payload):
        try:
            parts.append(load_prompt(resource))
        except PromptTemplateError as exc:
            raise AlignmentPolicyUnavailable(resource) from exc
    return '\n\n'.join(parts)
