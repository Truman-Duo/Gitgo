"""Provider-safe cancellation, mailbox and compaction boundaries."""
from dataclasses import dataclass
from .budget import TaskBudgetExceeded
from .context_window import manage_context
from .lifecycle import transition
from .llm import StreamCancelledError
from .models import ProcessStatus
from .outcome import OutcomeStatus
from .runtime_outcomes import _make_result, _cancellation_result, _budget_exhausted_result


@dataclass(frozen=True)
class PreparedTurn:
    result: dict | None = None
    window: dict | None = None


class TurnPreparation:
    def __init__(self, process, session, provider, get_window, governance, compact, apply_mailbox, emit):
        self.process, self.session, self.provider = process, session, provider
        self.get_window, self.governance, self.compact = get_window, governance, compact
        self.apply_mailbox, self.emit = apply_mailbox, emit

    def prepare(self):
        process, session = self.process, self.session
        self.governance.refresh()
        if process.cancel_requested:
            return PreparedTurn(result=_cancellation_result(process, session))
        try:
            if process.task_budget is not None:
                process.task_budget.check_deadline()
            manager = getattr(process, "_manager", None)
            if manager is not None:
                manager.wait_for_interface_decision(process)
            self.apply_mailbox()
            if session.context_abort_requested:
                session.context_abort_requested = False
                transition(process, ProcessStatus.FAILED)
                return PreparedTurn(result=_make_result(process, session,
                    'The task stopped safely because forced context compaction was declined.',
                    error_code='CONTEXT_FORCE_COMPACTION_DECLINED',
                    error_message='User declined lossy provider-visible compaction', outcome_status=OutcomeStatus.FAILED))
            if session.force_compact_requested:
                session.force_compact_requested = False
                _, terminal = self.compact('user_approved_force_compact', force=True, failure_required=True)
                if terminal is not None:
                    return PreparedTurn(result=terminal)
            if session.manual_compact_requested:
                try:
                    _, terminal = self.compact('manual')
                finally:
                    session.manual_compact_requested = False
                if terminal is not None:
                    return PreparedTurn(result=terminal)
            return self._window_boundary()
        except TaskBudgetExceeded as exc:
            return PreparedTurn(result=_budget_exhausted_result(process, session, exc))
        except StreamCancelledError:
            return PreparedTurn(result=_cancellation_result(process, session))

    def _window_boundary(self):
        process, session = self.process, self.session
        check = self.get_window().check(session)
        auto = bool(process.runtime_preferences.get('auto_compact', True))
        if check['action'] != 'none':
            self.emit({'event': 'context_window_action', **check, 'context_epoch': session.context_epoch})
        if check['action'] == 'force_compact' and not auto:
            transition(process, ProcessStatus.FAILED)
            return PreparedTurn(result=_make_result(process, session,
                'Automatic compaction is disabled. Run /compact and retry the task.',
                error_code='MANUAL_COMPACTION_REQUIRED',
                error_message='The context reached its hard watermark while automatic compaction was disabled',
                window_action='force_compact', window_usage_ratio=float(check.get('usage_ratio') or 0)))
        if check['action'] in {'prune', 'force_compact'} and auto:
            needs = manage_context(session, self.governance.harness,
                self.provider if check['action'] == 'force_compact' else None,
                model_token_limit=session.model_context_limit)
            if needs and check['action'] == 'force_compact':
                _, terminal = self.compact('automatic_force_watermark', failure_required=True,
                    retention_suggestions=self.governance.retention_suggestions())
                if terminal is not None:
                    return PreparedTurn(result=terminal)
        return PreparedTurn(window=check)
