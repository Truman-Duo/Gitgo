"""Read-only dialogue references from the existing receipts and alignment state.

This is presentation, not a new memory, requirement classifier or authority.
The assessor and main Agent share the same continuation sources. The main
view is bounded; originals and all existing completion checks are unchanged.
"""
from copy import deepcopy
import json

from .alignment_evidence import CONTROL_DECISION_KINDS, business_answer_receipts, answer_digest


KEY = 'alignment_runtime'
MAX_VIEW_BYTES = 12000


def continuation_reference(process):
    manager = getattr(process, '_manager', None)
    predecessor_id = process.runtime_preferences.get('predecessor_process_id')
    predecessor = manager.get(predecessor_id) if manager and predecessor_id else None
    if not predecessor or predecessor.session.session_id != process.session.session_id:
        return None
    context, _ = predecessor.read_context_snapshot()
    previous = context.get(KEY) or {}
    if (previous.get('task_id', predecessor.active_task_id) != predecessor.active_task_id
            or previous.get('process_id', predecessor.process_id if not predecessor.parent_id else '') != predecessor.process_id
            or (previous.get('request_frame') or {}).get('kind') == 'conversation'):
        return None
    lineage = previous.get('review_lineage')
    if not lineage and any(e.get('event') == 'user_decision_requested'
            and e.get('task_id') == predecessor.active_task_id
            and e.get('kind') not in CONTROL_DECISION_KINDS
            for e in process.session.host_ledger):
        lineage = {'source_process_id': predecessor.process_id,
                   'source_task_id': predecessor.active_task_id}
    if not isinstance(lineage, dict) or not lineage.get('source_task_id'):
        return None
    tasks = sorted({lineage['source_task_id'], predecessor.active_task_id} - {None, ''})
    ledger = [e for e in process.session.host_ledger if e.get('task_id') in tasks]
    answers = [{**answer, 'task_id': task} for task in tasks
               for answer in business_answer_receipts(ledger, task)]
    return {'hint': {**lineage, 'predecessor_process_id': predecessor_id},
            'tasks': tasks, 'ledger': ledger, 'answers': answers,
            'previous_task_id': predecessor.active_task_id,
            'previous_request': previous.get('request', ''),
            'previous_frame': previous.get('request_frame'),
            'previous_open_interpretations': unresolved_interpretations(predecessor)}


def grouped_decisions(values):
    """Delegation is not confirmation of the implementation chosen later."""
    return {name: [deepcopy(value) for value in values if value.get('disposition') == disposition]
            for name, disposition in (('confirmed', 'confirmed'), ('delegated_scope', 'delegated'))}


