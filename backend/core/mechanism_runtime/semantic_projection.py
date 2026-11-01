"""Read-only composition semantics shared by model assembly and Grill.

Uses existing immutable definitions/instance heads. It does not load plugins,
settle generations, execute callbacks, answer questions or grant authority.
"""
from copy import deepcopy
import json

from .repository import Repository


def semantic_program(program):
    return {**{key: deepcopy(program[key]) for key in
        ('id', 'intent', 'source_refs', 'scope', 'digest', 'required_capabilities', 'bindings')},
        'elements': [{key: deepcopy(node[key]) for key in ('id', 'use', 'version', 'parameters', 'boundaries')}
                     for node in program['elements']]}


def component_contracts(programs, registry):
    """Read exact loaded descriptor identities; no callback/lease or judgment."""
    descriptors={(e['id'],e['version'],e['digest']):e for e in registry.catalog()['elements']}
    references=sorted({(n['use'],n['version'],n['contract_digest'])
        for program in programs if program for n in program['elements']})
    items=[]
    for identifier,version,identity in references[:64]:
        descriptor=descriptors.get((identifier,version,identity))
        item={'id':identifier,'version':version,'digest':identity,'available':descriptor is not None}
        if descriptor:
            item.update({key:deepcopy(descriptor[key]) for key in ('inputs','outputs','boundaries','effect','capabilities')})
            description=descriptor.get('description','')
            item.update(description=description[:4000],description_truncated=len(description)>4000)
        items.append(item)
    return {'items':items,'total':len(references),'partial':len(references)>64,'authority_granted':False}


def completion_controls(process, instance):
    task = process.active_task_id or process.process_id
    answers = {item.get('decision_id'): item for item in process.session.host_ledger
        if item.get('event') == 'user_decision_received' and item.get('task_id') == task}
    return [{**{key: directive.get(key) for key in ('kind', 'decision_id', 'event_id', 'epoch', 'return_count')},
        'answer_received': directive.get('decision_id') in answers,
        'answer': str(answers.get(directive.get('decision_id'), {}).get('answer', ''))[:2000],
        'authority_granted': False}
        for directive in list(instance.get('completion_directives', {}).values())[-8:]
        if directive.get('kind') == 'awaiting_user' and directive.get('decision_id')]


def review_observations(instance):
    """One source-bound projection for model, final alignment and product UI.

    These are independent reviewers' fallible judgments, not permissions or
    completion certificates. Old records without a candidate binding remain
    visible history; consumers must not infer which new candidate they cover.
    """
    from .contracts import digest
    records = sorted(instance.get('reviews', {}).items(), key=lambda item: item[1].get('created_at', ''))[-3:]
    return [{**{key: review.get(key) for key in
                 ('reviewer_process_id', 'epoch', 'phase', 'candidate_digest', 'definition_ref')},
             'event_id': event_id, 'verdict': review.get('result', {}).get('verdict'),
             'summary': str(review.get('result', {}).get('summary', ''))[:2000],
             'summary_truncated': len(str(review.get('result', {}).get('summary', ''))) > 2000,
             'evidence_digest': digest(review.get('result', {})), 'authority_granted': False}
            for event_id, review in records]


def describe_instance(process, instance, *, observations=False):
    value = {key: deepcopy(instance.get(key)) for key in
        ('id', 'definition_id', 'definition_revision', 'epoch', 'phase', 'effective_ref', 'desired_ref',
         'version_policy', 'managed_by', 'user_control')}
    value['instance_id'] = instance['id']
    value['program_digest'] = instance['compiled']['digest']
    value['behavior_active'] = instance['phase'] == 'ready' or bool(instance.get('effective_ref') and instance['phase'] in {'pending_activation', 'activating'})
    if instance['phase'] not in {'saved', 'suspended', 'detached'}:
        value['program'] = semantic_program(instance['compiled'])
    if observations:
        holds = sorted(instance.get('holds', {}).values(), key=lambda item: item.get('decision_sequence', 0))
        value['execution'] = {
            'checkpoints': [{key: hold.get(key) for key in
                ('unit_id', 'phase', 'epoch', 'decision_id', 'answer_id', 'hints')} for hold in holds[-8:]],
            'return_counters': deepcopy(instance.get('return_counters', {})),
            'completion_controls': completion_controls(process, instance),
            'reviews': review_observations(instance)}
        value['error'] = deepcopy(instance.get('error'))
    return value


