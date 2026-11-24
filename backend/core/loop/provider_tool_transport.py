"""Stable model-access framing; canonical tools retain all execution authority.

Only the transport function is stable. Its basic interface catalog projects
the already filtered live surface; selected large contracts arrive on demand. Decoding does
not look up, admit, authorize, validate business arguments or execute a tool.
"""
from copy import deepcopy
import json


CALL = 'gitgo_call'
FUNCTION = {'type':'function', 'function':{'name':CALL,
    'description':'Propose one currently available Gitgo tool call. runtime.tool_contracts supplies its basic interface. An on_demand tool first returns its exact details without executing; read them and resubmit the complete intended call. This framing grants no capability or permission.',
    'parameters':{'type':'object', 'properties':{'name':{'type':'string'},
        'arguments':{'type':'object','additionalProperties':True}},
        'required':['name','arguments'],'additionalProperties':True}}}
GUIDANCE = (
    'The native function gitgo_call is transport framing, not a business tool or an authorization service. '
    'Use gitgo_call with {"name": exact_current_tool_name, "arguments": original_argument_object}. '
    'runtime.tool_contracts supplies essential interfaces. When runtime.tool_groups is present, use '
    'capability_status operation=catalog (optional group, cursor) or search (query) to discover more tools, '
    'then describe with one exact name for its full contract. Search may miss matches; paged catalog is exhaustive. '
    'Without tool_groups, the complete basic catalog is supplied for compatibility. '
    'Call immediate_interface tools directly; capability_status describe can show their full guidance if needed. '
    'Other definitions marked detail_state=on_demand are overviews. '
    'Propose that selected tool with its basic arguments (or an empty object to inspect it); Runtime returns its exact current '
    'definition without executing the untouched calls. Read those details and resubmit the intended complete invocation. '
    'Compact definitions are already complete and need no separate preparation. '
    'Only names in runtime.capability.available_tools are currently callable; an interface view is not permission. '
    'Use one envelope only. The runtime removes bounded redundant transport-only envelopes, then runs the original tool pipeline, argument validation, capability lease, '
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
    """Project only the live basic interface; exact large definitions arrive
    at their selected call boundary, rather than the start of every task."""
    live = catalog(tools)
    for name, behavior in (behaviors or {}).items():
        if name in live:
            live[name]['runtime_behavior'] = deepcopy(behavior)
    from .tool_context import contract_overview
    return {name: contract_overview(value) for name, value in live.items()}


def reference_sections(tools, behaviors=None):
    from .tool_discovery import project_catalog
    live = catalog(tools)
    for name, behavior in (behaviors or {}).items():
        if name in live:
            live[name]['runtime_behavior'] = deepcopy(behavior)
    return project_catalog(live)


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
    original = {'id':str(call.get('id') or ''),'type':'function','function':{
        'name':CALL,'arguments':raw_arguments if raw_arguments is not None else json.dumps(args,ensure_ascii=False,separators=(',',':'))}}
    # CALL is reserved framing, never a business capability. Remove only exact
    # copies of that frame, without interpreting or unwrapping business fields.
    # The original wire transaction remains the replay authority, and the
    # resolved leaf still enters the unchanged capability/policy pipeline once.
    for _ in range(4):
        if (not isinstance(args,dict) or set(args) != {'name','arguments'}
            or not isinstance(args['name'],str) or not args['name'].strip()
            or not isinstance(args['arguments'],dict)):
            raise ValueError('INVALID_TOOL_TRANSPORT: expected exact name and arguments object; no tool executed')
        if args['name'] != CALL:
            break
        args=args['arguments']
    else:
        raise ValueError('INVALID_TOOL_TRANSPORT: nested framing exceeds four envelopes; no tool executed')
    return {**call, 'name':args['name'], 'args':deepcopy(args['arguments']), '_provider_wire_call':original}
