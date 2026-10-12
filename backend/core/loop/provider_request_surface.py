"""Prepare protocol inputs from the live tool surface and repaired history."""
from dataclasses import dataclass
from .llm_adapter import build_tools_json


@dataclass(frozen=True)
class ProviderRequestSurface:
    tools: list | None
    web_mode: str
    protocol: str


def prepare_provider_request(process, provider, surface, route, emit):
    tools = build_tools_json(surface.tools) if surface.tools else None
    mode = str(process.runtime_preferences.get('web_search_mode') or 'auto')
    protocol = str(getattr(getattr(provider, 'protocol', ''), 'value', getattr(provider, 'protocol', '')))
    if tools and mode == 'disabled':
        tools = [item for item in tools if str((item.get('function') or {}).get('name') or item.get('name') or '')
                 not in {'web_search', 'web_fetch'}] or None
    active = [str((item.get('function') or {}).get('name') or item.get('name') or '') for item in tools or []]
    excluded = {'web_search', 'web_fetch'} if mode == 'disabled' else set()
    route.prepare_envelope(tools, surface.discovery(active, excluded=excluded))
    sealed = process.session.seal_dangling_tool_calls(recovery_code='LIVE_TOOL_RESULT_UNAVAILABLE', provider_route=route.route)
    if sealed:
        emit({'event': 'provider_tool_calls_sealed', 'tool_call_ids': sealed,
              'execution_state': 'unknown', 'recovery_code': 'LIVE_TOOL_RESULT_UNAVAILABLE'})
    return ProviderRequestSurface(tools, mode, protocol)
