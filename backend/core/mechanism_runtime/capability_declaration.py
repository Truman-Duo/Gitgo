"""Compile callable declarations into the runtime's canonical typed program.

This is a compiler input, not a second executor. Only event plumbing is implicit;
conditions and non-event data dependencies are explicit semantic choices.
"""
from copy import deepcopy

from .contracts import bounded_json, identifier


def lower(declaration, registry, source_refs):
    value = bounded_json(declaration)
    if not isinstance(value, dict) or set(value) - {'id', 'intent', 'on', 'calls'}:
        raise ValueError('CAPABILITY_DECLARATION_INVALID')
    identifier(value.get('id'))
    boundaries = value.get('on')
    if not isinstance(boundaries, list) or not boundaries or len(set(boundaries)) != len(boundaries):
        raise ValueError('CAPABILITY_EVENTS_REQUIRED')
    calls = value.get('calls')
    if not isinstance(calls, list) or not 1 <= len(calls) <= 63:
        raise ValueError('CAPABILITY_CALLS_REQUIRED')
    event_id = 'lifecycle'
    elements = [{'id': event_id, 'use': 'runtime.event', 'version': 1, 'parameters': {'boundaries': boundaries}}]
    bindings, descriptors, names = [], {}, set()
    for call in calls:
        if not isinstance(call, dict) or set(call) - {'id', 'use', 'version', 'parameters', 'inputs', 'when', 'after', 'on'}:
            raise ValueError('CAPABILITY_CALL_INVALID')
        name = identifier(call.get('id'))
        if name == event_id or name in names:
            raise ValueError('CAPABILITY_CALL_ID_CONFLICT')
        names.add(name)
        descriptor = registry.resolve(call.get('use'), call.get('version'))
        if descriptor['entry']:
            raise ValueError('CAPABILITY_ENTRY_IS_RUNTIME_OWNED')
        descriptors[name] = descriptor
        elements.append({'id': name, 'use': descriptor['id'], 'version': descriptor['version'],
            'parameters': deepcopy(call.get('parameters', {}))})
        if 'on' in call:
            selected = call['on']
            if (not isinstance(selected, list) or not selected or any(not isinstance(b, str) for b in selected)
                    or not set(selected).issubset(boundaries)):
                raise ValueError('CAPABILITY_CALL_BOUNDARY_INVALID: ' + name)
            elements[-1]['on'] = deepcopy(selected)

    def endpoint(reference):
        if not isinstance(reference, str) or '.' not in reference:
            raise ValueError('CAPABILITY_OUTPUT_REFERENCE_REQUIRED')
        node, port = reference.rsplit('.', 1)
        if node not in names | {event_id}:
            raise ValueError('CAPABILITY_OUTPUT_NODE_MISSING')
        return {'node': node, 'port': port}

    def bind(kind, reference, name, port, **extra):
        bindings.append({'kind': kind, 'from': endpoint(reference), 'to': {'node': name, 'port': port}, **extra})

    for call in calls:
        name, descriptor = call['id'], descriptors[call['id']]
        inputs = call.get('inputs', {})
        if not isinstance(inputs, dict):
            raise ValueError('CAPABILITY_INPUTS_INVALID')
        ports = {p['name']: p for p in descriptor['inputs']}
        unknown = sorted(set(inputs) - set(ports))
        if unknown:
            available = ', '.join(f"{port}:{definition['type']}" for port, definition in ports.items()) or '(none)'
            raise ValueError(f"CAPABILITY_INPUT_UNKNOWN: call '{name}' ({descriptor['id']}@{descriptor['version']}); "
                f"unknown inputs: {', '.join(unknown)}; available inputs: {available}. "
                'Bind only the listed input ports; component configuration belongs in parameters.')
        if 'when' in call and 'after' in call:
            raise ValueError('CAPABILITY_CONTROL_AMBIGUOUS')
        if ('when' in call or 'after' in call) and ('go' not in ports or 'go' in inputs):
            raise ValueError('CAPABILITY_CONTROL_PORT_REQUIRED')
        for port, definition in ports.items():
            if port in inputs:
                bind('trigger' if definition['type'] == 'trigger' else 'data', inputs[port], name, port)
            elif port == 'go' and definition['type'] == 'trigger':
                if 'when' in call:
                    condition = call['when']
                    if isinstance(condition, str):
                        condition = {'output': condition, 'equals': True}
                    if (not isinstance(condition, dict) or set(condition) - {'output', 'equals'}
                            or type(condition.get('equals', True)) is not bool):
                        raise ValueError('CAPABILITY_CONDITION_INVALID')
                    bind('condition', condition.get('output'), name, port, when=condition.get('equals', True))
                else:
                    bind('trigger', call.get('after', event_id + '.go'), name, port)
            elif port == 'event' and definition['type'] == 'record':
                bind('data', event_id + '.event', name, port)
            elif definition['required']:
                raise ValueError('CAPABILITY_EXPLICIT_INPUT_REQUIRED: ' + name + '.' + port)
    return {'schema_version': 2, 'id': value['id'], 'intent': value.get('intent'), 'scope': 'task',
        'source_refs': list(source_refs), 'elements': elements, 'bindings': bindings}


def revise(declaration, changes):
    """Named edits retain node identity; no JSON array offsets or hidden rewrite."""
    result = bounded_json(declaration)
    changes = bounded_json(changes)
    if not isinstance(changes, dict) or not changes or set(changes) - {'intent', 'on', 'calls', 'remove'}:
        raise ValueError('CAPABILITY_REVISION_INVALID')
    for key in ('intent', 'on'):
        if key in changes:
            result[key] = changes[key]
    edits = changes.get('calls', [])
    removed = changes.get('remove', [])
    if not isinstance(edits, list) or not isinstance(removed, list) or len(set(removed)) != len(removed):
        raise ValueError('CAPABILITY_REVISION_CALLS_INVALID')
    current = {call['id']: call for call in result['calls']}
    if any(name not in current for name in removed):
        raise ValueError('CAPABILITY_REVISION_NODE_MISSING')
    for name in removed:
        del current[name]
    seen = set()
    for edit in edits:
        if not isinstance(edit, dict) or edit.get('id') in seen or edit.get('id') in removed:
            raise ValueError('CAPABILITY_REVISION_NODE_CONFLICT')
        name = identifier(edit.get('id'))
        seen.add(name)
        previous = current.get(name, {})
        if not previous and not edit.get('use'):
            raise ValueError('CAPABILITY_REVISION_NEW_CALL_REQUIRES_USE: ' + name
                + '; existing call ids: ' + ', '.join(current))
        merged = {**previous, **edit}
        for key in ('parameters', 'inputs'):
            if key in edit:
                if not isinstance(edit[key], dict):
                    raise ValueError('CAPABILITY_REVISION_MAPPING_REQUIRED')
                merged[key] = {**previous.get(key, {}), **edit[key]}
        current[name] = merged
    result['calls'] = list(current.values())
    return result
