"""Source-bound relevance of model-elicited questions, never user consent.

This uses the existing workflow record/ledger and semantic assessment. It only
supports a read-only answer/plan; mutation admission still sees a non-complete
question. User-authored workflow obligations are not eligible.
"""
import copy
import hashlib
import json

from .alignment_evidence import business_answer_receipts, source_answer, answer_digest


class ElicitationRelevanceError(ValueError):
    def __init__(self, message):
        super().__init__(message)
        self.validation_details = {'code': 'QUESTION_RETIREMENT_INVALID'}


def read_only_eligible(process):
    # Adaptive supervisors keep task_kind=answer while executing an admitted
    # self_execute/delegate contract. Task kind alone is not a read-only proof.
    context, _ = process.read_context_snapshot()
    mode = (context.get('task_contract') or {}).get('execution_mode')
    return process.task_kind in {'answer', 'plan'} and mode not in {'self_execute', 'delegate', 'either'}


def receipt_epoch(process):
    receipts = business_answer_receipts(process.session.host_ledger,
        process.active_task_id or process.process_id)
    return hashlib.sha256(json.dumps(receipts, sort_keys=True).encode()).hexdigest()


def valid_retirement(process, node, record, state):
    certificate = record.get('elicitation_retirement')
    if (not isinstance(certificate, dict) or not read_only_eligible(process)
            or node['id'] not in state.get('elicitation_tiers', {})
            or certificate.get('receipt_epoch') != receipt_epoch(process)):
        return False
    source = source_answer(process.session.host_ledger, state['task_id'],
        certificate.get('decision_id'), certificate.get('source_node_id', ''))
    return bool(source and source.get('process_id', process.process_id) == process.process_id
        and source.get('answer') and answer_digest(source['answer']) == certificate.get('source_digest')
        and isinstance(certificate.get('quote'), str) and certificate['quote'] in source['answer'] and any(
            e.get('event') == 'alignment_question_retired' and e.get('task_id') == state['task_id']
            and e.get('node_id') == node['id'] and e.get('certificate') == certificate
            for e in process.session.host_ledger))


def retire_questions(workflow, proposals):
    if not isinstance(proposals, list) or len(proposals) > 8:
        raise ElicitationRelevanceError('QUESTION_RETIREMENT_INVALID: retire_questions requires at most eight proposals')
    if not proposals:
        return
    process = workflow.process
    state = workflow._load()
    nodes = {n['id']: n for n in state.get('plan', {}).get('nodes', [])}
    targets = [p.get('node_id') for p in proposals if isinstance(p, dict)]
    if (not read_only_eligible(process) or len(targets) != len(proposals)
            or len(set(targets)) != len(targets)):
        raise ElicitationRelevanceError('QUESTION_RETIREMENT_INVALID: only distinct model-elicited read-only questions may be retired')
    epoch = receipt_epoch(process)
    events = []
    for proposal in proposals:
        name = proposal['node_id']
        node = nodes.get(name)
        if (not node or node['kind'] != 'decision' or name not in state.get('elicitation_tiers', {})
                or name in state.get('records', {})
                or any(name in n['depends_on'] and n['id'] not in targets for n in nodes.values())):
            raise ElicitationRelevanceError('QUESTION_RETIREMENT_INVALID: cannot waive a user obligation, resolved answer or active dependent')
        reason, quote = proposal.get('reason'), proposal.get('quote')
        source = source_answer(process.session.host_ledger, state['task_id'],
            proposal.get('decision_id'), proposal.get('source_node_id', ''))
        receipts = business_answer_receipts(process.session.host_ledger, state['task_id'])
        actual = any(r['decision_id'] == proposal.get('decision_id')
                     and r.get('source_node_id', '') == proposal.get('source_node_id', '') for r in receipts)
        deferred = any(e.get('event') == 'user_decision_interpreted' and e.get('task_id') == state['task_id']
            and e.get('node_id') == name and e.get('disposition') == 'deferred' for e in process.session.host_ledger)
        if (not actual or not source or source.get('process_id', process.process_id) != process.process_id
                or not isinstance(quote, str) or not quote.strip() or len(quote) > 2000
                or quote not in source.get('answer', '') or not isinstance(reason, str)
                or not reason.strip() or len(reason) > 2000 or deferred):
            raise ElicitationRelevanceError('QUESTION_RETIREMENT_INVALID: cite actual business evidence; explicit waiting and control answers cannot waive questions')
        certificate = {**{k: proposal.get(k, '') for k in ('decision_id','source_node_id','quote','reason')},
            'source_digest': answer_digest(source['answer']), 'receipt_epoch': epoch,
            'scope': 'read_only_question_relevance'}
        state['records'][name] = {'elicitation_retirement': certificate,
            'dependencies': {d: hashlib.sha256(json.dumps(state['records'].get(d), sort_keys=True).encode()).hexdigest()
                             for d in node['depends_on']}}
        events.append({'event': 'alignment_question_retired', 'task_id': state['task_id'],
            'process_id': process.process_id, 'node_id': name, 'certificate': copy.deepcopy(certificate)})
    process.session.host_ledger.extend(events)
    workflow._save(state)
