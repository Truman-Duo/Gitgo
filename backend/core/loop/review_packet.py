"""Mechanical evidence indexes over one immutable review snapshot.

These describe observed operations and versions, never prove semantic quality,
unobserved effects or permission. Unknown producers remain in business scope.
"""


def operation_coverage(receipts, inline):
    visible = {r['receipt_id'] for r in inline if r.get('receipt_id')}
    groups = {'business': [], 'runtime_control': [], 'observation': []}
    for index, receipt in enumerate(receipts):
        contract = receipt.get('execution_contract') or {}
        control = (contract.get('kind') == 'data_broker'
            and str(contract.get('broker', '')).startswith('host.runtime.'))
        # Actual execution/test/write witnesses override even an inconsistent
        # control descriptor; an unknown effect must never disappear as a read.
        execution = receipt.get('effect') == 'workspace_write' or any(k in receipt
            for k in ('test_completed', 'command_completed', 'after_sha256', 'child_receipts'))
        group = ('business' if execution else 'runtime_control' if control else
            'observation' if receipt.get('effect') in {'read','external_read'} else 'business')
        groups[group].append((index, receipt))
    business = groups['business']
    missing = [(i, r) for i, r in business if r.get('receipt_id') not in visible]
    return {'business': {'total': len(business), 'inline': len(business)-len(missing),
        'complete': not missing, 'missing': [{'receipt_id': r.get('receipt_id'),
            'tool_name': r.get('tool_name'), 'json_pointer': f'/receipts/{i}'} for i,r in missing[:32]],
        'missing_index_partial': len(missing)>32},
        'runtime_control_count': len(groups['runtime_control']),
        'observation_count': len(groups['observation']),
        'meaning': 'Coverage of observed business operation receipts only. Configuration/control and reads are separately counted, not missing business effects. Unknown effects stay business. This is not proof of correctness or absence of unobserved effects.'}