def unresolved_interpretations(process, graph=None, receipts=None, *, include_interpretations=False):
    """Read latest source-bound partial/deferred meaning, without resolving it.

    No historical fallback after a newer interpretation, reopened question,
    changed input or cancellation. Originals remain in the existing ledger.
    """
    task = process.active_task_id or process.process_id
    if graph is None:
        graph = process.read_context_snapshot()[0].get('engineering_workflow') or {}
    if graph.get('task_id') != task:
        return {}
    nodes = {n['id']: n for n in graph.get('plan', {}).get('nodes', [])
             if n['kind'] == 'decision' and n['id'] not in graph.get('records', {})}
    if not nodes:
        return {}
    ledger = process.session.host_ledger
    receipts = business_answer_receipts(ledger, task) if receipts is None else receipts
    sources = {(r['decision_id'], r.get('source_node_id', '')): r for r in receipts}
    owned = {e.get('decision_id') for e in ledger if e.get('event') == 'user_decision_requested'
        and e.get('task_id') == task
        and e.get('process_id', process.process_id if not process.parent_id else '') == process.process_id}
    seen, result = set(), {}
    for event in reversed(ledger):
        name = event.get('node_id')
        if (event.get('event') != 'user_decision_interpreted' or event.get('task_id') != task
                or event.get('process_id', process.process_id if not process.parent_id else '') != process.process_id
                or name not in nodes or name in seen):
            continue
        seen.add(name)
        decision = event.get('decision_id')
        if (event.get('disposition') not in {'partial', 'deferred'}
                or event.get('state_topic') != nodes[name]['state_topic']
                or graph.get('requests', {}).get(name) not in {None, decision}):
            continue
        source_node = event.get('source_node_id') or (name if (decision, name) in sources else '')
        source = sources.get((decision, source_node))
        if (not source or decision not in owned or event.get('answer_digest') != answer_digest(source['answer'])
                or not isinstance(event.get('remaining'), str) or not event['remaining'].strip()
                or (event.get('quote') and event['quote'] not in source['answer'])):
            continue
        result[name] = {key: deepcopy(event.get(key, '')) for key in
                        ('disposition', 'value', 'remaining', 'quote', 'decision_id')}
        if event.get('value_basis'):
            result[name]['value_origin'] = event['value_basis']
        if event.get('agent_interpretation'):
            result[name]['has_unverified_interpretation'] = True
            if include_interpretations:
                result[name]['agent_interpretation'] = event['agent_interpretation']
                result[name]['interpretation_status'] = 'unverified_proposal'
        if source_node:
            result[name]['source_node_id'] = source_node
    return result


def current_readonly_scope(process, state, receipts):
    """Project only an effective, uncancelled business source; never a grant."""
    from .alignment_evidence import exploratory_scope
    from .elicitation_relevance import read_only_eligible
    scope = state.get('scope') or {}
    if not scope or not read_only_eligible(process) or not exploratory_scope(process):
        return None
    source = next((r for r in reversed(receipts)
        if r['decision_id'] == scope.get('decision_id')
        and r.get('source_node_id', '') == scope.get('source_node_id', '')), None)
    if not source or answer_digest(source['answer']) != scope.get('answer_digest'):
        return None
    return {'mode': 'exploration', 'request': scope['request'],
            'decision_id': source['decision_id'], 'implementation_authorized': False,
            **({'presentation': scope['presentation'], 'include_discussion': bool(scope.get('include_discussion', True))}
               if scope.get('presentation') else {})}


