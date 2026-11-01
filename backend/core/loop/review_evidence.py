"""Immutable, task-scoped review evidence over the existing CAS reader.

The inline inventory is bounded, not the evidence lifetime. An owned reviewer
can page/search only the snapshot explicitly handed to that review, including
receipts no longer present in conversation or a recent-event window.
"""
from copy import deepcopy
import hashlib
import json


def retained_tool_results(process, receipts):
    """Preserve witnessed result bodies, including failures and no-write commands.

    A command digest or exit code does not identify the command or its output.
    Retain exact available bodies, never substitute the Agent's verification
    prose. Identical copies across epochs are deduplicated; different retained
    representations stay explicit instead of silently choosing one.
    """
    witnesses = {r['receipt_id']: r for r in receipts if r.get('receipt_id')}
    results, seen = [], set()
    for epoch in [*process.session.epoch_archive, {'messages': process.session.messages}]:
        for message in epoch.get('messages', []):
            receipt = message.get('receipt') or {}
            identifier = receipt.get('receipt_id')
            if (message.get('message_type') != 'tool_result' or identifier not in witnesses
                    or receipt != witnesses[identifier] or 'data' not in message):
                continue
            item = {'receipt_id': identifier, 'tool_name': receipt.get('tool_name'),
                'is_error': bool(message.get('is_error')), 'data': deepcopy(message['data']),
                'origin': 'retained_tool_result_body'}
            digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()
            if digest not in seen:
                seen.add(digest)
                results.append({**item, 'digest': digest})
    return results


def committed_diffs(process, receipts):
    """Retain actual write-result patches across conversation compaction.

    A receipt/hash alone is not a before-image. Never reconstruct a patch from
    a model summary, another task, a failed write, or arbitrary backup files.
    """
    witnesses = {r['receipt_id']: r for r in receipts if r.get('receipt_id')
        and r.get('succeeded') is True and r.get('committed') is True
        and r.get('effect') == 'workspace_write' and r.get('effect_state') == 'committed'}
    found = {}
    epochs = [*process.session.epoch_archive, {'messages': process.session.messages}]
    for epoch in epochs:
        for message in epoch.get('messages', []):
            receipt = message.get('receipt') or {}
            identifier = receipt.get('receipt_id')
            data = message.get('data') or {}
            if (message.get('message_type') != 'tool_result' or message.get('is_error')
                    or identifier not in witnesses or receipt != witnesses[identifier]
                    or not isinstance(data, dict) or not isinstance(data.get('diff'), str) or not data['diff']):
                continue
            item = {'receipt_id': identifier, 'files': deepcopy(receipt.get('files', [])),
                'diff': data['diff'], 'before_sha256': receipt.get('before_sha256'),
                'after_sha256': receipt.get('after_sha256'), 'origin': 'committed_tool_result_patch',
                'full_before_file': False}
            if identifier in found and found[identifier] != item:
                raise ValueError('REVIEW_DIFF_IDENTITY_CONFLICT')
            found[identifier] = item
    return list(found.values())


def inline_review_evidence(evidence):
    """The same bounded evidence contract for owned and lifecycle reviewers."""
    return {key: evidence[key] for key in ('receipts', 'artifact_inventory',
        'artifact_notices', 'observed_diffs', 'receipt_source') if key in evidence}


