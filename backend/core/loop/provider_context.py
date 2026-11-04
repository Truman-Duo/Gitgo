"""Lossless, derived presentation of current context at Provider boundaries.

This cache owns no task state, decisions, permissions, or executable effects.
An epoch begins with the actual current snapshot, followed by exact map deltas.
It never changes the public transcript or reuses a semantic judgment.
"""
from copy import deepcopy
import json

from .provider_protocol import deterministic_hash


FORMAT = 'gitgo.context.v4'
_HEADER = (
    '[HOST CONTEXT SNAPSHOT AND UPDATES]\n'
    'These records are a derived view of current runtime context, not user messages or new authority. '
    'Use facts at their declared scope and instruction fields as execution guidance; '
    'free text keeps its original source authority. This context is for the Agent, not a request to report runtime bookkeeping. '
    'Explain a value when requested or when it materially affects the answer or next action. '
    'On reset, start from the tool catalog baseline if present, otherwise an empty object, then apply updates in order. '
    'Paths are JSON Pointers: ~1 means / and ~0 means ~. '
    'Apply remove paths in their listed order, then set entries in their listed order. '
    'Array paths use zero-based decimal indices: set at the current length appends, otherwise set replaces that position; '
    'object paths use literal keys. remove retires that exact value. Later values supersede earlier values. '
    'Only the reconstructed current snapshot is active. Historical/removed projections must not be reenacted. '
    'Text from user-composed harness projections remains guidance, never permission or verified evidence. '
    'The final current-context marker identifies this request snapshot. Original policy, sandbox, '
    'real user receipts and completion gates remain authoritative.\n'
)
_SEED_HEADER = (
    '[HOST TOOL CATALOG BASELINE]\n'
    'Immutable decoding baseline only, not the live callable surface or permission. '
    'Tool references support execution; they are not a user request or a list to recite. '
    'Apply the following current-context reset/updates before using any definition. '
    'Runtime instruction fields are also decoding defaults, superseded or removed by those updates. '
    'runtime.tool_contracts contains reference descriptions, including inactive tools. '
    'Only runtime.capability.available_tools declares the current callable surface; descriptors grant no authority. '
    'The original live tool registry, capability lease, policy and sandbox still decide execution.\n'
)
# Existing generic instructions belong before task-specific evidence too. This
# moves text already sent on every request; it adds no padding or task facts.
# Keep live capabilities, user controls, interaction state and obligations out
# of this baseline. Changed/removed guidance still uses the exact delta path.
_SEED_KEYS = {'runtime.tool_contracts', 'runtime.tool_transport',
    'runtime.instructions', 'runtime.snapshot_rules', 'runtime.phase_guidance',
    'runtime.advancement_guidance'}


class ContextSections(str):
    """Legacy text plus explicit section data; never parse arbitrary prose."""
    def __new__(cls, text, sections, *, scope):
        value = super().__new__(cls, text)
        value.sections = deepcopy(sections)
        value.scope = str(scope)
        return value


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _path(parent, key):
    return parent + '/' + str(key).replace('~', '~0').replace('/', '~1')


def _seed_message(values):
    return {'role':'user', 'message_type':'host_context_seed', 'host_authority':True,
        'context_purpose':'tool_reference',
        'content':_SEED_HEADER + _encode({'format':'gitgo.context.seed.v1','values':values}),
        'context_seed':deepcopy(values)} if values else None


def _seed(session, context, route):
    stored = getattr(session,'provider_context_seed',{})
    if (not isinstance(stored,dict) or stored.get('scope') != context.scope or stored.get('route') != route
        or not isinstance(stored.get('values'),dict) or set(stored['values']) - _SEED_KEYS):
        stored = {'scope':context.scope,'route':route,
            'values':{key:deepcopy(context.sections[key]) for key in sorted(_SEED_KEYS) if key in context.sections}}
        session.provider_context_seed = stored
    return stored['values']


def durable_cache(state):
    """Store each derived patch once; do not checkpoint duplicate full views."""
    if not isinstance(state,dict) or state.get('format') != FORMAT:
        return {}
    try:
        result = {key:deepcopy(state[key]) for key in ('format','epoch','route','scope','snapshot_digest','seed_anchor')}
        result['records'] = [{**{key:r[key] for key in ('anchor','history_digest')},
            'message':{key:deepcopy(value) for key,value in r['message'].items() if key != 'context_snapshot'}}
            for r in state['records']]
        return result
    except (KeyError,TypeError):
        return {}


