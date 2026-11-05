"""Bounded facts for semantic callbacks, derived from existing task evidence.

No independent history or authority. An unstarted invocation is not a business
effect, a received prediction is not permission, and a later proposal is not
the earlier operation merely because the two look similar.
"""
from copy import deepcopy
import json

from .contracts import digest


def checkpoint_observations(process, instance):
    task = process.active_task_id or process.process_id
    answers = {e.get('decision_id'): e for e in process.session.host_ledger
        if e.get('event') == 'user_decision_received' and e.get('task_id') == task}
    receipts = {r.get('harness_unit'): r for r in process.tool_receipts
        if r.get('harness_unit') and r.get('task_id', task) == task}
    holds = sorted(instance.get('holds', {}).values(), key=lambda h: h.get('decision_sequence', 0))
    result = []
    for hold in holds[-8:]:
        answer = answers.get(hold.get('answer_id'))
        receipt = receipts.get(hold['unit_id'])
        item = {k: deepcopy(hold[k]) for k in
            ('unit_id', 'phase', 'epoch', 'decision_id', 'answer_id', 'hints', 'reason') if k in hold}
        item.update(answer_received=answer is not None, authority_granted=False)
        # Exact reply is needed when a released checkpoint is superseded by a
        # later admission check. Superseded describes the call, not the answer.
        if answer:
            text = str(answer.get('answer', ''))
            item.update(answer=text[:2000], answer_truncated=len(text) > 2000)
        if receipt:
            item['actual_result'] = {k: deepcopy(receipt[k]) for k in
                ('receipt_id', 'tool_name', 'succeeded', 'effect_state', 'execution_state') if k in receipt}
        elif hold.get('reason') in {'invocation_changed_before_resume', 'call_rejected_before_effect_admission'}:
            item['actual_result'] = {'execution_state': 'not_started'}
        result.append(item)
    return result


def recent_tool_observations(process, *, limit=12):
    """Chronological receipts plus small, witnessed failure bodies.

    Read exact retained messages across compaction through the existing evidence
    helper. Missing/truncated evidence stays explicit, never reconstructed from
    a model summary. Detailed output text is data, not instructions.
    """
    from backend.core.loop.review_evidence import retained_tool_results
    task = process.active_task_id or process.process_id
    receipts = [r for r in process.tool_receipts if r.get('task_id', task) == task][-limit:]
    failed = [r for r in receipts if r.get('succeeded') is False and r.get('effect_state') == 'committed'][-3:]
    bodies = retained_tool_results(process, failed)
    result = []
    for receipt in receipts:
        item = {k: deepcopy(receipt[k]) for k in
            ('tool_name', 'receipt_id', 'effect', 'succeeded', 'committed', 'effect_state',
             'execution_state', 'error_code', 'timestamp', 'harness_unit') if k in receipt}
        item['receipt_digest'] = digest(receipt)
        contract = receipt.get('execution_contract') or {}
        item['runtime_control'] = str(contract.get('broker', '')).startswith('host.runtime.')
        matches = [b for b in bodies if b['receipt_id'] == receipt.get('receipt_id')]
        if matches:
            # Conflicting retained representations are not silently merged.
            body = matches[-1]
            data = body['data']
            if isinstance(data, dict):
                data = {k: data[k] for k in ('target', 'test_id', 'passed', 'success', 'error',
                    'stdout', 'stderr', 'exit_code', 'command_result', 'seed_results') if k in data}
            text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
            item['result_evidence'] = {'body_digest': body['digest'], 'preview': text[:1600],
                'truncated': len(text) > 1600, 'retained_representations': len(matches), 'origin': body['origin']}
        result.append(item)
    return result