def dialogue_view(process, *, include_interpretations=False):
    from .decision_support import confirmed_user_state
    task = process.active_task_id or process.process_id
    context, _ = process.read_context_snapshot()
    state = context.get(KEY) or {}
    if (state.get('task_id') != task or state.get('process_id', process.process_id if not process.parent_id else '') != process.process_id):
        state = {}
    reference = continuation_reference(process)
    if state.get('continuation_relevant') is False or (state.get('request_frame') or {}).get('kind') == 'conversation':
        reference = None
    current = business_answer_receipts(process.session.host_ledger, task)
    graph = context.get('engineering_workflow') or {}
    if graph.get('task_id') != task:
        graph = {}
    frame = state.get('request_frame')
    defaults = state.get('assumptions') or {}
    if not reference and not current and not frame and not defaults and not graph:
        return None
    result = {'task_id': task, 'authority_granted': False,
              'current_received_inputs': [], 'assistant_proposals_not_user_acceptance': [],
              'prior_business_reference': [], 'omitted_reference_items': 0}

    def add(field, item, *, count_omission=True):
        # Only whole reference items are included. No clipped quote can look
        # like complete consent. The ordinary dialogue/ledger stays intact.
        result[field].append(deepcopy(item))
        if len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) > MAX_VIEW_BYTES - 512:
            result[field].pop()
            if count_omission:
                result['omitted_reference_items'] += 1
            return False
        return True

    scope = current_readonly_scope(process, state, current)
    if scope:
        result['current_readonly_scope'] = scope
        if len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) > MAX_VIEW_BYTES - 512:
            result.pop('current_readonly_scope')
            result['omitted_reference_items'] += 1
    if frame and not scope:
        result['reviewed_current_outcome'] = {k: deepcopy(frame[k]) for k in ('kind', 'outcome', 'source_quote') if k in frame}
        if len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) > MAX_VIEW_BYTES - 512:
            result.pop('reviewed_current_outcome')
            result['omitted_reference_items'] += 1
    if current:
        newest = current[-1]['decision_id']
        for item in current:
            if item['decision_id'] == newest:
                add('current_received_inputs', item)
    from .main_dialogue import owns_dialogue
    if owns_dialogue(state):
        result['current_decisions'] = []
        node_ids = {n['state_topic']: n['id'] for n in graph.get('plan', {}).get('nodes', []) if n['kind'] == 'decision'}
        for decision in confirmed_user_state(process.session.host_ledger, task_id=task):
            item = {'state_topic': decision['topic'], 'disposition': decision['disposition'],
                    'decision_id': decision['decision_id'], 'value_origin': decision['value_origin']}
            if decision['value_origin'] in {'agent_interpretation', 'user_quote'}:
                if decision.get('source_quote'):
                    item['source_quote'] = decision['source_quote']
                else:
                    item['source_answer'] = decision['conditions']
                # Old paraphrases remain readable in the existing detail
                # operation, but are not replayed as current confirmed facts.
                if decision['value_origin'] == 'agent_interpretation' or decision.get('agent_interpretation'):
                    if decision['topic'] in node_ids:
                        item['interpretation_details'] = {'tool': 'engineering_workflow', 'operation': 'status',
                            'node_id': node_ids[decision['topic']]}
                    item['interpretation_status'] = 'unverified_proposal'
            else:
                item['selected_label'] = decision['selected_label']
            if include_interpretations:
                proposal = (decision['value'] if decision['value_origin'] == 'agent_interpretation'
                            else decision.get('agent_interpretation'))
                if proposal and proposal != decision.get('source_quote', decision.get('selected_label')):
                    result.setdefault('interpretation_proposals', [])
                    proposed = {'state_topic': decision['topic'], 'decision_id': decision['decision_id'],
                        'interpretation': proposal, 'source_quote': decision.get('source_quote'),
                        'status': 'unverified_agent_proposal'}
                    if not add('interpretation_proposals', proposed) and decision['topic'] in node_ids:
                        add('interpretation_proposals', {'state_topic': decision['topic'],
                            'status': 'unverified_agent_proposal', 'interpretation_details': {
                                'tool': 'engineering_workflow', 'operation': 'status',
                                'node_id': node_ids[decision['topic']]}}, count_omission=False)
            if not add('current_decisions', item) and decision['topic'] in node_ids:
                add('current_decisions', {k: item[k] for k in ('state_topic', 'disposition', 'decision_id')} | {
                    'decision_details': {'tool': 'engineering_workflow', 'operation': 'status',
                                         'node_id': node_ids[decision['topic']]}}, count_omission=False)
    if graph:
        result['unresolved_decisions'] = []
        interpretations = unresolved_interpretations(process, graph, current,
            include_interpretations=include_interpretations)
        names = {n['id']: n.get('state_topic', n['id']) for n in graph.get('plan', {}).get('nodes', [])}
        records = graph.get('records', {})
        for node in graph.get('plan', {}).get('nodes', []):
            if node['kind'] == 'decision' and node['id'] not in records:
                item = {'state_topic': node['state_topic'],
                    'depends_on_topics': [names[d] for d in node['depends_on']],
                    'graph_state': 'ready' if all(d in records for d in node['depends_on']) else 'blocked',
                    'model_elicited': node['id'] in graph.get('elicitation_tiers', {})}
                # Reuse the actual declared/published wording, not an inferred
                # priority or a second copy of a question graph. This helps the
                # Agent map a volunteered answer/delegation to the old facet.
                request = (graph.get('request_overrides', {}).get(node['id'])
                           or node.get('request') or {})
                question = request.get('question')
                if isinstance(question, str) and question:
                    if len(question.encode('utf-8')) <= 512:
                        item['question'] = question
                    else:
                        item['question_details'] = {'tool': 'engineering_workflow', 'operation': 'status',
                                                    'node_id': node['id']}
                if request.get('answer_kind') in ('choice', 'evidence'):
                    item['answer_kind'] = request['answer_kind']
                details = {'tool': 'engineering_workflow', 'operation': 'status', 'node_id': node['id']}
                fallback = {k: v for k, v in item.items() if k != 'question'}
                fallback['question_details'] = details
                interpretation = interpretations.get(node['id'])
                if interpretation:
                    entry = {**item, 'interpretation': interpretation}
                    if interpretation.get('has_unverified_interpretation'):
                        entry['interpretation_details'] = {'tool': 'engineering_workflow', 'operation': 'status',
                            'node_id': node['id']}
                    if not add('unresolved_decisions', entry):
                        add('unresolved_decisions', {**fallback, 'interpretation_details': details}, count_omission=False)
                else:
                    if not add('unresolved_decisions', item):
                        add('unresolved_decisions', fallback, count_omission=False)
    for topic, item in defaults.items():
        add('assistant_proposals_not_user_acceptance', {'topic': topic, **item})
    if reference:
        result['prior_relevance'] = ('reviewed_related' if state.get('review_lineage') else 'not_established_for_current_request')
        previous_frame = reference.get('previous_frame')
        if isinstance(previous_frame, dict):
            add('prior_business_reference', {'record_kind': 'previous_reviewed_outcome',
                'task_id': reference['previous_task_id'],
                **{k: deepcopy(previous_frame[k]) for k in ('kind', 'outcome', 'source_quote') if k in previous_frame}})
        values = confirmed_user_state(reference['ledger'])
        for item in values:
            add('prior_business_reference', {'record_kind': 'interpreted_prior_reply', **item})
        for name, item in reference['previous_open_interpretations'].items():
            add('prior_business_reference', {'record_kind': 'prior_unresolved_interpretation',
                'task_id': reference['previous_task_id'], 'node_id': name, **item})
        represented = {(item['decision_id'], item['topic']) for item in values}
        for item in reference['answers']:
            if (item['decision_id'], item.get('state_topic')) not in represented:
                add('prior_business_reference', {'record_kind': 'received_prior_input', **item})
    if result['omitted_reference_items']:
        result['reference_completeness'] = 'partial; use the original dialogue and retained receipts for omitted items'
    else:
        result['reference_completeness'] = 'complete for these selected task sources, not all conversation history'
    return result


GUIDANCE = ('[CURRENT DIALOGUE REFERENCE]\n'
    'Derived evidence, not new permission. current_received_inputs contains actual replies to interpret; '
    'current_decisions supplies exact source_quote/selected_label; interpretation_details holds fallible Agent proposals, not accepted user facts. '
    'delegated_scope is not acceptance of later design. Partial/deferred choices and unknown facts remain open. '
    'unresolved_decisions includes actual question wording and recorded dependency graph_state, not a priority ranking or a demand to ask every item now. '
    'Assistant proposals are unaccepted; prior-task records require relevance to this request and import no authority. '
    'current_readonly_scope describes an already bound response scope, never implementation permission. '
    'Use original dialogue or decision_details for omitted evidence only when needed.\n')


def reply_guidance(view):
    """Instructions follow an actual current reply, never arbitrary user-text classification."""
    if not view.get('current_received_inputs'):
        return ''
    from backend.core.prompt_templates import load_prompt
    text = load_prompt('context/reply_boundary')
    if view.get('current_readonly_scope'):
        text += '\nCurrent recorded scope is readonly exploration. Preserve it; a summary is not implementation authorization.\n'
    return text