def prepare_review_evidence(process):
    from .completion_protocol import _current_task_receipts
    receipts = deepcopy(_current_task_receipts(process))
    task = process.active_task_id or process.process_id
    storage = getattr(process, 'bound_storage', None) or getattr(getattr(process, '_manager', None), 'storage', None)
    if storage is None:
        raise ValueError('REVIEW_EVIDENCE_STORAGE_REQUIRED')
    claim = getattr(process, 'completion_claim', None)
    from .delivery_artifacts import collect_artifacts
    from backend.core.mechanism_runtime.semantic_projection import snapshot
    artifacts, notices = collect_artifacts(process, list(getattr(claim, 'files', ()) or ()))
    diffs = committed_diffs(process, receipts)
    results = retained_tool_results(process, receipts)
    result_pointers = {}
    for index, item in enumerate(results):
        result_pointers.setdefault(item['receipt_id'], []).append(f'/tool_results/{index}')
    bundle = {'process_id': process.process_id, 'task_id': task, 'receipts': receipts,
        'task': process.task_description, 'result': process.result,
        'artifacts': artifacts, 'artifact_notices': notices, 'observed_diffs': diffs, 'tool_results': results,
        'harness_semantics': snapshot(process, observations=True),
        'completion_claim': claim.to_dict() if getattr(claim, 'to_dict', None) else None}
    source = storage.put_tool_result(json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        process_id=process.process_id, task_id=task, tool_name='review_evidence_snapshot')
    fields = ('receipt_id', 'tool_name', 'task_id', 'effect', 'effect_state', 'succeeded', 'committed',
        'error_code', 'files', 'before_sha256', 'after_sha256', 'test_id', 'test_target', 'test_completed',
        'test_passed', 'command_completed', 'command_exit_code', 'validation_version')
    items, size = [], 0
    # Select effect/verification records before incidental queries; preserve
    # their original order and full negative/stale results. This is an index,
    # never a semantic certification or a newest-success-only projection.
    ordered = [i for i, r in enumerate(receipts) if r.get('effect') not in (None, 'read')
        or any(k in r for k in ('test_completed', 'command_completed', 'after_sha256', 'child_receipts'))]
    chosen = set(ordered)
    ordered += [i for i in range(len(receipts)) if i not in chosen]
    for index in ordered:
        receipt = receipts[index]
        raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        item = {k: receipt[k] for k in fields if k in receipt}
        item.update(json_pointer=f'/receipts/{index}', receipt_digest=hashlib.sha256(raw.encode()).hexdigest())
        if receipt.get('receipt_id') in result_pointers:
            item['result_json_pointers'] = result_pointers[receipt['receipt_id']]
        if len(json.dumps(item, ensure_ascii=False)) > 2200:
            item = {k: item[k] for k in ('receipt_id', 'tool_name', 'json_pointer', 'receipt_digest') if k in item}
            item['details_deferred'] = True
        length = len(json.dumps(item, ensure_ascii=False))
        if len(items) >= 64 or size + length > 16000:
            break
        items.append(item)
        size += length
    previews, remaining = [], 8000
    for index, item in enumerate(diffs):
        if len(previews) == 8 or remaining <= 0:
            break
        text = item['diff'][:min(4000, remaining)]
        previews.append({**item, 'diff': text, 'complete': len(text) == len(item['diff']),
            'json_pointer': f'/observed_diffs/{index}'})
        remaining -= len(text)
    return {'receipts': items, 'artifacts': artifacts, 'artifact_notices': notices,
        'artifact_inventory': [{**{k: a[k] for k in ('path', 'sha256', 'receipt_id', 'bytes')},
            'json_pointer': f'/artifacts/{i}'} for i, a in enumerate(artifacts)],
        'observed_diffs': previews,
        'receipt_source': {**source, 'owner_process_id': process.process_id,
        'owner_task_id': task, 'total_receipts': len(receipts), 'inline_partial': len(items) < len(receipts),
        'total_diffs': len(diffs), 'diff_inventory_partial': len(previews) < len(diffs),
        'snapshot_pointers': {'artifacts': '/artifacts', 'diffs': '/observed_diffs',
            'tool_results': '/tool_results', 'task_control': '/harness_semantics'},
        'inline_is_inventory': True, 'authority_granted': False}}


def read_review_evidence(process, args):
    """Narrow delegation through tool_result_open, never arbitrary parent data."""
    source = getattr(process, 'runtime_preferences', {}).get('review_evidence_source') or {}
    if not source or args.get('locator') != source.get('locator'):
        return None
    manager = getattr(process, '_manager', None)
    owner = manager.get(source.get('owner_process_id')) if manager else None
    if (process.actor_kind != 'reviewer' or process.capability_profile_id != 'review.independent'
            or process.review_target_id != source.get('owner_process_id') or owner is None
            or (owner.active_task_id or owner.process_id) != source.get('owner_task_id')
            or (owner.process_id != process.parent_id and owner.parent_id != process.parent_id)):
        raise PermissionError('REVIEW_EVIDENCE_SCOPE_MISMATCH')
    storage = getattr(owner, 'bound_storage', None) or getattr(manager, 'storage', None)
    if storage is None:
        raise ValueError('REVIEW_EVIDENCE_STORAGE_REQUIRED')
    return storage.read_tool_result(source['locator'], owner_process_id=owner.process_id,
        offset=int(args.get('offset', 0) or 0), max_chars=int(args.get('max_chars', 24000) or 24000),
        json_pointer=str(args.get('json_pointer', '') or ''), query=str(args.get('query', '') or ''))
