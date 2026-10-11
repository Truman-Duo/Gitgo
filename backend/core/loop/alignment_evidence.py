"""User input is evidence, not automatically a resolved requirement.

Semantic interpretation proposes a value; Host binds it to the exact current
question and immutable user receipt. Deferred/partial input never closes gates.
"""
from __future__ import annotations

import hashlib
import json

CONTROL_DECISION_KINDS = frozenset({'permission', 'recovery', 'completion_exception', 'context_force_compaction'})


def answer_digest(answer):
    return hashlib.sha256(str(answer).encode('utf-8')).hexdigest()


def alignment_input_digest(process, *, contract_override=None):
    task_id = process.active_task_id or process.process_id
    controls = CONTROL_DECISION_KINDS
    control_ids = {e.get('decision_id') for e in process.session.host_ledger
                   if e.get('task_id') == task_id and e.get('kind') in controls}
    events = [e for e in process.session.host_ledger if e.get('task_id') == task_id and e.get('event') in
              {'user_decision_requested', 'user_decision_received', 'user_decision_interpreted', 'user_decision_cancelled'}
              and e.get('decision_id') not in control_ids]
    context, _ = process.read_context_snapshot()
    state = context.get('alignment_runtime') or {}
    from .alignment_routing import POLICY_VERSION
    from .task_contract import semantic_contract
    contract = semantic_contract(contract_override if contract_override is not None else context.get('task_contract') or {})
    updates = [{k: m.get(k) for k in ('mailbox_message_id', 'content')}
               for m in process.session.messages[state.get('message_start', len(process.session.messages)):]
               if m.get('mailbox_message_id') and m.get('message_type') == 'conversation']
    return hashlib.sha256(json.dumps({'events': events, 'request': state.get('request'), 'scope': state.get('scope'),
                                     'assumptions': state.get('assumptions'), 'constraints': process.task_constraints, 'updates': updates,
                                     'policy_version': POLICY_VERSION, 'contract': contract,
                                     'workflow_plan': (context.get('engineering_workflow') or {}).get('plan'),
                                     }, sort_keys=True).encode()).hexdigest()


def resolved_answer(ledger, answer, topic):
    for item in reversed(ledger):
        if (item.get('event') == 'user_decision_interpreted'
                and item.get('task_id') == answer.get('task_id')
                and item.get('decision_id') == answer.get('decision_id')
                and item.get('state_topic') == topic
                and item.get('answer_digest') == answer_digest(answer.get('answer', ''))):
            return item if item.get('disposition') in {'confirmed', 'delegated'} else None
    # An explicit selection is a user-owned value; free-form discussion is not.
    if answer.get('selected_label') and answer.get('kind') != 'clarification':
        return {**answer, 'value': answer['selected_label'], 'disposition': 'confirmed'}
    return None


def source_answer(ledger, task_id, decision_id, source_node_id=''):
    event = next((e for e in reversed(ledger) if e.get('event') == 'user_decision_received'
                  and e.get('task_id') == task_id and e.get('decision_id') == decision_id), None)
    if not event:
        return None
    if event.get('kind') in CONTROL_DECISION_KINDS:
        return None
    if event.get('answers'):
        item = event['answers'].get(source_node_id)
        return {**event, **item} if item else None
    return event


def exploratory_scope(process):
    context, _ = process.read_context_snapshot()
    state = context.get('alignment_runtime') or {}
    scope = state.get('scope') or {}
    task_id = process.active_task_id or process.process_id
    if (state.get('task_id') != task_id or state.get('process_id', process.process_id if not process.parent_id else '') != process.process_id
            or process.task_kind not in {'answer', 'plan'} or scope.get('mode') != 'exploration'):
        return False
    source = source_answer(process.session.host_ledger, task_id, scope.get('decision_id'), scope.get('source_node_id', ''))
    return bool(source and scope.get('answer_digest') == answer_digest(source['answer'])
                and scope.get('request') and scope['request'] == scope.get('quote') and scope['quote'] in source['answer'])


def interpret_answer(process, node, answer, proposal, *, publish=True):
    if not isinstance(proposal, dict) or len(json.dumps(proposal).encode()) > 16_000:
        raise ValueError('answer interpretation must be a bounded object')
    disposition = proposal.get('disposition')
    if disposition not in {'confirmed', 'partial', 'deferred', 'delegated'}:
        raise ValueError('invalid answer disposition')
    quote = str(proposal.get('quote') or '').strip()
    value = str(proposal.get('value') or '').strip()
    if (quote and quote not in str(answer.get('answer', ''))) or (not quote and (value or disposition in {'confirmed', 'delegated'})):
        raise ValueError('interpretation requires an exact quote from the current user answer')
    if disposition in {'confirmed', 'delegated'} and not value:
        raise ValueError('a resolved answer requires a semantic value')
    if disposition in {'partial', 'deferred'} and not str(proposal.get('remaining') or '').strip():
        raise ValueError('unresolved input requires the remaining issue')
    event = {'event': 'user_decision_interpreted', 'task_id': process.active_task_id or process.process_id,
             'process_id': process.process_id, 'decision_id': answer['decision_id'],
             'state_topic': node['state_topic'], 'node_id': node['id'],
             'answer_digest': answer_digest(answer['answer']), 'disposition': disposition,
             'value': value, 'quote': quote, 'remaining': str(proposal.get('remaining') or '')}
    if proposal.get('source_node_id'):
        event['source_node_id'] = proposal['source_node_id']
    if proposal.get('choice_key'):
        choice = str(proposal['choice_key']).strip()
        if not choice or len(choice) > 200:
            raise ValueError('conditional choice requires a bounded canonical key')
        event['choice_key'] = choice
    if publish:
        process.session.host_ledger.append(event)
    return event
