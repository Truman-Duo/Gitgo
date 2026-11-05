"""Stable model-access framing; canonical tools retain all execution authority.

Only the transport function is stable. Its current callable catalog is an
exact context projection of the already filtered live surface. Decoding does
not look up, admit, authorize, validate business arguments or execute a tool.
"""
from copy import deepcopy
import json


CALL = 'gitgo_call'
FUNCTION = {'type':'function', 'function':{'name':CALL,
    'description':'Call one currently available Gitgo tool. Read its exact name, description and argument schema in the current runtime.tool_contracts context. This transport grants no capability or permission.',
    'parameters':{'type':'object', 'properties':{'name':{'type':'string'},
        'arguments':{'type':'object','additionalProperties':True}},
        'required':['name','arguments'],'additionalProperties':True}}}
GUIDANCE = (
    'The native function gitgo_call is transport framing, not a business tool or an authorization service. '
    'Use gitgo_call with {"name": exact_current_tool_name, "arguments": original_argument_object}. '
    'runtime.tool_contracts contains the exact currently callable definitions, including argument schemas. '
    'runtime.tool_contracts is a reference catalog. Only names in runtime.capability.available_tools are currently callable; '
    'a retained description is not availability or permission. '
    'The runtime unwraps once, then runs the original tool pipeline, argument validation, capability lease, '
    'policy, manual/auto admission, sandbox, cancellation and completion rules. '
    'A call to an absent or revoked tool fails; do not infer a new capability from the stable transport function. '
    'Multiple calls may use normal native parallel function calls; do not put a batch inside arguments.'
)


def wire_tools(tools):
    return [deepcopy(FUNCTION)] if tools else None


def catalog(tools):
    return {str((item.get('function') or item)['name']):deepcopy(item.get('function') or item)
        for item in tools or []}


def reference_catalog(session, tools, *, scope, behaviors=None):
    """Retain unchanged schemas without retaining callable capabilities.

    Reuses the optional presentation cache, not a new registry or authority.
    The active surface always replaces a schema, and scope changes discard
    prior descriptions. Inactive descriptions are bounded and never executed.
    """
    state = getattr(session, 'provider_context_cache', {})
    previous = (state.get('snapshot') or {}).get('runtime.tool_contracts', {}) if state.get('scope') == scope else {}
    live = catalog(tools)
    for name, behavior in (behaviors or {}).items():
        if name in live:
            live[name]['runtime_behavior'] = deepcopy(behavior)
    retained = {name: deepcopy(value) for name, value in previous.items() if name not in live}
    retained = dict(list(retained.items())[-128:])
    return {**retained, **live}


def argument_error_guidance(tool_name, args, schema):
    """Diagnose framing at a rejected schema boundary; never guess/unwrap it."""
    if (isinstance(args, dict) and set(args) == {'name', 'arguments'}
            and args['name'] == tool_name and isinstance(args['arguments'], dict)
            and not {'name', 'arguments'}.issubset(schema.get('properties', {}))):
        return ('The business arguments contain an extra transport envelope. Use native gitgo_call once with '
            '{"name":"' + tool_name + '","arguments":{the business fields}}. '
            'Do not nest another name/arguments object inside arguments. No operation was executed.')
    return ''


def decode(call, *, raw_arguments=None):
    if call.get('name') != CALL:
        return call
    args=call.get('args')
    if (not isinstance(args,dict) or set(args) != {'name','arguments'}
        or not isinstance(args['name'],str) or not args['name'].strip()
        or not isinstance(args['arguments'],dict)):
        raise ValueError('INVALID_TOOL_TRANSPORT: expected exact name and arguments object; no tool executed')
    original = {'id':str(call.get('id') or ''),'type':'function','function':{
        'name':CALL,'arguments':raw_arguments if raw_arguments is not None else json.dumps(args,ensure_ascii=False,separators=(',',':'))}}
    return {**call, 'name':args['name'], 'args':deepcopy(args['arguments']), '_provider_wire_call':original}
