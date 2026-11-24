"""Agent-requested renewal of pre-admitted execution steps, not permissions."""
from .budget import TaskBudgetExceeded


def request_extension(process, args):
    from backend.core.errors import error_payload
    from .mechanism_lifecycle import commit_activation, fence
    from .llm import StreamCancelledError
    budget = process.task_budget
    # ToolPipeline injects this bound workspace after public schema validation;
    # neither identities nor step counts are accepted from the model.
    allowed = {'operation', 'reason', 'remaining_work', '_workspace', '_allowed_roots'}
    if set(args) - allowed:
        return error_payload('EXECUTION_STEP_RENEWAL_INVALID', message='Runtime supplies identities and step counts; supply only reason and remaining_work.',
            details={'unexpected_fields': sorted(set(args) - allowed)})
    reason = args.get('reason') if isinstance(args.get('reason'), str) else ''
    remaining = args.get('remaining_work') if isinstance(args.get('remaining_work'), str) else ''
    reason, remaining = reason.strip(), remaining.strip()
    if not reason or len(reason) > 2000 or not remaining or len(remaining) > 4000:
        return error_payload('EXECUTION_STEP_RENEWAL_INVALID',
            message='Supply a short reason and the concrete remaining work; Runtime selects the next admitted allowance.')
    if budget is None:
        return error_payload('EXECUTION_STEP_RENEWAL_DISABLED')
    with fence(process), process._checkpoint_lock:
        if process.cancel_requested or process.cancellation_event.is_set():
            raise StreamCancelledError('cancelled before execution-step renewal')
        if process.session.mechanism_activation_blocked:
            return error_payload('EXECUTION_STEP_RENEWAL_PERSISTENCE_BLOCKED')
        before = budget.step_allowance(process.process_id)
        old_limit = process.max_steps
        try:
            extension = budget.extend_step_allowance(process.process_id, steps_used=process.steps_used,
                reason=reason, remaining_work=remaining)
        except TaskBudgetExceeded as exc:
            return error_payload(exc.code, message=str(exc), details={'step_allowance': before})
        process.max_steps = extension['limit']
        event = {'event': 'execution_step_allowance_extended', 'task_id': process.active_task_id,
                 'process_id': process.process_id, **extension}
        def rollback():
            budget.restore_step_allowance(process.process_id, before)
            process.max_steps = old_limit
        store = getattr(getattr(process, '_manager', None), 'session_store', None)
        if store is not None:
            commit_activation(process, event, store, rollback)
        else:
            process.session.host_ledger.append({**event, 'persistence': 'memory_only'})
        return {'accepted': True, 'step_allowance': budget.step_allowance(process.process_id),
                'authority_granted': False, 'runtime_next': 'Continue the existing task; completed effects and all consumed budgets remain counted.'}


def notify_boundary(process, session, emit):
    budget = process.task_budget
    if budget is None:
        return
    allowance = budget.step_allowance(process.process_id)
    remaining = process.max_steps - process.steps_used
    if remaining > max(3, min(10, process.max_steps // 5)):
        return
    if process.runtime_preferences.get('step_boundary_notified') == process.max_steps:
        return
    process.runtime_preferences['step_boundary_notified'] = process.max_steps
    renewable = allowance['renewals_used'] < allowance['maximum_renewals']
    guidance = ("If more work is needed within the accepted scope, call task_mechanism "
        "operation=extend_execution with reason and remaining_work. Runtime supplies the next fixed segment; "
        "this grants no tool or resource permission." if renewable else
        "No further step segment is available. Prioritize verification and an honest delivery or retained recovery state; do not omit required checks or claim unfinished work is complete.")
    message = f"Execution allowance: {process.steps_used}/{process.max_steps} steps used. {guidance}"
    session.append_host_steering(message, steering_type='execution_step_boundary', version=str(process.max_steps))
    emit({'event': 'execution_step_allowance_near_limit', 'steps_used': process.steps_used,
          'limit': process.max_steps, 'remaining': remaining, 'renewal_available': renewable})