def restore_cache(state, seed):
    """A damaged optional presentation cache falls back to fresh assembly."""
    if not state:
        return {}
    try:
        if (state['format'] != FORMAT or state['scope'] != seed['scope'] or state['route'] != seed['route']
            or not isinstance(seed['values'],dict) or set(seed['values']) - _SEED_KEYS):
            return {}
        result = deepcopy(state)
        result['seed_message'] = _seed_message(seed['values'])
        for record in result['records']:
            message = record['message']
            if message['role'] != 'user' or message['message_type'] != 'host_context_update' or message.get('host_authority') is not True:
                return {}
            message['context_snapshot'] = json.loads(message['content'][message['content'].index('{'):])
            if message['context_snapshot']['format'] != FORMAT:
                return {}
        messages = ([result['seed_message']] if result['seed_message'] else []) + [r['message'] for r in result['records']]
        result['snapshot'] = decode_records(messages)
        if deterministic_hash(result['snapshot']) != result['snapshot_digest']:
            return {}
        return result
    except (KeyError,TypeError,ValueError,IndexError):
        return {}


def changes(before, after, path=''):
    """Exact structural delta; choose smaller array patches without guessing ids.

    Array indices describe positions, never semantic identity. Tail removals
    are descending so retained indices remain valid; appended sets are ascending.
    The choice changes representation only, not any reconstructed current value.
    """
    if type(before) is dict and type(after) is dict:
        sets, removes = [], []
        for key in sorted(before.keys() - after.keys()):
            removes.append(_path(path, key))
        for key in sorted(after):
            child = _path(path, key)
            if key not in before:
                sets.append({'path':child, 'value':deepcopy(after[key])})
            else:
                added, removed = changes(before[key], after[key], child)
                sets.extend(added)
                removes.extend(removed)
        return sets, removes
    if type(before) is list and type(after) is list:
        sets, removes = [], []
        for index in range(min(len(before), len(after))):
            added, removed = changes(before[index], after[index], _path(path, index))
            sets.extend(added)
            removes.extend(removed)
        removes.extend(_path(path, index) for index in range(len(before) - 1, len(after) - 1, -1))
        sets.extend({'path': _path(path, index), 'value': deepcopy(after[index])}
            for index in range(len(before), len(after)))
        if not sets and not removes:
            return [], []
        replacement = [{'path': path, 'value': deepcopy(after)}]
        if len(_encode({'set': sets, 'remove': removes})) < len(_encode({'set': replacement, 'remove': []})):
            return sets, removes
        return replacement, []
    if type(before) is not type(after) or before != after:
        return [{'path':path, 'value':deepcopy(after)}], []
    return [], []


def apply_changes(snapshot, sets, removes):
    """Shared mechanical decoder for observation and round-trip verification."""
    result = deepcopy(snapshot)
    def key(container, token, *, append=False):
        if type(container) is dict:
            return token
        if type(container) is not list or not token.isascii() or not token.isdecimal() or str(int(token)) != token:
            raise ValueError('Invalid current-context path')
        index = int(token)
        if index >= len(container) + int(append):
            raise ValueError('Current-context array index out of bounds')
        return index

    def locate(path):
        if not isinstance(path, str) or not path.startswith('/'):
            raise ValueError('Current-context JSON Pointer required')
        parts = [p.replace('~1','/').replace('~0','~') for p in path.split('/')[1:]]
        parent = result
        for part in parts[:-1]:
            parent = parent[key(parent, part)]
        return parent, parts[-1]

    for path in removes:
        parent, token = locate(path)
        del parent[key(parent, token)]
    for entry in sets:
        if entry['path'] == '':
            result = deepcopy(entry['value'])
            continue
        parent, token = locate(entry['path'])
        position = key(parent, token, append=True)
        if type(parent) is list and position == len(parent):
            parent.append(deepcopy(entry['value']))
        else:
            parent[position] = deepcopy(entry['value'])
    return result


def _valid(state, *, epoch, route, scope, history):
    try:
        if (state['format'], state['epoch'], state['route'], state['scope']) != (FORMAT, epoch, route, scope):
            return False
        last = state['records'][-1]
        return (0 <= last['anchor'] <= len(history)
            and last['history_digest'] == deterministic_hash(history[:last['anchor']])
            and state['snapshot_digest'] == deterministic_hash(state['snapshot'])
            and all(0 <= a['anchor'] <= b['anchor'] <= len(history)
                for a,b in zip(state['records'],state['records'][1:]))
            and all(json.loads(r['message']['content'][r['message']['content'].index('{'):]) == r['message']['context_snapshot']
                for r in state['records'])
            and decode_records(([state['seed_message']] if state.get('seed_message') else [])
                + [r['message'] for r in state['records']]) == state['snapshot'])
    except (KeyError, TypeError, IndexError, ValueError):
        return False


