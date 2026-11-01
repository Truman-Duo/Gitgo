"""Preserve recorded native tool transactions across context garbage collection.

This projection never invokes tools or manufactures calls/results. Old broken
epochs can recover their exact pair from the durable epoch archive; ambiguity
or a missing source is an explicit error, not an invitation to replay effects.
"""
from copy import deepcopy


def complete_transactions(messages, states, archives, call_ids):
    selected = list(messages)
    merged = dict(states)
    tools = [m for m in selected if m.get('provider_visible') is not False and m.get('role') == 'tool' and m.get('tool_call_id')]
    heads = [m for m in selected if m.get('provider_visible') is not False and m.get('role') == 'assistant' and
             call_ids(states.get(m.get('provider_state_id'), {}) or m.get('provider_state') or m)]
    if not tools and not heads:
        return selected, merged
    selected_ids = {m['tool_call_id'] for m in tools}
    head_ids = set().union(*(call_ids(states.get(m.get('provider_state_id'), {}) or
        m.get('provider_state') or m) for m in heads))
    if selected_ids == head_ids:
        return selected, merged

    # Read actual contiguous assistant batches. Archives may contain successive
    # snapshots of the same batch; prefer the one with all recorded receipts.
    batches = {}
    for source in [*archives, {'messages': messages, 'provider_state': states}]:
        history, state_map = source.get('messages', []), source.get('provider_state', {})
        for index, head in enumerate(history):
            if head.get('role') != 'assistant' or head.get('provider_visible') is False:
                continue
            state = state_map.get(head.get('provider_state_id'), {}) or head.get('provider_state') or head
            ids = call_ids(state)
            if not ids:
                continue
            end = index + 1
            while end < len(history) and history[end].get('role') != 'assistant':
                end += 1
            tail = history[index + 1:end]
            outputs = [m for m in tail if m.get('role') == 'tool' and m.get('tool_call_id') in ids
                       and m.get('provider_visible') is not False]
            identity = head.get('provider_state_id') or ('inline', tuple(sorted(ids)))
            prior = batches.get(identity)
            if prior and (prior['state'] != state or prior['head'] != head):
                raise ValueError('PROVIDER_TOOL_TRANSACTION_SOURCE_AMBIGUOUS')
            if prior is None or len(outputs) > len(prior['outputs']):
                batches[identity] = {'head': head, 'state': state, 'ids': ids,
                    'outputs': outputs, 'span': history[index:end]}

    needed = {}
    for item in tools + heads:
        state_id = item.get('provider_state_id')
        candidates = ([batches[state_id]] if state_id in batches else [b for b in batches.values()
            if item.get('tool_call_id') in b['ids']])
        if not candidates and item in heads:
            candidates = [b for b in batches.values() if b['head'] == item]
        if len(candidates) != 1:
            raise ValueError('PROVIDER_TOOL_TRANSACTION_SOURCE_' + ('MISSING' if not candidates else 'AMBIGUOUS'))
        batch = candidates[0]
        key = batch['head'].get('provider_state_id') or tuple(sorted(batch['ids']))
        needed[key] = batch

    for batch in needed.values():
        head, outputs = batch['head'], batch['outputs']
        present = [i for i, m in enumerate(selected) if m in batch['span']]
        state_id = head.get('provider_state_id')
        if state_id and batch['state'] is not head:
            merged[state_id] = deepcopy(batch['state'])
        # Restore the original batch's exact output order, keeping intervening
        # Host deltas once. No new effect or receipt is created.
        insertion = min(present)
        selected = [m for m in selected if m != head and not
            (m.get('role') == 'tool' and m.get('tool_call_id') in batch['ids'])]
        selected[insertion:insertion] = deepcopy([head, *outputs])
    return selected, merged
