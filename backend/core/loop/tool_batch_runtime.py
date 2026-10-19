"""Ordered tool batches, exact permission suspension and continuation.

All live/resumed calls share the normal ToolExecution/ToolPipeline. This
controller owns no grants and cannot weaken the policy boundary.
"""
from __future__ import annotations
import time
from dataclasses import dataclass
from .loop_bookkeeping import _latest_error_code
from .lifecycle import transition
from .models import ProcessStatus
from .outcome import OutcomeStatus
from .runtime_outcomes import _make_result
from .tool_execution import ToolExecution
from .tool_summary import compact_tool_summary
from .tool_result_support import _host_tool_error_result, _extract_referenced_files, _extract_error_code, _apply_host_task_transitions

@dataclass(frozen=True)
class LiveBatchResult:
    result: dict | None = None
    retry: bool = False
    results: list | None = None
    task_kind_changed: bool = False


class ToolBatchRuntime:
    def __init__(self, process, session, get_tools, loop_guard, ctx, workspace_path, turn_wall_started, emit):
        self.process, self.session, self.get_tools = process, session, get_tools
        self.loop_guard, self.ctx = loop_guard, ctx
        self.workspace_path, self.turn_wall_started, self.emit = workspace_path, turn_wall_started, emit

    @property
    def tools_dict(self):
        # Contracts/leases can rebuild the surface within this same turn.
        # Every live or resumed segment must use the current mounted catalog.
        return self.get_tools()

    def commit_results(self, tool_calls: list[dict], results: list, *, rolled_back: bool):
        """One provider-continuity and audit path for live and resumed calls."""
        results.sort(key=lambda item: int(item.call_index))
        # A provider may issue several independent calls to the same tool in
        # one response (for example, three different files).  A shared Host
        # precondition can make all of them fail identically, but that is one
        # failed strategy turn, not three retry attempts.  Count each
        # (tool,error) at most once per batch so parallel work cannot trip the
        # cross-turn storm guard immediately.  Different arguments are
        # different recovery strategies and must remain independently visible.
        recorded_storm_errors: set[tuple[str, str, str]] = set()
        for r in results:
            tool_call = tool_calls[r.call_index]
            ref_files = _extract_referenced_files(
                r.tool_name, r.data if r.data else {},
            )
            self.session.append_tool_result(
                r.formatted,
                tool_name=r.tool_name,
                tool_call_id=str(tool_call.get("id", "")),
                is_error=r.is_error,
                data=r.data,
                receipt=r.receipt,
                referenced_files=ref_files,
            )
            self.process.tool_receipts.append(dict(r.receipt or {}))
            from .loop_bookkeeping import capture_lesson_outcome
            capture_lesson_outcome(self.process, r, self.workspace_path,
                                   rolled_back=rolled_back, on_event=self.emit)
            search_fallback = (r.data or {}).get("provider_fallback") if r.tool_name == "web_search" else None
            if search_fallback:
                self.emit({"event": "provider_capability_fallback",
                                   "capability": "hosted_web_search", **dict(search_fallback)})
            self.emit({
                "event": "tool_result",
                "tool_name": r.tool_name,
                "tool_call_id": str(tool_call.get("id", "")),
                "is_error": r.is_error,
                "error": r.error,
                "duration_ms": r.duration_ms,
                "result_preview": str(r.formatted)[:2000],
                "compact_summary": compact_tool_summary(
                    r.tool_name, r.data, is_error=r.is_error,
                    error=str(r.error or ""),
                ),
                "diff": str((r.data or {}).get("diff") or "")[:100000],
                "receipt": dict(r.receipt or {}),
                "diagnostics": dict(r.diagnostics or {}),
                "tool_result": dict(r.spill or {}),
                "rolled_back": rolled_back,
            }, detail={
                "arguments": tool_call.get("args", {}),
                "formatted_result": r.formatted,
                "data": r.data,
                "receipt": dict(r.receipt or {}),
                "diagnostics": dict(r.diagnostics or {}),
            })
            tool = self.tools_dict.get(r.tool_name)
            effect = getattr(getattr(tool, "effect", "read"), "value", "read")
            self.loop_guard.record_tool_result(
                r.tool_name, tool_call.get("args", {}), r.is_error,
                effect=effect,
            )
            from backend.core.loop.operation_policy import is_effectful_mutation
            if not r.is_error and is_effectful_mutation(effect):
                self.process.successful_actions += 1
                if self.process.parent_id:
                    try:
                        from backend.core.loop.coordination import observe_interface_changes
                        manager = getattr(self.process, "_manager", None)
                        if manager is not None:
                            observed = observe_interface_changes(
                                manager, self.process, self.workspace_path,
                                summary=(
                                    f"Host observed declared interface changes after "
                                    f"{r.tool_name}."
                                ),
                            )
                            for coordination_event in observed.get("events", []):
                                self.emit({
                                    "event": "coordination_event",
                                    **coordination_event,
                                }, detail=coordination_event)
                    except (OSError, ValueError) as exc:
                        self.emit({
                            "event": "coordination_observation_failed",
                            "tool_name": r.tool_name,
                            "error": str(exc),
                        })
            if r.is_error:
                error_code = _extract_error_code(r)
                error_key = self.loop_guard.storm_key(
                    r.tool_name, error_code, tool_call.get("args", {}),
                )
                if error_key not in recorded_storm_errors:
                    self.loop_guard.record_tool_error(
                        r.tool_name, error_code, tool_call.get("args", {}),
                    )
                    recorded_storm_errors.add(error_key)

        self.ctx.artifacts["engineering_workflow"].invoke({"operation": "status"})


    def missing_permission(self, calls: list[dict], execution_id: str=''):
        """Return the first sensitive call that still lacks an exact grant."""
        from backend.core.loop.operation_policy import decide_tool_operation
        from backend.core.loop.permission_broker import matching_grant
        for call_index, call in enumerate(calls):
            tool = self.tools_dict.get(str(call.get("name") or ""))
            if tool is None:
                continue
            from .composite_runtime import call_key
            key = call_key(call, execution_id, call_index)
            if key in self.process.runtime_preferences.get("composite_continuations", {}):
                # Pipeline validates the saved definition, task and prefix
                # receipts. Do not consume another wrapper grant on resume.
                continue
            public_args = {
                key: value for key, value in dict(call.get("args") or {}).items()
                if not str(key).startswith("_")
            }
            policy = decide_tool_operation(tool, public_args)
            if policy.requires_user and matching_grant(
                self.process, tool.name, public_args,
                per_invocation=bool(getattr(tool, "approval_per_invocation", False)),
            ) is None:
                return call_index, tool, public_args
        return None


    def suspend_for_permission(self, calls: list[dict], execution_id: str, permission_call_index: int, tool, public_args: dict, *, nested: bool=False):
        """Persist the unstarted suffix before presenting its permission card."""
        from backend.core.loop.permission_broker import automatic_permission_request
        decision = automatic_permission_request(
            self.process, tool, public_args, self.workspace_path,
        )
        self.process.pending_tool_batch = {
            "execution_id": execution_id,
            "tool_calls": [dict(item) for item in calls],
            "permission_call_index": int(permission_call_index),
            "decision_id": decision.get("decision_id", ""),
            "created_at": time.time(),
            "nested_permission": nested,
        }
        transition(self.process, ProcessStatus.AWAITING_USER)
        self.emit({
            "event": "decision_required",
            "decision": decision,
        }, detail={"suspended_tool_calls": calls})
        from backend.core.loop.decision_support import format_decision_request
        result = _make_result(
            self.process, self.session, format_decision_request(decision),
            duration_ms=(time.time() - self.turn_wall_started) * 1000,
            outcome_status=OutcomeStatus.AWAITING_USER,
        )
        self.process.result = result
        return result


    def pause_nested_results(self, calls, results, execution_id):
        suspended = next((r for r in results if r.suspended), None)
        if suspended is None:
            return None
        index = int(suspended.call_index)
        self.commit_results(calls, [r for r in results if int(r.call_index) < index], rolled_back=False)
        permission = self.ctx.artifacts.pop("nested_permission")
        return self.suspend_for_permission(calls[index:], execution_id, 0,
            permission["tool"], permission["arguments"], nested=True)


    def execute_approved_segment(self, calls: list[dict], execution_id: str):
        """Execute and commit a permission-complete ordered batch segment."""
        if not calls:
            return []
        execution = ToolExecution(execution_id=execution_id, ctx=self.ctx, tool_calls=calls)
        execution.begin()
        results = execution.execute_batch(self.tools_dict)
        if not execution._rolled_back:
            execution.commit()
        paused = self.pause_nested_results(calls, results, execution_id)
        if paused is not None:
            return paused
        returned = {int(item.call_index) for item in results}
        for index, call in enumerate(calls):
            if index not in returned:
                results.append(_host_tool_error_result(
                    tool_name=str(call.get("name") or "unknown"),
                    execution_id=execution_id,
                    call_index=index,
                    error_name="TOOL_NOT_STARTED",
                    details={"execution_state": "not_started"},
                    next_actions=[{"action": "inspect_then_retry"}],
                ))
        self.commit_results(calls, results, rolled_back=bool(execution._rolled_back))
        if not execution._rolled_back:
            _apply_host_task_transitions(self.process, results)
        return results


    def execute_resumed_batch(self, batch: dict):
        calls = [dict(item) for item in list(batch.get("tool_calls") or [])]
        execution_id = str(batch.get("execution_id") or "permission-resume")
        action = str(batch.get("decision_action") or "")
        if action == "deny":
            if batch.get("nested_permission"):
                from .composite_runtime import discard
                discard(self.process, execution_id)
            denied_index = min(
                max(0, int(batch.get("permission_call_index") or 0)),
                max(0, len(calls) - 1),
            )
            prefix_result = self.execute_approved_segment(calls[:denied_index], execution_id)
            if isinstance(prefix_result, dict):
                return prefix_result
            denied_call = calls[denied_index:denied_index + 1]
            if denied_call:
                denied = _host_tool_error_result(
                    tool_name=str(denied_call[0].get("name") or "unknown"),
                    execution_id=execution_id,
                    call_index=0,
                    error_name="TOOL_PERMISSION_DENIED",
                    details={"execution_state": "not_started", "user_decision": "deny"},
                    next_actions=[{"action": "change_strategy"}],
                )
                self.commit_results(denied_call, [denied], rolled_back=False)
            calls = calls[denied_index + 1:]
            if not calls:
                return None

        missing = self.missing_permission(calls, execution_id)
        if missing is not None:
            missing_index, tool, public_args = missing
            # Calls before the next approval boundary are already authorized;
            # commit them now, then pause before the first unapproved effect.
            prefix_result = self.execute_approved_segment(calls[:missing_index], execution_id)
            if isinstance(prefix_result, dict):
                return prefix_result
            remaining = calls[missing_index:]
            return self.suspend_for_permission(
                remaining, execution_id, 0, tool, public_args,
            )
        segment_result = self.execute_approved_segment(calls, execution_id)
        return segment_result if isinstance(segment_result, dict) else None


    def execute_live_batch(self, tool_calls):
        process, session, loop_guard, ctx = self.process, self.session, self.loop_guard, self.ctx
        tools_dict, on_stream_event = self.tools_dict, self.emit
        _missing_permission, _suspend_tool_batch_for_permission = self.missing_permission, self.suspend_for_permission
        _pause_nested_results, _commit_tool_results = self.pause_nested_results, self.commit_results
        # Storm handling remains a loop concern.  Tool governance itself is
        # owned by ToolPipeline so expanded composite steps use the exact
        # same path and wrapper/component checks are never duplicated.
        import uuid
        execution_id = str(uuid.uuid4())
        missing_approval = _missing_permission(tool_calls, execution_id)
        if missing_approval is not None:
            permission_call_index, pending_tool, public_args = missing_approval
            return LiveBatchResult(result=_suspend_tool_batch_for_permission(
                tool_calls, execution_id, permission_call_index,
                pending_tool, public_args,
            ))
        filtered_calls, filtered_to_provider, results, deferred_nudges = self._filter_storm_calls(tool_calls, execution_id)

        execution = None
        if filtered_calls:
            execution = ToolExecution(
                execution_id=execution_id,
                ctx=ctx,
                tool_calls=filtered_calls,
            )
            execution.begin()
            executed_results = execution.execute_batch(tools_dict)
            for result in executed_results:
                local_index = int(result.call_index)
                if 0 <= local_index < len(filtered_to_provider):
                    result.call_index = filtered_to_provider[local_index]
                results.append(result)
            if not execution._rolled_back:
                execution.commit()

        rolled_back = bool(execution and execution._rolled_back)
        nested_pause = _pause_nested_results(tool_calls, results, execution_id)
        if nested_pause is not None:
            return LiveBatchResult(result=nested_pause)
        returned_indexes = {int(result.call_index) for result in results}
        # A crash can cancel calls that were already emitted by the
        # provider but never started. They still require explicit outputs.
        for provider_index, tool_call in enumerate(tool_calls):
            if provider_index in returned_indexes:
                continue
            results.append(_host_tool_error_result(
                tool_name=str(tool_call.get("name") or "unknown"),
                execution_id=execution_id,
                call_index=provider_index,
                error_name="TOOL_NOT_STARTED",
                details={
                    "execution_state": "not_started",
                    "rolled_back": rolled_back,
                },
                next_actions=[{
                    "action": "inspect_then_retry",
                    "effect": "Confirm current state before retrying a possibly related operation.",
                }],
            ))

        # Provider-turn atomicity: all tool outputs are contiguous and in
        # call order. Only after this loop may Host/user messages be added.
        _commit_tool_results(tool_calls, results, rolled_back=rolled_back)

        if rolled_back:
            rollback_reason = str(execution._rollback_reason or "tool execution failed")
            session.append_user(
                "[HOST ROLLBACK] The preceding tool batch was rolled back: "
                + rollback_reason
                + ". Inspect current state before retrying.",
                message_type="governance_nudge",
            )
            if on_stream_event:
                on_stream_event({
                    "event": "rollback_notification",
                    "process_id": process.process_id,
                    "execution_id": execution.execution_id,
                    "reason": rollback_reason,
                })
        for nudge in dict.fromkeys(deferred_nudges):
            session.append_user(nudge, message_type="governance_nudge")

        task_kind_changed = False
        if not rolled_back:
            task_kind_changed = _apply_host_task_transitions(process, results)

        if rolled_back:
            return LiveBatchResult(retry=True)

        return LiveBatchResult(results=results, task_kind_changed=task_kind_changed)


    def _filter_storm_calls(self, tool_calls, execution_id):
        process, loop_guard = self.process, self.loop_guard
        filtered_calls = []
        filtered_to_provider: list[int] = []
        results = []
        deferred_nudges: list[str] = []
        for provider_index, tc in enumerate(tool_calls):
            tool_name = tc["name"]
            # v0.45: Storm break
            storm_nudge = loop_guard.check_storm_break(
                tool_name,
                _latest_error_code(process, tool_name),
                tc.get("args", {}),
            )
            if storm_nudge:
                results.append(_host_tool_error_result(
                    tool_name=tool_name,
                    execution_id=execution_id,
                    call_index=provider_index,
                    error_name="TOOL_CALL_STORM_BLOCKED",
                    details={
                        "execution_state": "not_started",
                        "last_error_code": _latest_error_code(process, tool_name),
                        "guidance": storm_nudge,
                    },
                    next_actions=[{
                        "action": "change_strategy",
                        "effect": "Inspect the prior error and materially change the call before retrying.",
                    }],
                ))
                deferred_nudges.append(storm_nudge)
                continue

            filtered_calls.append(tc)
            filtered_to_provider.append(provider_index)

        return filtered_calls, filtered_to_provider, results, deferred_nudges
