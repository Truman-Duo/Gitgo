"""Actionable model views, never authority or a substitute for retained evidence.

Tools keep their full typed result for runtime consumers, UI and review. Only
the model presentation opts into these views; omitted fields remain available
through the existing process-scoped tool_result_open reader.
"""
from copy import deepcopy


def select(value, keys):
    return {key: deepcopy(value[key]) for key in keys if key in value}


def contract_view(value):
    # Keep every business obligation verbatim. Compiler graphs, activation
    # inventories and provenance journals are not instructions to the Agent.
    return select(value, ('revision', 'goal', 'execution_mode', 'delegation_required',
        'minimum_delegated_outcomes', 'deliverables', 'acceptance_criteria',
        'uncertainties', 'required_tool_calls', 'adopt_process_ids', 'requires_user_decision',
        'user_requested_delegation', 'independent_workstreams', 'target_files', 'required_test_ids',
        'depends_on', 'input_interfaces', 'output_interfaces', 'interface_contract',
        'relationship_policy', 'agent_required_delegation', 'handoff_summary'))


def instance_view(value):
    result = select(value, ('id', 'instance_id', 'definition_id', 'definition_revision',
        'epoch', 'phase', 'program_digest', 'version_policy', 'behavior_active', 'user_control', 'control_effect', 'error', 'maintenance'))
    if value.get('inbox'):
        items = list(value['inbox'].values())
        actionable = [item for item in items if item['status'] in {'candidate', 'pending_adoption'}]
        result['inbox'] = [{**select(item, ('id', 'status', 'question')),
            'text': item['text'][:1200], 'text_truncated': len(item['text']) > 1200} for item in actionable[:8]]
        result['inbox_total'] = len(items)
        result['inbox_actionable'] = len(actionable)
        result['inbox_partial'] = len(actionable) > 8
    program = value.get('program') or value.get('compiled') or {}
    if program.get('intent'):
        result['intent'] = program['intent']
    execution = value.get('execution') or {}
    # Negative findings and actual control replies must remain visible. Do not
    # tell the model to repeat lifecycle callbacks already owned by runtime.
    result['execution'] = select(execution, ('checkpoints', 'completion_controls', 'reviews'))
    if execution.get('reviews'):
        current = [item for item in execution['reviews'] if item.get('epoch') == value.get('epoch')]
        # A new candidate/revision is not a request to reread old findings.
        # The latest observation retains its exact candidate/version binding;
        # neither this selection nor an approved verdict grants authority.
        result['execution']['reviews'] = deepcopy(current[-1:])
        if len(execution['reviews']) > len(current[-1:]):
            result['execution']['review_history_available_on_request'] = True
    if not result['execution']:
        result.pop('execution')
    return result


def task_view(value):
    result = select(value, ('task_id', 'process_id', 'source_refs', 'catalog_version', 'authority_granted', 'work_item_links'))
    result['contract'] = contract_view(value.get('contract') or {})
    result['advancement'] = select(value.get('advancement') or {}, ('mode', 'phase', 'user_control'))
    state = value.get('state') or {}
    result['state'] = select(state, ('revision', 'epoch', 'phase', 'suspended', 'detached',
        'lifecycle_control', 'plan', 'review_requirements'))
    for name in ('changes', 'maintenance_items'):
        result['state'][name] = {key: deepcopy(item) for key, item in state.get(name, {}).items()
            if item.get('status') == 'pending'}
    related = state.get('related') or {}
    result['state']['related'] = {'summary': related.get('summary'), 'candidates': {key: {name: deepcopy(content) for name, content in item.items() if name != 'execution'}
        for key, item in related.get('candidates', {}).items()
        if item.get('status') not in {'rejected', 'withdrawn', 'resolved'}}}
    result['owned_scope_updates'] = [{**select(item, ('process_id', 'task_id', 'changes', 'plan_revision', 'phase')),
        'contract': contract_view(item.get('contract') or {})} for item in value.get('owned_scope_updates', [])]
    result['details'] = 'task_mechanism status returns a reference to the full current state; inspect only when needed.'
    return result


