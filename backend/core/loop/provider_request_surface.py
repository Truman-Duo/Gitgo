"""Prepare protocol inputs from the live tool surface and repaired history."""
from dataclasses import dataclass
from .llm_adapter import build_tools_json


@dataclass(frozen=True)
class ProviderRequestSurface:
    tools: list | None
    web_mode: str
    protocol: str


def prepare_provider_request(process, provider, surface, route, emit):
    if getattr(surface, "synchronize", None):
        surface.synchronize()
    from backend.core.mechanism_runtime.application import bound_application
    composition = bound_application(process)
    if composition:
        composition.maintain()
        composition.requirements.synchronize()
        composition.inbox.reconcile()
        composition.drain_discussions()
        composition.dispatch("before_context", f"context:{process.session.session_id}:{process.session.context_epoch}:{process.steps_used}",
            {"task_id": composition.task, "context_epoch": process.session.context_epoch, "steps_used": process.steps_used})
    tools = build_tools_json(surface.tools) if surface.tools else None
    mode = str(process.runtime_preferences.get('web_search_mode') or 'auto')
    protocol = str(getattr(getattr(provider, 'protocol', ''), 'value', getattr(provider, 'protocol', '')))
    if tools and mode == 'disabled':
        tools = [item for item in tools if str((item.get('function') or {}).get('name') or item.get('name') or '')
                 not in {'web_search', 'web_fetch'}] or None
    from .provider_cache import order_live_tools
    tools = order_live_tools(process.session, tools) or None
    active = [str((item.get('function') or {}).get('name') or item.get('name') or '') for item in tools or []]
    excluded = {'web_search', 'web_fetch'} if mode == 'disabled' else set()
    route.prepare_envelope(tools, surface.discovery(active, excluded=excluded))
    sealed = process.session.seal_dangling_tool_calls(recovery_code='LIVE_TOOL_RESULT_UNAVAILABLE', provider_route=route.route)
    if sealed:
        emit({'event': 'provider_tool_calls_sealed', 'tool_call_ids': sealed,
              'execution_state': 'unknown', 'recovery_code': 'LIVE_TOOL_RESULT_UNAVAILABLE'})
    from .provider_tool_transport import wire_tools
    return ProviderRequestSurface(wire_tools(tools), mode, protocol)