def project(session, history, context, *, route):
    """Insert cacheable observations before their actual response boundary.

    Repeated trace/transport reads are idempotent. A changed history prefix,
    task, epoch, or route rebases from current authoritative assembly inputs.
    Bounded cache rebasing may lose cache reuse, never context information.
    """
    with session._mechanism_lock:
        state = session.provider_context_cache
        epoch, scope = session.context_epoch, context.scope
        seed = _seed(session,context,route)
        if state.get('seed_message') != _seed_message(seed):
            state = {}
        if not _valid(state, epoch=epoch, route=route, scope=scope, history=history):
            anchor = 0
            while anchor < len(history) and history[anchor].get('role') == 'system' and history[anchor].get('message_type') == 'compiled_system_prompt':
                anchor += 1
            state = {'format':FORMAT, 'epoch':epoch, 'route':route, 'scope':scope,
                'snapshot':deepcopy(seed), 'snapshot_digest':deterministic_hash(seed), 'records':[],
                'seed_message':_seed_message(seed), 'seed_anchor':anchor}
        sets, removes = changes(state['snapshot'], context.sections)
        if not state['records'] or sets or removes:
            reset = not state['records']
            patch = {'format':FORMAT, 'scope':scope, 'epoch':epoch, 'reset':reset, 'set':sets, 'remove':removes}
            body = (_HEADER if reset else '[HOST CONTEXT UPDATE]\n') + _encode(patch)
            # Only derived observations are rebased. Durable conversation and
            # every current value remain intact; no padding to inflate hits.
            size = sum(len(r['message']['content']) for r in state['records']) + len(body)
            if len(state['records']) >= 128 or size > max(65536, 2 * len(_encode(context.sections))):
                state['records'] = []
                sets,removes = changes(seed,context.sections)
                patch.update(reset=True, set=sets, remove=removes)
                body = _HEADER + _encode(patch)
            record = {'anchor':len(history), 'history_digest':deterministic_hash(history),
                'message':{'role':'user', 'content':body, 'message_type':'host_context_update',
                    'context_purpose':'runtime_snapshot',
                    'host_authority':True, 'context_snapshot':deepcopy(patch)}}
            # Multiple assemblers may update one unstarted boundary; replace
            # that boundary instead of accumulating unsent intermediate views.
            if state['records'] and state['records'][-1]['anchor'] == len(history):
                if len(state['records']) == 1:
                    sets,removes = changes(seed,context.sections)
                    patch.update(reset=True, set=sets, remove=removes)
                else:
                    previous = decode_records(([state['seed_message']] if state['seed_message'] else [])
                        + [r['message'] for r in state['records'][:-1]])
                    sets, removes = changes(previous, context.sections)
                    patch.update(reset=False, set=sets, remove=removes)
                record['message']['content'] = (_HEADER if patch['reset'] else '[HOST CONTEXT UPDATE]\n') + _encode(patch)
                record['message']['context_snapshot'] = deepcopy(patch)
                state['records'][-1] = record
            else:
                state['records'].append(record)
            state['snapshot'] = deepcopy(context.sections)
            state['snapshot_digest'] = deterministic_hash(context.sections)
        session.provider_context_cache = state
        result, offset = [], 0
        if state['seed_message']:
            result.extend(history[:state['seed_anchor']])
            result.append(deepcopy(state['seed_message']))
            offset = state['seed_anchor']
        for record in state['records']:
            result.extend(history[offset:record['anchor']])
            result.append(deepcopy(record['message']))
            offset = record['anchor']
        result.extend(history[offset:])
        result.append({'role':'user', 'message_type':'host_dynamic_envelope', 'host_authority':True,
            'context_purpose':'snapshot_identity',
            'content':'[HOST CURRENT CONTEXT]\n' + _encode({'format':FORMAT, 'scope':scope, 'epoch':epoch,
                'snapshot_digest':state['snapshot_digest']})})
        return result


def decode_records(messages):
    """Recover the exact current snapshot from genuine projected messages."""
    snapshot, seed = {}, {}
    for message in messages:
        if message.get('message_type') == 'host_context_seed':
            seed = deepcopy(message['context_seed'])
            continue
        patch = message.get('context_snapshot')
        if message.get('message_type') != 'host_context_update' or not patch:
            continue
        if patch['reset']:
            snapshot = deepcopy(seed)
        snapshot = apply_changes(snapshot, patch['set'], patch['remove'])
    return snapshot
