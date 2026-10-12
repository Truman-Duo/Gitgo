"""Current interaction facts, projected from existing workflow and receipts.

This is not a question classifier or a second interaction state machine.
The Agent chooses whether to ask; only committed requests create cards.
"""
from .decision_timeline import decision_timeline
from .engineering_workflow import EngineeringWorkflow
from .alignment_evidence import resolved_answer


def interaction_snapshot(process):
    task = process.active_task_id or process.process_id
    ledger = [event for event in process.session.host_ledger
              if event.get('task_id') == task and event.get('process_id') == process.process_id]
    cards = decision_timeline(ledger)
    pending = process.pending_decision or {}
    awaiting = [card['decision']['decision_id'] for card in cards
                if card['status'] == 'awaiting_user']
    pending_id = pending.get('decision_id')
    consistent = (not pending_id and not awaiting) or (
        pending.get('task_id') == task and pending.get('process_id') == process.process_id
        and awaiting == [pending_id])
    workflow = EngineeringWorkflow(process)
    state = workflow._load()
    ready, uninterpreted = [], []
    if state:
        status = workflow.status()
        ready = status.get('question_frontier', [])
        for node in state['plan']['nodes']:
            if node['kind'] != 'decision':
                continue
            answers = workflow._answers(node)
            if answers and not resolved_answer(process.session.host_ledger, answers[-1], node['state_topic']):
                uninterpreted.append(node['id'])
    return {
        'state': 'consistent' if consistent else 'inconsistent',
        'pending_card': pending_id if consistent else None,
        'recorded_ready_questions': ready[:8],
        'ready_question_count': len(ready),
        'answers_needing_interpretation': uninterpreted[:8],
        'uninterpreted_answer_count': len(uninterpreted),
    }
