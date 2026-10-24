"""Bounded provider progress recovery; never executes partial calls."""

from backend.core.prompt_templates import load_prompt, render_prompt
from .answer_continuation import remember_answer_fragment
from .recovery_guidance import diagnostic_hint
from .delivery_runtime import DeliveryBoundary
from .lifecycle import transition
from .models import ProcessStatus
from .outcome import OutcomeStatus
from .runtime_outcomes import _make_result

class ProviderProgress:
    def __init__(self, process, session, guard, emit):
        self.process, self.session, self.guard, self.emit = process, session, guard, emit
        self.no_progress_recoveries = 0
        self.incomplete_tool_call_counts = {}

    def handle(self, response, duration_ms):
        self._record_invalid(response.invalid_tool_calls)
        if response.provider_incomplete_reason and not response.tool_calls:
            return self._incomplete(response, duration_ms)
        if response.invalid_tool_calls and not response.tool_calls:
            return self._malformed(response.invalid_tool_calls)
        return DeliveryBoundary()

    def _record_invalid(self, invalid_tool_calls):
        loop_guard = self.guard
        for invalid in invalid_tool_calls:
            tool_name = str(invalid.get("name") or "unknown")
            loop_guard.record_tool_result(
                tool_name,
                {"invalid_json": True},
                True,
                effect="read",
            )
            loop_guard.record_tool_error(
                tool_name, "INVALID_TOOL_ARGUMENT_JSON",
                {"invalid_json": True},
            )

    def _incomplete(self, response, duration_ms):
        process, session = self.process, self.session
        _emit_observation = self.emit
        accumulated_text, provider_incomplete_reason = response.accumulated_text, response.provider_incomplete_reason
        pending_tool_calls = response.pending_tool_calls
        if process.task_kind == "answer" and not accumulated_text.strip():
            if self.no_progress_recoveries < 1:
                self.no_progress_recoveries += 1
                session.append_user(
                    load_prompt('recovery/no_progress') + diagnostic_hint('PROVIDER_NO_PROGRESS'),
                    message_type="host_provider_continuation",
                )
                _emit_observation({
                    "event": "provider_no_progress_recovery",
                    "attempt": self.no_progress_recoveries,
                    "max_attempts": 1,
                    "reason": provider_incomplete_reason,
                })
                return DeliveryBoundary(retry=True)
            session.quarantine_failed_provider_turn(
                "answer_incomplete_without_public_text",
            )
            transition(process, ProcessStatus.FAILED)
            return DeliveryBoundary(result=_make_result(
                process,
                session,
                "The model exhausted two bounded direct-answer attempts without "
                "producing a reply.",
                duration_ms=duration_ms,
                error_code="PROVIDER_NO_PROGRESS",
                error_message=(
                    "A direct-answer provider turn remained incomplete after one "
                    "bounded recovery; the failed turn was isolated from future input"
                ),
                outcome_status=OutcomeStatus.FAILED,
            ))
        unfinished_names = sorted({
            str(item.get("name") or "unknown")
            for item in pending_tool_calls.values()
            if not item.get("done")
        })
        for tool_name in unfinished_names:
            self.incomplete_tool_call_counts[tool_name] = (
                self.incomplete_tool_call_counts.get(tool_name, 0) + 1
            )
        repeated = [
            name for name in unfinished_names
            if self.incomplete_tool_call_counts.get(name, 0) >= 3
        ]
        if repeated:
            transition(process, ProcessStatus.FAILED)
            return DeliveryBoundary(result=_make_result(
                process,
                session,
                accumulated_text,
                duration_ms=duration_ms,
                error_code="PROVIDER_TOOL_CALL_TRUNCATION_STORM",
                error_message=(
                    "Provider repeatedly truncated function arguments for: "
                    + ", ".join(repeated)
                ),
                outcome_status=OutcomeStatus.FAILED,
            ))
        tool_hint = (
            load_prompt('recovery/unfinished_tool')
            if unfinished_names else ""
        )
        remember_answer_fragment(process, accumulated_text)
        session.append_user(
            render_prompt(
                'recovery/provider_continuation',
                provider_incomplete_reason=format(provider_incomplete_reason, ''),
                tool_hint=format(tool_hint, ''),
            ) + diagnostic_hint('PROVIDER_RESPONSE_INCOMPLETE'),
            message_type="host_provider_continuation",
        )
        return DeliveryBoundary(retry=True)

    def _malformed(self, invalid_tool_calls):
        session = self.session
        names = ", ".join(sorted({
            str(item.get("name") or "unknown") for item in invalid_tool_calls
        }))
        session.append_user(
            render_prompt('recovery/tool_arguments', names=format(names, '')) + diagnostic_hint('INVALID_TOOL_ARGUMENT_JSON'),
            message_type="host_tool_argument_error",
        )
        return DeliveryBoundary(retry=True)
