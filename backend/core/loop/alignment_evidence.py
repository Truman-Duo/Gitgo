"""User input is evidence, not automatically a resolved requirement.

Semantic interpretation proposes a value; Host binds it to the exact current
question and immutable user receipt. Deferred/partial input never closes gates.
"""
from __future__ import annotations

import hashlib
import json

CONTROL_DECISION_KINDS = frozenset({'permission', 'recovery', 'completion_exception', 'context_force_compaction', 'manual_execution', 'runtime_checkpoint'})


def answer_digest(answer):
    return hashlib.sha256(str(answer).encode('utf-8')).hexdigest()


def alignment_input_digest(process, *, contract_override=None, boundary_subjects=None):
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
    from .task_maintenance import alignment_basis
    from backend.core.mechanism_runtime.semantic_projection import alignment_basis as harness_basis
    contract = semantic_contract(contract_override if contract_override is not None else context.get('task_contract') or {})
    updates = [{k: m.get(k) for k in ('mailbox_message_id', 'content')}
               for m in process.session.messages[state.get('message_start', len(process.session.messages)):]
               if m.get('mailbox_message_id') and m.get('message_type') == 'conversation']
    return hashlib.sha256(json.dumps({'events': events, 'request': state.get('request'), 'scope': state.get('scope'),
                                     'assumptions': state.get('assumptions'), 'constraints': process.task_constraints, 'updates': updates,
                                     'policy_version': POLICY_VERSION, 'contract': contract,
                                     'boundary_subjects': boundary_subjects or [],
                                     'task_maintenance': alignment_basis(process),
                                     'harness_semantics': harness_basis(process),
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
    label = answer.get('selected_label')
    if label:
        # A round's envelope is clarification even when its actual child is a
        # finite choice. Recover that meaning from the published card, never
        # from model-supplied interpretation fields or the wrapper's kind.
        request = next((e for e in reversed(ledger)
                        if e.get('event') == 'user_decision_requested'
                        and (answer.get('task_id') is None or e.get('task_id') == answer.get('task_id'))
                        and e.get('decision_id') == answer.get('decision_id')), {})
        if any(e.get('event') == 'user_decision_cancelled'
               and e.get('decision_id') == answer.get('decision_id') for e in ledger):
            return None
        questions = request.get('questions') or [request]
        question = next((q for q in questions if q.get('state_topic') == topic), {})
        if (question.get('kind') in {'choice', 'preference', 'direction', 'verification', 'checkpoint'}
                and any(o.get('label') == label for o in question.get('options') or [])):
            return {**answer, 'value': label, 'choice_key': label, 'disposition': 'confirmed'}
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


def business_answer_receipts(ledger, task_id):
    """Project exact business replies, including legacy cards outside the DAG.

    Received text remains evidence to interpret, never a confirmed value or a
    permission. Controls, cancelled cards and other tasks stay excluded.
    """
    from .decision_support import SEMANTIC_DECISION_KINDS
    requests = {e.get('decision_id'): e for e in ledger
                if e.get('event') == 'user_decision_requested' and e.get('task_id') == task_id}
    cancelled = {e.get('decision_id') for e in ledger
                 if e.get('event') == 'user_decision_cancelled' and e.get('task_id') == task_id}
    result = []
    for event in ledger:
        if event.get('event') != 'user_decision_received' or event.get('task_id') != task_id:
            continue
        request = requests.get(event.get('decision_id'), {})
        if request.get('kind') not in SEMANTIC_DECISION_KINDS or event.get('decision_id') in cancelled:
            continue
        if event.get('answers'):
            for question in request.get('questions') or []:
                item = event['answers'].get(question.get('node_id'))
                if item and isinstance(item.get('answer'), str):
                    result.append({'decision_id': event['decision_id'], 'source_node_id': question['node_id'],
                        'state_topic': question['state_topic'], 'question': question['question'], 'answer': item['answer']})
        elif isinstance(event.get('answer'), str):
            result.append({'decision_id': event['decision_id'], 'state_topic': request.get('state_topic', ''),
                'question': request.get('question', ''), 'answer': event['answer']})
    return result


def exploratory_scope(process, *, admission=False):
    context, _ = process.read_context_snapshot()
    state = context.get('alignment_runtime') or {}
    scope = state.get('scope') or {}
    task_id = process.active_task_id or process.process_id
    if (state.get('task_id') != task_id or state.get('process_id', process.process_id if not process.parent_id else '') != process.process_id
            or (not admission and (process.task_kind not in {'answer', 'plan'} or scope.get('admission_only')))
            or scope.get('mode') != 'exploration'):
        return False
    source = source_answer(process.session.host_ledger, task_id, scope.get('decision_id'), scope.get('source_node_id', ''))
    controls = CONTROL_DECISION_KINDS
    requests = {e.get('decision_id'): e for e in process.session.host_ledger
                if e.get('event') == 'user_decision_requested' and e.get('task_id') == task_id}
    latest = next((e for e in reversed(process.session.host_ledger)
                   if e.get('event') == 'user_decision_received' and e.get('task_id') == task_id
                   and requests.get(e.get('decision_id'), {}).get('kind', e.get('kind')) not in controls), None)
    if not latest or latest.get('decision_id') != scope.get('decision_id'):
        return False
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
