"""Compile the immediately preceding assignment before admitting a successor.

Continuation reuses identity, not completion evidence. An unfinished assignment
keeps its obligations until a deliberate scope-disposition protocol exists.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .lifecycle import is_consumable_terminal


class ContinuationAdmissionError(ValueError):
    def __init__(self, code, message, *, process_id, retry_remaining=False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.next_actions = [{"action": "list_agents", "reason": "Inspect the original execution, closeout and retained obligations."}]
        if retry_remaining:
            self.next_actions.append({"action": "delegate_task", "patch": {
                "continue_process_id": process_id, "continuation_mode": "remaining"},
                "reason": "Retain the unfinished assignment and append the new requirements."})


def _union(previous, proposed):
    return list(dict.fromkeys([*(previous or []), *(proposed or [])]))


def _tool_requirements(previous, proposed):
    """Retain lower and upper bounds; different composite scopes stay separate."""
    merged = {}
    for raw in [*(previous or []), *(proposed or [])]:
        item = deepcopy(raw)
        key = (item.get('tool_name'), bool(item.get('include_composite_steps', False)))
        if key in merged:
            old = merged[key]
            item['min_calls'] = max(int(old.get('min_calls', 1)), int(item.get('min_calls', 1)))
            maxima = [row['max_calls'] for row in (old, item) if row.get('max_calls') is not None]
            if maxima:
                item['max_calls'] = min(maxima)
            if item.get('max_calls') is not None and item['max_calls'] < item['min_calls']:
                raise ValueError('CONTINUATION_REQUIREMENTS_CONFLICT: tool bounds cannot weaken or contradict retained obligations')
        merged[key] = item
    return list(merged.values())


def _interfaces(previous, proposed, references):
    """Preserve frozen definitions, not just their IDs or a refreshed baseline."""
    rows = {row['ref']: deepcopy(row) for row in (previous or {}).get('interfaces', [])}
    for row in (proposed or {}).get('interfaces', []):
        ref = row['ref']
        if ref in rows and any(rows[ref].get(key) != row.get(key)
                for key in ('file', 'symbol', 'initially_present', 'frozen_signature')):
            raise ValueError('CONTINUATION_INTERFACE_CONFLICT: explicitly reconcile changed interface ' + ref)
        rows.setdefault(ref, deepcopy(row))
    if set(references) - rows.keys():
        raise ValueError('CONTINUATION_INTERFACE_CONTRACT_MISSING: retained interface definitions must be available')
    interfaces = [rows[ref] for ref in sorted(rows)]
    canonical = json.dumps(interfaces, ensure_ascii=False, sort_keys=True)
    return {'contract_id': hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16], 'interfaces': interfaces}


def compile_continuation(previous, previous_contract, owner, proposed, *, mode=''):
    """Return effective fields and provenance without changing either process."""
    if mode not in {'', 'remaining', 'follow_up', 'replace'}:
        raise ValueError('continuation_mode must be remaining, follow_up, or replace')
    pid = previous.process_id
    def reject(code, message, retry_remaining=False):
        raise ContinuationAdmissionError(code, message, process_id=pid, retry_remaining=retry_remaining)
    if not is_consumable_terminal(previous):
        reject('CONTINUATION_CLOSEOUT_PENDING', 'The previous execution has not reliably closed. Wait for closeout or recover its checkpoint before continuing.')
    if previous_contract.get('superseded_by'):
        reject('CONTINUATION_ALREADY_SUPERSEDED', 'Continue the latest execution of this assignment.')
    result = previous.result if isinstance(previous.result, dict) else {}
    review = (getattr(owner, 'child_reviews', {}) or {}).get(pid) or {}
    accepted = (previous.status.value == 'completed' and result.get('status') == 'completed'
                and review.get('verdict') == 'approved' and bool(review.get('summary')))
    worktree = previous.worktree or {}
    if worktree.get('path') and worktree.get('mode') == 'write' and worktree.get('state') != 'shared' and not worktree.get('promoted'):
        accepted = False
    mode = mode or ('follow_up' if accepted else 'remaining')
    if mode in {'follow_up', 'replace'} and not accepted:
        reject('CONTINUATION_OBLIGATIONS_PENDING',
               'Unfinished or unaccepted work cannot be silently replaced. Retain it with remaining; '
               'replacement requires explicit disposition of the original responsibility, which this continuation call does not perform.',
               retry_remaining=True)
    fields = deepcopy(proposed)
    required_before = bool(previous_contract.get('required_for_parent_completion', True))
    if fields.pop('required_for_parent_completion_explicit', False):
        if required_before and fields.get('required_for_parent_completion') is False:
            error = ContinuationAdmissionError('CONTINUATION_PARENT_OBLIGATION_PENDING',
                'This successor would retire a required outcome of the parent task. '
                'Explicitly optional continuation requires parent-obligation disposition, which this call does not perform; '
                'omit the flag to retain the obligation or start an independent task after the parent finishes.', process_id=pid)
            error.next_actions.append({'action': 'delegate_task', 'patch': {
                'continue_process_id': pid, 'required_for_parent_completion': True},
                'reason': 'Keep the original parent completion obligation on the successor.'})
            raise error
    else:
        fields['required_for_parent_completion'] = required_before
    retained = mode == 'remaining'
    if retained:
        # New executions must not be forced to repeat a known external effect,
        # nor guess whether an ambiguous effect happened. Keep the old outcome
        # visible for explicit reconciliation instead of issuing another lease.
        if any(receipt.get('effect_state') == 'ambiguous' or
               (receipt.get('effect') == 'external' and receipt.get('committed') is True)
               for receipt in previous.tool_receipts):
            reject('CONTINUATION_EFFECT_RECONCILIATION_REQUIRED',
                   'The prior assignment contains a committed external effect or an ambiguous effect. Verify and reconcile that outcome before assigning another execution.')
        if (fields['task_kind'] != previous.task_kind
                or fields['profile_id'] != previous.capability_profile_id):
            reject('CONTINUATION_SCOPE_MISMATCH', 'Remaining work must retain its original task kind and capability profile.')
        old_tools = list(previous_contract['allowed_tools'] if 'allowed_tools' in previous_contract
                         else previous.tool_registry.list_all() if previous.tool_registry is not None else [])
        if fields.get('allowed_tools') and not set(fields['allowed_tools']).issubset(old_tools):
            reject('CONTINUATION_SCOPE_MISMATCH', 'Remaining work cannot expand the previous issued tool scope.')
        fields['allowed_tools'] = fields.get('allowed_tools') or old_tools
        for key in ('acceptance_criteria', 'required_test_ids', 'target_files', 'depends_on', 'input_interfaces', 'output_interfaces'):
            fields[key] = _union(previous_contract.get(key), fields.get(key))
        fields['required_tool_calls'] = _tool_requirements(previous_contract.get('required_tool_calls'), fields.get('required_tool_calls'))
        fields['interface_contract'] = _interfaces(previous_contract.get('interface_contract'), fields.get('interface_contract'),
            fields['input_interfaces'] + fields['output_interfaces'])
    fields['continuation'] = {
        'source_process_id': pid,
        'source_task_id': previous.active_task_id or pid,
        'mode': mode,
        'prior_outcome_accepted': accepted,
        'retained_obligations': retained,
        'retained_assignment': ((previous_contract.get('continuation') or {}).get('retained_assignment')
                                or previous.task_description) if retained else '',
    }
    return fields


def direct_user_task_origin(process):
    """Host ledger provenance, never a command/runtime preference, releases A's obligation.

    parent_id still identifies the conversation's original A for presentation.
    A Host-admitted independent user turn does not become an A delegation.
    """
    return _direct_user_origin(process.session.host_ledger, process.process_id, process.active_task_id)


def direct_user_task_origin_from_state(state):
    process = state.get('process') or {}
    return _direct_user_origin((state.get('session') or {}).get('host_ledger', []),
                               process.get('process_id'), process.get('task_id'))


def _direct_user_origin(ledger, process_id, task_id):
    for event in reversed(ledger):
        if (event.get('event') == 'direct_user_task_admitted'
                and event.get('process_id') == process_id
                and event.get('task_id') == task_id
                and event.get('accepted_source_process_id')
                and event.get('owner_process_id')):
            return event
    return None


def _continuation_owner(previous, manager, store=None, workspace=None):
    original = manager.get(previous.parent_id)
    if original is None:
        return None
    # A same-session A successor can review or supersede an inherited B. On a
    # cold direct-B entry, restore only the latest explicit owner, not every A.
    if store is not None:
        deleted = store.deleted_process_ids()
        for link in reversed(store.list_process_links()):
            pid = link['process_id']
            if (pid in deleted or link.get('parent_process_id')
                    or link.get('session_id') != original.session.session_id):
                continue
            live = manager.get(pid)
            state = None if live else store.load_process_state(pid)
            contracts = live.delegated_contracts if live else (state or {}).get('runtime_state', {}).get('delegated_contracts', {})
            if previous.process_id in contracts:
                if live is None:
                    from .recovery import restore_incomplete_processes
                    restore_incomplete_processes(store, manager, workspace, include_process_ids=[pid])
                break
    candidates = [candidate for candidate in manager.list_processes()
        if candidate.actor_kind == 'supervisor'
        and candidate.session.session_id == original.session.session_id
        and previous.process_id in candidate.delegated_contracts
        and manager.owns_child(candidate, previous)]
    return max(candidates, key=lambda candidate: candidate.created_at) if candidates else original


def require_direct_followup(previous, manager, *, store=None, workspace=None):
    """New independent user turns require a reliably closed predecessor.

    The first turn also requires the original delegated outcome to be accepted.
    Subsequent user turns use the Host's durable admission receipt; they do not
    invent a missing A contract or accept an ordinary user_direct_continuation flag.
    """
    if not is_consumable_terminal(previous, manager):
        raise ContinuationAdmissionError('CONTINUATION_CLOSEOUT_PENDING',
            'Wait for reliable closeout before starting another user turn.', process_id=previous.process_id)
    origin = direct_user_task_origin(previous)
    if origin is not None:
        return {'direct_user_origin': dict(origin)}
    owner = _continuation_owner(previous, manager, store, workspace)
    contract = (getattr(owner, 'delegated_contracts', {}) or {}).get(previous.process_id)
    if contract is None:
        raise ContinuationAdmissionError('CONTINUATION_OWNER_CONTRACT_REQUIRED',
            'The original supervisor assignment is unavailable; restore it before starting a new task on this worker session.',
            process_id=previous.process_id)
    compiled = compile_continuation(previous, contract, owner, {
        'task_kind': previous.task_kind, 'profile_id': previous.capability_profile_id,
    }, mode='follow_up')
    compiled['direct_user_origin'] = {'accepted_source_process_id': previous.process_id,
                                     'owner_process_id': owner.process_id}
    return compiled
