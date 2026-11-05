"""Cache routing hints and ordering, independent of execution authority."""
from .provider_protocol import CacheIntent, deterministic_hash


def prefix_intent(messages, tools, *, mode, purpose="main", epoch=0, prompt_hash=""):
    prefix = []
    for message in messages:
        if message.get("role") != "system":
            break
        prefix.append({"role": "system", "content": message.get("content", "")})
    stable = deterministic_hash(prefix)
    return CacheIntent(mode=mode, context_epoch=epoch,
        # Routing is a reuse hint, never a capability grant. Keep it stable
        # when the live tool set changes; record the full schema separately.
        stable_prefix_key=deterministic_hash({"purpose": purpose, "stable": stable}),
        stable_prefix_hash=stable, tool_schema_hash=deterministic_hash(tools or []),
        prompt_schema_hash=prompt_hash)


def order_live_tools(session, tools):
    """Keep surviving definitions in place; append new names deterministically.

    The caller has already filtered authorization. Never retain a withdrawn
    definition for caching. Only current names are retained across restart.
    """
    def name(item):
        return str((item.get("function") or item).get("name", ""))
    active = {name(item): item for item in tools or []}
    order = list(dict.fromkeys(session.provider_tool_order))
    order.extend(sorted(set(active).difference(order)))
    session.provider_tool_order = [key for key in order if key in active]
    return [active[key] for key in order if key in active]