def alignment_task_view(value):
    """Current semantic evidence without consumer acknowledgements or copies.

    This assessor has no detail-reading tool, so keep every unique semantic
    field, including historical user sources, failed/retired changes, plans,
    dependency versions and full review findings. Only known compiler hashes
    and a byte-equivalent duplicate definition are omitted. The authority and
    readiness digest continue using the original full state.
    """
    result = deepcopy(value)
    result.pop('consumer_projection', None)
    contract = result.get('contract') or {}
    state = result.get('state') or {}
    if 'mechanism' in contract and contract['mechanism'] == state.get('definition'):
        contract.pop('mechanism')
    # Keep activation status and generation, rather than a second inventory
    # of digests for the same already-committed contract and plan.
    for key in ('contract_digest', 'workflow_digest'):
        (contract.get('activation') or {}).pop(key, None)
    manifest = state.get('manifest') or {}
    for key in ('digest', 'contract_digest', 'definition_digest', 'workflow_digest', 'governance_digest'):
        manifest.pop(key, None)
    state.pop('contract_basis', None)
    return result


def task_feedback(args, data):
    if data.get('error'):
        return data
    if 'state' in data and 'contract' in data:
        return task_view(data)
    if args.get('operation') == 'preview':
        return select(data, ('base_revision', 'behavior_diff', 'preview_digest', 'authority_granted'))
    return data


def contract_feedback(args, data):
    if data.get('accepted') is not True:
        return data
    result = select(data, ('accepted', 'host_validated', 'authority_granted', 'routing_advice', 'maintenance_status'))
    result['contract_revision'] = data['contract']['revision']
    result['execution_mode'] = data['contract'].get('execution_mode')
    result['activation'] = select(data.get('activation') or {}, ('phase', 'status', 'revision', 'error'))
    maintenance = data.get('maintenance_status') or {}
    if maintenance.get('phase') == 'needs_review':
        result['next_action'] = ('Before mutation or delivery, supply the outstanding plan/related_review or change resolutions '
            'with declare_task_contract operation=revise. Keep the plan proportional to actual business work; '
            'do not list Runtime bookkeeping as extra work.')
    return result


def completion_feedback(args, data):
    if data.get('accepted') is not True or data.get('error'):
        return data
    return {**select(data, ('accepted', 'verdict', 'target_process_id')),
        'runtime_next': 'The submitted claim is retained. Runtime evaluates the existing completion boundary; acceptance is not a delivery or correctness certificate.'}


