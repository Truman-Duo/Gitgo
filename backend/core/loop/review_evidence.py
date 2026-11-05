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
    # Full field values and provenance are preserved. Witnessed artifact/diff
    # inventories precede the growing receipt stream and per-attempt locator.
    return {key: evidence[key] for key in ('task_basis', 'artifact_inventory',
        'artifact_notices', 'observed_diffs', 'receipts', 'source_reads', 'task_control', 'operation_coverage', 'receipt_coverage', 'receipt_source') if key in evidence}


def read_observations(results):
    """Bounded exact file-read identities for before/after comparisons.

    Multiple observed versions stay distinct. No claim is made about unseen
    intervals, files or external effects; these are read facts, not a baseline
    reconstructed from the owner's narrative.
    """
    observations, remaining, materialized = [], 6000, set()
    for index, item in enumerate(results):
        data = item['data']
        if item['tool_name'] != 'read_file' or item['is_error'] or not isinstance(data, dict):
            continue
        if not data.get('path') or not data.get('sha256'):
            continue
        observation = {'receipt_id': item['receipt_id'], 'json_pointer': f'/tool_results/{index}',
            'observed_order': index,
            **{key: deepcopy(data[key]) for key in ('path', 'sha256', 'size', 'total_lines',
                'offset', 'returned_lines', 'truncated') if key in data}}
        identity = (data['path'], data['sha256'], data.get('offset', 0), data.get('returned_lines'))
        content = data.get('content')
        if identity not in materialized and isinstance(content, str) and len(content) <= min(1600, remaining):
            observation['witnessed_content'] = content
            observation['content_scope'] = 'Exact returned read page; truncated/offset/returned_lines still apply. Not a claim about the file now.'
            materialized.add(identity)
            remaining -= len(content)
        observations.append(observation)
    return {'items': observations[:32], 'total': len(observations), 'partial': len(observations) > 32,
        'meaning': 'Exact witnessed reads; unchanged hashes establish those observed contents only, not absence of all intervening effects.'}


def control_inventory(semantics):
    """Current lifecycle facts; compiler programs and prior reviews are on demand.

    This is evidence of runtime behavior, not proof that the business task is
    correct. Full history and component descriptors stay in the same snapshot.
    """
    items, size = [], 0
    for index, instance in enumerate(semantics.get('instances', [])):
        item = {k: deepcopy(instance[k]) for k in ('id', 'definition_id', 'definition_revision',
            'epoch', 'phase', 'effective_ref', 'desired_ref', 'version_policy', 'behavior_active',
            'error') if k in instance}
        pointer = f'/harness_semantics/instances/{index}'
        if 'program' in instance:
            item['configuration'] = {
                'intent': instance['program'].get('intent', ''),
                'json_pointer': pointer + '/program', 'details_deferred': True}
        execution = instance.get('execution') or {}
        item['execution'] = {k: deepcopy(execution[k]) for k in
            ('return_counters', 'completion_controls', 'checkpoints') if k in execution}
        # Earlier verdicts do not establish this candidate's correctness. Keep
        # their source discoverable, without front-loading other reviewers'
        # judgments or repeating the complete composition on every attempt.
        item['execution']['review_history'] = {
            'available': bool(execution.get('reviews')),
            'json_pointer': pointer + '/execution/reviews', 'details_deferred': True}
        item['json_pointer'] = pointer
        length = len(json.dumps(item, ensure_ascii=False))
        if size + length > 8000:
            break
        items.append(item)
        size += length
    return {'instances': items, 'partial': len(items) < len(semantics.get('instances', [])),
        'json_pointer': '/harness_semantics', 'authority_granted': False,
        'source': 'runtime_configuration_and_observations_not_semantic_approval'}


def witnessed_body(item):
    """Expose exact execution output even when its control envelope is large."""
    data = item['data']
    if len(json.dumps(data, ensure_ascii=False)) <= 2400:
        return deepcopy(item)
    if not isinstance(data, dict):
        return None
    fields = ('argv', 'cwd', 'stdout', 'stderr', 'exit_code', 'success', 'truncated',
        'execution_state', 'effect_state', 'producer_completed', 'producer_exit_code',
        'input_applicability', 'test_id', 'target', 'passed', 'seed_results', 'verification_state')
    projected = {k: deepcopy(data[k]) for k in fields if k in data}
    # Some native producers put the exact command body only in this field.
    if 'command_result' in data and not all(k in data for k in ('argv', 'exit_code')):
        projected['command_result'] = deepcopy(data['command_result'])
    if not projected or len(json.dumps(projected, ensure_ascii=False)) > 2400:
        return None
    return {**{k: deepcopy(v) for k, v in item.items() if k != 'data'},
        'data': projected, 'data_projection': 'exact_execution_fields; full body at result_json_pointers'}


