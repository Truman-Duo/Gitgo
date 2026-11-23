"""Project committed tool outcomes into question and completion transitions."""
import json

from backend.core.prompt_templates import render_prompt

from .completion_protocol import CompletionClaim, HostCompletionEvaluator, HostCompletionEvidence
from .decision_support import format_decision_request
from .delivery_runtime import DeliveryBoundary
from .lifecycle import transition
from .loop_bookkeeping import _inc_nudge_counter
from .models import ProcessStatus
from .outcome import OutcomeStatus
from .runtime_outcomes import _make_result


class ToolOutcomeRuntime:
    def __init__(self, process, session, delivery, emit, pause_blocked, capture_recovery):
        self.process, self.session, self.delivery = process, session, delivery
        self.emit, self.pause_blocked, self.capture_recovery = emit, pause_blocked, capture_recovery
        self._maintenance_snapshot = {}

    def evaluate(self, results, content, duration_ms, window, signals):
        process, session = self.process, self.session
        if process.pending_decision is not None:
            decision = dict(process.pending_decision)
            transition(process, ProcessStatus.AWAITING_USER)
            self.emit({'event': 'decision_required', 'decision': decision})
            result = _make_result(process, session, format_decision_request(decision),
                duration_ms=duration_ms, outcome_status=OutcomeStatus.AWAITING_USER)
            process.result = result
            return DeliveryBoundary(result=result)
        claim = next((r for r in results if r.tool_name in {'complete_task', 'complete_review', 'complete_supervision'}
                      and not r.is_error and isinstance(r.data, dict) and r.data.get('accepted') is True), None)
        if claim is None:
            claim = next((r for r in results if r.tool_name == 'promote_agent_changes'
                          and not r.is_error and isinstance(r.data, dict) and r.data.get('promoted') is True
                          and r.data.get('completion_result') and (r.receipt or {}).get('committed') is True), None)
            if claim is None:
                return DeliveryBoundary()
            process.completion_claim = CompletionClaim.from_args({
                'result': claim.data['completion_result'], 'files': claim.data.get('changed_files') or [],
            }, step=process.steps_used, process=process)
        if claim.tool_name == 'complete_supervision' and claim.data.get('terminal_status') == 'failed':
            return self._supervised_failure(claim, duration_ms, window)
        if claim.tool_name == 'complete_supervision':
            text = str(claim.data.get('result', ''))
        elif process.completion_claim is not None:
            text = process.completion_claim.result
        elif isinstance(process.review_claim, dict):
            text = str(process.review_claim.get('summary', ''))
        else:
            text = content or 'Task completed'
        prepare = getattr(self.delivery, 'prepare_candidate', None)
        if prepare is not None:
            prepared = prepare(text)
            if isinstance(prepared, DeliveryBoundary):
                return prepared
            text = prepared
        stage_arguments = {"completion_stage_entry": True} if self.delivery.has_registered_stages() else {}
        guard = self.delivery.guard.check(process, 'TASK_COMPLETE', session, self.delivery.signal_bus, signals, **stage_arguments)
        if guard.is_complete:
            return self.delivery.finish(text, duration_ms, window, source=claim.tool_name, degraded=guard.degraded)
        if guard.blocked:
            if any(gate["kind"] == "workbench_delivery" for gate in guard.gates):
                return self.delivery.blocked(guard, content)
            self.emit({'event': 'completion_gate', 'accepted': False, 'source': claim.tool_name, 'reason': guard.nudge_text})
            _inc_nudge_counter(process, 'completion_tool_rejection')
            terminal = self.pause_blocked(guard, process.completion_claim.result if process.completion_claim else content)
            if terminal is not None:
                return DeliveryBoundary(result=terminal)
            session.append_host_steering(guard.nudge_text, steering_type='completion_tool_rejection', version=str(process.steps_used))
        return DeliveryBoundary()

    def _supervised_failure(self, claim, duration_ms, window):
        process, session = self.process, self.session
        report = str(claim.data.get('result', ''))
        evaluation = HostCompletionEvaluator.evaluate_supervisor_failure(process, report)
        self.emit({'event': 'completion_gate', 'accepted': evaluation.allowed,
                   'source': 'complete_supervision', 'terminal_status': 'failed',
                   **({} if evaluation.allowed else {'reason': '; '.join(evaluation.reasons)})})
        if evaluation.allowed:
            self.capture_recovery(process)
            transition(process, ProcessStatus.FAILED)
            return DeliveryBoundary(result=_make_result(process, session, report, duration_ms=duration_ms,
                error_code='SUPERVISED_DELIVERY_FAILED', error_message=report, outcome_status=OutcomeStatus.FAILED,
                window_action=window['action'], window_usage_ratio=window['usage_ratio']))
        _inc_nudge_counter(process, 'completion_tool_rejection')
        from .loop_guard import render_completion_guidance
        session.append_host_steering(render_completion_guidance(evaluation.gates),
            steering_type='completion_tool_rejection', version=str(process.steps_used))
        return DeliveryBoundary(retry=True)

    def notify_evidence_ready(self, results=()):
        self._notify_maintained_files(results)
        process = self.process
        if (process.task_kind == 'action' and process.completion_claim is None
                and not process._nudge_counters.get('evidence_ready', 0)
                and HostCompletionEvidence.collect(process).factual_ready):
            process._nudge_counters['evidence_ready'] = 1
            self.session.append_host_steering('[HOST EVIDENCE READY]\n'
                'The required Host-observed receipts and tests are present. Do not repeat the work. '
                'Review the result once, then either call complete_task or provide the final result '
                'followed by a standalone TASK_COMPLETE line.', steering_type='completion_evidence_ready',
                version=str(process.steps_used))

    def _notify_maintained_files(self, results):
        process = self.process
        if process.actor_kind != "supervisor" or process.parent_id:
            return
        for result in results:
            if result.tool_name == "declare_task_contract" and not result.is_error and isinstance(result.data, dict):
                for route in result.data.get("maintained_assets") or []:
                    process._nudge_counters["maintenance_asset:" + route["asset_id"]] = route["revision"]
        paths = {str(result.data["path"]).replace("\\", "/") for result in results
                 if result.tool_name == "read_file" and not result.is_error
                 and isinstance(result.data, dict) and result.data.get("path")}
        if not paths:
            return
        from .task_contract import get_task_contract
        contract = get_task_contract(process)
        from .asset_maintenance import ASSET_ROUTE_LIMIT, maintenance_routes
        storage = getattr(getattr(getattr(process, "_manager", None), "session_store", None), "_storage", None)
        try:
            routes = maintenance_routes(process, contract, storage, observed_paths=paths,
                                        snapshot_cache=self._maintenance_snapshot)
        except (OSError, RuntimeError, ValueError) as exc:
            self.emit({"event": "asset_routing_observation_failed", "code": getattr(exc, "code", type(exc).__name__)})
            return
        routes = [route for route in routes
                  if process._nudge_counters.get("maintenance_asset:" + route["asset_id"]) != route["revision"]]
        if routes:
            displayed = routes[:ASSET_ROUTE_LIMIT]
            self.session.append_host_steering(
                render_prompt("context/maintained_files",
                              routes=json.dumps(displayed, ensure_ascii=False, separators=(",", ":")),
                              more=(render_prompt("context/maintained_files_more", count=len(routes)-len(displayed)).strip()
                                    if len(routes) > len(displayed) else "")).strip(),
                steering_type="maintained_files", version=str(process.steps_used))
            for route in displayed:
                process._nudge_counters["maintenance_asset:" + route["asset_id"]] = route["revision"]