def harness_feedback(args, data):
    if not data.get('error') and args.get('operation', '').startswith('inbox_') and 'candidate' in data:
        return {'candidate': select(data['candidate'], ('id', 'status', 'replaced_by', 'change_id')),
            **select(data, ('next_action', 'authority_granted'))}
    if not data.get('error') and args.get('operation') == 'preview' and data.get('kind') == 'harness_capability_deployment':
        # Show the requested meaning and exact selected revision once. The full
        # compiler/authority subject is retained by ToolPipeline, not replayed
        # as instructions to reconstruct its plumbing.
        result = {'operation': 'preview', 'state': 'preview_only',
            'next_action': 'This is a retained draft only; no definition is published or active. If this is the requested behavior, submit next_call before business operations that depend on it.',
            **select(data, ('kind', 'definition_id', 'definition_revision',
            'selection', 'definition_head_revision', 'previous_definition_revision',
            'instance_id', 'version_policy', 'committed', 'effect_state', 'authority_granted'))}
        if data.get('declaration') is None:
            result['target'] = deepcopy(data['target'])  # exact legacy meaning
            result['previous'] = deepcopy(data.get('previous'))
            result['diff'] = deepcopy(data['diff'])
        else:
            # The callable target is shown once, including every argument.
            # The apply next_call already carries this same declaration.
            if data.get('preview_ref') or data['next_operation'] != 'apply':
                result['declaration'] = deepcopy(data['declaration'])
            before, after = data.get('previous_declaration'), data['declaration']
            if before is not None:
                old = {item['id']: item for item in before['calls']}
                new = {item['id']: item for item in after['calls']}
                result['changes'] = {
                    'intent_before': before['intent'], 'on_before': deepcopy(before['on']),
                    'removed_calls': [deepcopy(old[k]) for k in sorted(old.keys() - new.keys())],
                    'changed_calls_before': [deepcopy(old[k]) for k in sorted(old.keys() & new.keys()) if old[k] != new[k]]}
            elif data.get('previous') is not None:
                result['previous_legacy_program'] = deepcopy(data['previous'])
            else:
                result['previous'] = None
        result['next_call'] = ({'operation': 'commit', 'preview_ref': data['preview_ref']}
            if data.get('preview_ref') else {**deepcopy(args), 'operation': data['next_operation']})
        if not data.get('preview_ref') and data['next_operation'] == 'use':
            # This is a proposed exact selection, not an execution ticket.
            # Normal source/semantic review and CAS checks still run on use.
            result['next_call']['definition_revision'] = data['definition_revision']
        result['next_call_guidance'] = 'Only if this behavior and selected version match the request. Preview made no changes; the normal execution boundary revalidates the call.'
        return result
    if data.get('error') or args.get('operation') in {'catalog', 'preview', 'preview_revision'}:
        # Explicit schema discovery/behavior preview and failures are already
        # requested information, not unsolicited operational bookkeeping.
        return data
    if 'instances' in data:
        result = {**select(data, ('project_id', 'task_id', 'requirements', 'authority_granted')),
            'instances': [instance_view(item) for item in data['instances']]}
        result['definitions'] = [{**select(item, ('id', 'revision')),
            'intent': (item.get('declaration') or item.get('legacy_program') or {}).get('intent'),
            **({'declaration': deepcopy(item['declaration'])} if args.get('definition_id') and item.get('declaration') else
               {'legacy_program': deepcopy(item['legacy_program'])} if args.get('definition_id') and item.get('legacy_program') else {})}
            for item in data.get('definitions', [])]
        return result
    if 'instance' in data:
        result = {key: deepcopy(value) for key, value in data.items() if key != 'instance'}
        result['instance'] = instance_view(data['instance'])
        # Deployment acknowledges configuration and phase, not a replay of
        # already observed review/checkpoint history. Live guidance owns that.
        if not data['instance'].get('error'):
            result['instance'].pop('execution', None)
        return result
    return data


def rework_feedback(directive, process):
    """Retain rejection guidance; retrieve supporting evidence only as needed.

    The original completion directive remains authoritative. Presentation or
    persistence failure returns it unchanged; never repeat a completed review.
    """
    storage = getattr(process, 'bound_storage', None) or getattr(getattr(process, '_manager', None), 'storage', None)
    if storage is None:
        return directive
    result = deepcopy(directive)
    for item in result.get('instructions', []):
        evidence = item.get('evidence')
        if not isinstance(evidence, dict):
            continue
        # Keep unknown fields and every finding/summary/scope qualification.
        # Only the detailed evidence array and executor tool inventory defer.
        item['evidence'] = {key: value for key, value in evidence.items() if key not in {'evidence', 'read_only_tools'}}
    if result == directive:
        return directive
    try:
        from backend.core.dispatch.truncation import serialize_result
        spill = storage.put_tool_result(serialize_result(directive), process_id=process.process_id,
            task_id=process.active_task_id or process.process_id, tool_name='completion_review', media_type='application/json')
    except Exception as error:
        return {**deepcopy(directive), 'model_presentation_notice': {'code': 'MODEL_VIEW_UNAVAILABLE',
            'detail': str(error)[:300], 'original_result_retained': True}}
    result['details'] = {'locator': spill['locator'], 'retrieval_tool': 'tool_result_open',
        'guidance': 'Full rejection evidence. Read a needed json_pointer when a finding needs verification; do not reopen merely to acknowledge the return.'}
    return result