def review_task_basis(process, task):
    """Exact task-local user inputs, including replies at control checkpoints.

    Control answers are evidence of what was said, not new task contracts,
    permissions, or proof of external effects. Preserve that distinction.
    """
    from .task_maintenance import source_receipts
    from .task_contract import get_task_contract
    sources = deepcopy(source_receipts(process))
    ledger = [e for e in process.session.host_ledger if e.get('task_id') == task]
    requests = {e['decision_id']: e for e in ledger
        if e.get('event') == 'user_decision_requested' and e.get('decision_id')}
    for event in ledger:
        if event.get('event') != 'user_decision_received' or event.get('decision_id') not in requests:
            continue
        ref = 'decision:' + event['decision_id']
        if ref not in sources:
            sources[ref] = {'kind': 'user_control_reply', 'received': deepcopy(event),
                'request': deepcopy(requests[event['decision_id']]),
                'authority_sequence': event.get('authority_sequence', 0), 'grants_authority': False}
    contract = deepcopy(get_task_contract(process))
    index, remaining = [], 6000
    for ref, value in sources.items():
        if len(index) == 32 or remaining <= 0:
            break
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        preview = raw[:min(1200, remaining)]
        remaining -= len(preview)
        index.append({'source_ref': ref, 'kind': value.get('kind'),
            'json_pointer': '/user_sources/' + ref.replace('~', '~0').replace('/', '~1'),
            'sha256': hashlib.sha256(raw.encode()).hexdigest(), 'preview': preview,
            'complete': len(preview) == len(raw)})
    from .model_feedback import contract_view
    current = contract_view(contract)
    contract_inline = len(json.dumps(current, ensure_ascii=False)) <= 8000
    return contract, sources, {'user_sources': index,
        'current_contract': current if contract_inline else None,
        'contract_details_deferred': not contract_inline,
        'total_user_sources': len(sources), 'user_sources_partial': len(index) < len(sources),
        'source_values_are_not_verified_external_effects': True, 'authority_granted': False,
        'current_contract_pointer': '/current_contract', 'contract_revision': contract.get('revision', 0)}


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
    contract, sources, basis = review_task_basis(process, task)
    semantics = snapshot(process, observations=True)
    bundle = {'process_id': process.process_id, 'task_id': task, 'receipts': receipts,
        'current_contract': contract, 'user_sources': sources,
        'task': process.task_description, 'result': process.result,
        'artifacts': artifacts, 'artifact_notices': notices, 'observed_diffs': diffs, 'tool_results': results,
        'harness_semantics': semantics,
        'completion_claim': claim.to_dict() if getattr(claim, 'to_dict', None) else None}
    source = storage.put_tool_result(json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        process_id=process.process_id, task_id=task, tool_name='review_evidence_snapshot')
    fields = ('receipt_id', 'tool_name', 'task_id', 'effect', 'effect_state', 'succeeded', 'committed',
        'error_code', 'files', 'before_sha256', 'after_sha256', 'test_id', 'test_target', 'test_completed',
        'test_passed', 'command_completed', 'command_exit_code', 'validation_version',
        'timestamp', 'started_at', 'completed_at')
    items, size = [], 0
    # Select effect/verification records before incidental queries; preserve
    # their original order and full negative/stale results. This is an index,
    # never a semantic certification or a newest-success-only projection.
    ordered = [i for i, r in enumerate(receipts) if r.get('effect') == 'workspace_write'
        or any(k in r for k in ('test_completed', 'command_completed', 'after_sha256', 'child_receipts'))]
    chosen = set(ordered)
    # Incidental successful reads remain searchable in the immutable snapshot.
    ordered += [i for i, receipt in enumerate(receipts) if i not in chosen
        and (receipt.get('succeeded') is False or receipt.get('error_code'))]
    for index in ordered:
        receipt = receipts[index]
        raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        item = {k: receipt[k] for k in fields if k in receipt}
        item.update(json_pointer=f'/receipts/{index}', receipt_digest=hashlib.sha256(raw.encode()).hexdigest())
        if receipt.get('receipt_id') in result_pointers:
            item['result_json_pointers'] = result_pointers[receipt['receipt_id']]
            # Inline a small exact command/test body, never the owner's claim.
            # Larger bodies remain available at their precise snapshot pointer.
            witnesses = [r for r in results if r['receipt_id'] == receipt['receipt_id']]
            if receipt.get('command_completed') is not None or receipt.get('test_completed') is not None:
                small = [body for r in witnesses if (body := witnessed_body(r)) is not None]
                if small:
                    item['witnessed_results'] = small
        if len(json.dumps(item, ensure_ascii=False)) > 4800:
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
    from .review_packet import operation_coverage
    return {'receipts': items, 'artifacts': artifacts, 'artifact_notices': notices, 'task_basis': basis,
        'operation_coverage': operation_coverage(receipts, items),
        'source_reads': read_observations(results),
        'receipt_coverage': {effect: {'total': sum(r.get('effect') == effect for r in receipts),
            'inline': sum(r.get('effect') == effect for r in items),
            'complete': sum(r.get('effect') == effect for r in receipts) == sum(r.get('effect') == effect for r in items)}
            for effect in sorted({str(r['effect']) for r in receipts if r.get('effect')})},
        'task_control': control_inventory(semantics),
        'artifact_inventory': [{**{k: a[k] for k in ('path', 'sha256', 'receipt_id', 'bytes')},
            'json_pointer': f'/artifacts/{i}'} for i, a in enumerate(artifacts)],
        'observed_diffs': previews,
        'receipt_source': {**source, 'owner_process_id': process.process_id,
        'owner_task_id': task, 'total_receipts': len(receipts), 'inline_partial': len(items) < len(receipts),
        'total_diffs': len(diffs), 'diff_inventory_partial': len(previews) < len(diffs),
        'snapshot_pointers': {'artifacts': '/artifacts', 'diffs': '/observed_diffs',
            'tool_results': '/tool_results', 'task_control': '/harness_semantics',
            'current_contract': '/current_contract', 'user_sources': '/user_sources'},
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
