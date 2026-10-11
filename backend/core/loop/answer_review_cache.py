"""Reuse a validated unresolved interpretation only for identical semantic input.

The cache never classifies new text, completes a gate, or grants authority.
Changed questions, dependencies, contracts, evidence or steering miss the cache.
"""
import copy
import hashlib
import json
from .alignment_evidence import answer_digest, source_answer
from .engineering_workflow import EngineeringWorkflow


def _input(process, payload, answer, *, published_question=None):
    task = process.active_task_id or process.process_id
    workflow = EngineeringWorkflow(process)
    state = workflow._load()
    node = next((n for n in state.get('plan', {}).get('nodes', []) if n['id'] == answer.get('node_id')), None)
    request = next((e for e in reversed(process.session.host_ledger)
                    if e.get('event') == 'user_decision_requested' and e.get('decision_id') == answer.get('decision_id')
                    and e.get('task_id') == task and e.get('process_id') == process.process_id), None)
    if node is None or request is None or request.get('kind') != 'clarification':
        return None
    questions = request.get('questions') or []
    if len(questions) > 1 or (questions and questions[0].get('node_id') != node['id']):
        return None
    source = source_answer(process.session.host_ledger, task, answer['decision_id'], node['id'] if questions else '')
    if not source or source.get('answer') != answer.get('answer'):
        return None
    context, _ = process.read_context_snapshot()
    alignment = context.get('alignment_runtime') or {}
    from .task_contract import semantic_contract
    from .alignment_routing import POLICY_VERSION
    shape = published_question or (questions[0] if questions else request)
    data = {'cache_schema': 1, 'policy': POLICY_VERSION, 'task': task, 'process': process.process_id,
            'kind': process.task_kind, 'node': node['id'], 'answer': answer_digest(source['answer']),
            'question': {'kind': shape.get('kind', 'clarification'), 'state_topic': node['state_topic'],
                         'question': shape.get('question'), 'why_user_must_decide': shape.get('why_user_must_decide'),
                         'options': shape.get('options') or [], 'allow_free_form': shape.get('allow_free_form', True)},
            'plan': state['plan'], 'records': {k: v for k, v in state.get('records', {}).items() if k != node['id']},
            'other_question_overrides': {k: v for k, v in state.get('request_overrides', {}).items() if k != node['id']},
            'contract': semantic_contract(context.get('task_contract') or {}), 'constraints': process.task_constraints,
            'request': alignment.get('request'), 'scope': alignment.get('scope'), 'defaults': alignment.get('assumptions'),
            'updates': payload.get('user_updates'), 'history': payload.get('historical_decisions'),
            'conversation': payload.get('conversation_context'), 'description': process.task_description,
            'governance': {k: context.get(k) for k in ('brief', 'signals', 'context_policy')},
            'receipts': [r for r in process.tool_receipts if r.get('task_id') == task]}
    key = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return key, node, source


def remember_unresolved(process, payload, assessment):
    """Called only after the normal assessment compiler committed successfully."""
    answers = payload.get('actual_answers') or []
    if len(answers) != 1 or assessment.get('status') not in {'needs_input', 'waiting'}:
        return
    pending = process.pending_decision or {}
    next_questions = pending.get('questions') or [pending]
    if (len(next_questions) != 1 or pending.get('task_id') != (process.active_task_id or process.process_id)
            or pending.get('process_id') != process.process_id
            or next_questions[0].get('node_id') != answers[0]['node_id']):
        return
    # The assessment validated this residual question against the old answer.
    # Bind the cache to that actual published question, not to the wording it
    # replaced. New answers or further question edits still miss the fingerprint.
    identity = _input(process, payload, answers[0], published_question=next_questions[0])
    if identity is None:
        return
    key, node, source = identity
    interpreted = next((e for e in reversed(process.session.host_ledger)
                        if e.get('event') == 'user_decision_interpreted' and e.get('task_id') == source['task_id']
                        and e.get('node_id') == node['id'] and e.get('decision_id') == source['decision_id']
                        and e.get('answer_digest') == answer_digest(source['answer'])), None)
    if not interpreted or interpreted.get('disposition') not in {'partial', 'deferred'}:
        return
    questions = [q for q in assessment.get('questions', [])
                 if q.get('node_id') == node['id'] or q.get('state_topic') == node['state_topic']]
    if interpreted['disposition'] == 'partial' and len(questions) != 1:
        return
    proposal = {k: copy.deepcopy(assessment.get(k)) for k in ('status', 'request_frame', 'reason')}
    proposal.update(questions=copy.deepcopy(questions), assumptions=[], interpretations=[{
        k: interpreted[k] for k in ('node_id', 'decision_id', 'disposition', 'value', 'quote', 'remaining', 'choice_key', 'source_node_id')
        if k in interpreted}])
    process.session.host_ledger.append({'event': 'alignment_unresolved_cached',
        'task_id': source['task_id'], 'process_id': process.process_id, 'input_key': key,
        'source_decision_id': source['decision_id'], 'node_id': node['id'],
        'answer_digest': answer_digest(source['answer']), 'proposal': proposal})


def reuse_unresolved(process, payload, runtime):
    if (payload.get('stage') != 'admission' or process.task_kind not in {'answer', 'plan'}
            or process.pending_decision or len(payload.get('actual_answers') or []) != 1):
        return None
    answer = payload['actual_answers'][0]
    identity = _input(process, payload, answer)
    if identity is None:
        return None
    key, node, source = identity
    if EngineeringWorkflow(process).status().get('question_frontier', [])[:1] != [node['id']]:
        return None
    ledger = process.session.host_ledger
    cached = next((e for e in reversed(ledger[-256:]) if e.get('event') == 'alignment_unresolved_cached'
                   and e.get('task_id') == source['task_id'] and e.get('process_id') == process.process_id
                   and e.get('input_key') == key and e.get('source_decision_id') != source['decision_id']), None)
    if cached is None:
        return None
    proposal = copy.deepcopy(cached['proposal'])
    if (not isinstance(proposal, dict) or proposal.get('status') not in {'needs_input', 'waiting'}
            or not isinstance(proposal.get('interpretations'), list) or len(proposal['interpretations']) != 1
            or not isinstance(proposal['interpretations'][0], dict)):
        raise ValueError('invalid unresolved interpretation cache')
    prior = next((e for e in reversed(ledger) if e.get('event') == 'user_decision_interpreted'
                  and e.get('task_id') == source['task_id'] and e.get('process_id') == process.process_id
                  and e.get('node_id') == node['id']
                  and e.get('decision_id') == cached['source_decision_id']
                  and e.get('answer_digest') == answer_digest(source['answer'])), None)
    old = proposal['interpretations'][0]
    prior_source = source_answer(ledger, source['task_id'], cached['source_decision_id'],
                                 (prior or {}).get('source_node_id', ''))
    if not prior_source or answer_digest(prior_source['answer']) != answer_digest(source['answer']):
        return None
    if not prior or prior.get('disposition') not in {'partial', 'deferred'} or any(
            old.get(k) != prior.get(k) for k in ('disposition', 'value', 'quote', 'remaining')):
        return None
    old['decision_id'] = source['decision_id']
    if source.get('answers'):
        old['source_node_id'] = node['id']
    else:
        old.pop('source_node_id', None)
    return runtime.apply(proposal, payload)