def describe_instances(process, values, *, observations=False):
    items, remaining = [], 24576
    for instance in values:
        view = describe_instance(process, instance, observations=observations)
        if 'program' in view:
            size = len(json.dumps(view['program'], ensure_ascii=False).encode())
            if size <= remaining:
                remaining -= size
            else:
                view.pop('program')
                view['program_details_deferred_for_budget'] = True
        items.append(view)
    return items


def next_boundary_guidance(process, instances):
    """Derive operational guidance from authority, without interpreting intent.

    The Agent still interprets pending requests and proposes semantic changes.
    This projection does not adopt, activate, answer, or certify anything.
    """
    from backend.core.loop.task_maintenance import state_for
    state = state_for(process)
    changes = [item for item in state.get('changes', {}).values() if item.get('status') == 'pending']
    actions = {}
    for instance in instances:
        phase = instance['phase']
        action = ('keep_inactive' if phase in {'saved', 'suspended', 'detached'} else
            'inspect_blocked_evidence' if phase == 'blocked' else
            'retain_inflight_generation_until_boundary' if phase in {'pending_activation', 'activating'} else
            'use_effective_program')
        actions[instance['id']] = {'action': action, 'epoch': instance['epoch'],
            'definition_revision': instance['definition_revision'], 'effective_ref': instance['effective_ref'],
            'desired_ref': instance.get('desired_ref'), 'version_policy': instance['version_policy']}
    return {'task_id': process.active_task_id or process.process_id, 'maintenance_revision': state.get('revision', 0),
        'maintenance_epoch': state.get('epoch', 0), 'instances': actions,
        'pending_changes': [{key: deepcopy(item.get(key)) for key in
            ('id', 'source_ref', 'user_adopted', 'target_task_id', 'base_contract_revision', 'requires_rebase')}
            for item in changes[:16]], 'pending_change_count': len(changes), 'partial': len(changes) > 16,
        'next_action': ('interpret_pending_changes_before_new_effect_or_completion' if changes else
            'continue_current_task_under_effective_instances'),
        'instruction': ('Read each pending change from task_mechanism status and its real source. Interpret information/questions; '
            'reconcile changed requirements with the existing contract, plan and verification. Preserve accepted obligations, '
            'inflight review generations and return counts. Do not manufacture an answer or replay a finished stage. '
            'When there are no pending changes, continue the business task; this projection requests no new setup or review.'),
        'origin': 'derived_current_lifecycle', 'authority_granted': False}


def snapshot(process, *, observations=False):
    task = process.active_task_id or process.process_id
    storage = getattr(process, 'bound_storage', None)
    if storage is None:
        app = getattr(process, '_harness_application', None)
        storage = app.repo.storage if app is not None else None
    values = Repository(storage).list_instances(task, limit=100) if storage is not None else []
    result={'task_id': task, 'instances': describe_instances(process, values, observations=observations), 'authority_granted': False}
    app=getattr(process, '_harness_application', None)
    if app is not None:
        result['component_contracts']=component_contracts([value['compiled'] for value in values], app.registry)
    return result


def alignment_basis(process):
    """Only semantic configuration/control changes invalidate certificates.

    Runtime counters, hints and ordinary event journal writes are observations,
    not new user requirements. Their live projection is still supplied to a
    fresh question/delivery assessment.
    """
    view = snapshot(process)
    return {'task_id': view['task_id'], 'instances': [{key: item[key] for key in
        ('id', 'definition_id', 'definition_revision', 'epoch', 'phase', 'effective_ref', 'desired_ref',
         'version_policy', 'managed_by', 'user_control', 'program_digest')} for item in view['instances']]}


def unsettled_checkpoints(process):
    """Read the authority, not a bounded UI projection, for terminal gating."""
    storage = getattr(process, 'bound_storage', None)
    if storage is None:
        return []
    receipts = {r.get('harness_unit'): r for r in process.tool_receipts if r.get('harness_unit')}
    pending = []
    for value in Repository(storage).list_instances(process.active_task_id or process.process_id, limit=100):
        if value['phase'] not in {'ready', 'pending_activation', 'activating', 'blocked'}:
            continue
        for unit, hold in value.get('holds', {}).items():
            receipt = receipts.get(unit)
            if hold['phase'] in {'preparing','waiting'} or (hold['phase'] == 'answered' and
                    (not receipt or receipt.get('effect_state') in {'unknown','ambiguous','partial'})):
                pending.append({'instance_id': value['id'], 'unit_id': unit, 'phase': hold['phase']})
    return pending
