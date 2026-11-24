"""Failure facts must survive transport, routing, review and tool feedback."""
import json
import socket
import ssl
import urllib.error
from types import SimpleNamespace

import pytest

from backend.core.loop.provider_transport import HttpProviderTransport, ProviderHttpError
from backend.core.loop.provider_router import failure_kind, ProviderRoutesUnavailable
from backend.core.loop.provider_failure import provider_diagnostic, provider_failure_message


@pytest.mark.parametrize("error,reason,retryable", [
    (socket.timeout("synthetic"), "timeout", True),
    (urllib.error.URLError(socket.gaierror(-2, "synthetic")), "dns_failure", True),
    (ConnectionResetError("synthetic"), "connection_reset", True),
    (ConnectionRefusedError("synthetic"), "connection_refused", True),
    (ssl.SSLCertVerificationError("synthetic"), "tls_certificate", False),
    (json.JSONDecodeError("synthetic", "{", 1), "invalid_response", False),
    (ProviderHttpError(429, "synthetic", {}), "HTTP_429", True),
])
def test_real_transport_and_route_keep_distinct_cause(error, reason, retryable):
    def fail(*args, **kwargs):
        raise error
    with pytest.raises(Exception) as raised:
        list(HttpProviderTransport(open_url=fail).stream_sse(
            "https://not-called.invalid", {}, {}, timeout=1))
    assert failure_kind(raised.value)[1] == reason
    failure = provider_diagnostic(raised.value, stage="provider_request")
    assert failure["reason"] == reason and failure["retryable"] is retryable
    assert type(error).__name__ in failure["exception_types"]
    routed = ProviderRoutesUnavailable([{"provider_id": "configured", "reason": reason, "failure": failure}])
    result = provider_diagnostic(routed, stage="requirement_review")
    assert result["reason"] == reason and result["attempts"][0]["failure"] == failure
    assert result["retryable"] is retryable and result["effect_scope"] == "reviewed_calls"


def test_unavailable_review_is_not_a_semantic_rejection_and_executes_no_call(tmp_path_factory):
    from backend.core.loop.agent_tool import AgentTool
    from backend.core.loop.execution_contract import data_broker
    from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
    from backend.core.loop.session import AgentSession
    from backend.core.loop.alignment_review import AlignmentReview, AlignmentReviewHooks
    from backend.core.loop.runtime_outcomes import _make_result
    from backend.core.loop.tool_proposal_runtime import ToolProposalRuntime

    process = AgentProcess("failure-review", "supervisor", RingLevel.RING_0,
        active_task_id="task", session=AgentSession(), task_kind="answer",
        worktree_path=str(tmp_path_factory), status=ProcessStatus.RUNNING)
    emitted, notices, executed = [], [], []
    noop = lambda *a, **kw: None
    hooks = AlignmentReviewHooks(
        emit=lambda event, **kw: emitted.append((event, kw)), frontend_emit=None,
        notice=lambda *a, **kw: notices.append((a, kw)), refresh_prompt=noop,
        before_attempt=noop, route_notice=noop,
        result=lambda response, **kw: _make_result(process, process.session, response, **kw),
        cancellation=noop, budget_exhausted=noop)
    failure = provider_diagnostic(socket.timeout("synthetic"), stage="provider_request")
    error = ProviderRoutesUnavailable([{"provider_id": "configured", "reason": "timeout", "failure": failure}])
    outcome = AlignmentReview(process, None, hooks).failure(error)
    assert outcome["error"]["retryable"]
    assert outcome["error"]["details"]["details"]["failure"]["reason"] == "timeout"
    assert outcome["error"]["details"]["details"]["failure"]["stage"] == "requirement_review"
    assert "超时" in outcome["response"] and "ProviderRoutesUnavailable" not in outcome["response"]
    assert emitted[0][0]["failure"]["reason"] == "timeout"
    tool = AgentTool("business", "Business", {}, lambda args: executed.append(args),
        execution_contract=data_broker("test.business"))
    boundary = ToolProposalRuntime(process, process.session, lambda: {"business": tool},
        lambda **kw: outcome).review([{"name": "business", "id": "proposed", "args": {}}], "")
    assert boundary.result is outcome and not executed
    result = process.session.messages[-1]
    assert result["data"]["error_code"] == "ALIGNMENT_REVIEW_FAILED"
    assert result["data"]["review_state"] == "unavailable"
    assert result["data"]["failure"]["reason"] == "timeout"
    assert result["data"]["executed"] is False
    assert "boundary prevented" not in result["content"]


def test_legacy_interruption_keeps_unknown_cause_and_nonretryable_state():
    from backend.core.loop.llm import StreamInterruptedError
    assert failure_kind(StreamInterruptedError(retryable=False)) == (False, "transport_failure")
    failure = provider_diagnostic(StreamInterruptedError(), stage="main_response")
    assert failure["reason"] == "transport_failure"
    assert failure["effect_scope"] == "current_request"
    assert failure['code'] == 'PROVIDER_TRANSPORT_CAUSE_UNKNOWN'
    assert '具体原因未记录' in provider_failure_message(failure)


def test_route_aggregation_keeps_nested_leaf_and_distinct_mixed_causes():
    dns = provider_diagnostic(socket.gaierror(-2, 'synthetic'), stage='provider_request')
    same = ProviderRoutesUnavailable([{'provider_id': 'a', 'reason': 'transport_failure', 'failure': dns}])
    result = provider_diagnostic(same, stage='main_response')
    assert result['code'] == 'PROVIDER_DNS_FAILED'
    assert result['reason'] == 'dns_failure'
    assert result['stage'] == 'main_response'
    mixed = ProviderRoutesUnavailable([*same.attempts,
        {'provider_id': 'b', 'reason': 'configuration_unavailable'}])
    result = provider_diagnostic(mixed, stage='requirement_review')
    assert result['code'] == 'PROVIDER_ROUTES_MULTIPLE_FAILURES'
    assert result['causes'] == ['configuration_unavailable', 'dns_failure']
    assert result['attempts'][0]['failure'] == dns
    assert result['recovery_class'] == 'inspect_each_provider_attempt'
    assert provider_diagnostic(ProviderRoutesUnavailable([]), stage='main_response')['code'] == 'PROVIDER_ROUTES_MISSING'


@pytest.mark.parametrize('reason,code', [
    ('configuration_unavailable', 'PROVIDER_CONFIGURATION_UNAVAILABLE'),
    ('tools_unsupported', 'PROVIDER_TOOLS_UNSUPPORTED'),
    ('circuit_unavailable', 'PROVIDER_CIRCUIT_OPEN'),
    ('HTTP_401', 'PROVIDER_AUTHENTICATION_FAILED'),
    ('HTTP_429', 'PROVIDER_RATE_LIMITED'),
    ('HTTP_503', 'PROVIDER_SERVER_ERROR'),
])
def test_leaf_codes_are_registered_and_keep_actual_route_reason(reason, code):
    from backend.core.errors import error_payload
    result = provider_diagnostic(ProviderRoutesUnavailable([{'provider_id': 'a', 'reason': reason}]),
                                 stage='requirement_review')
    assert result['code'] == code and result['reason'] == reason
    assert error_payload(code)['error'] == code


def test_assessment_schema_errors_are_not_reported_as_transport_or_one_generic_error():
    from backend.core.loop.alignment_review import InvalidAlignmentAssessment
    codes = {InvalidAlignmentAssessment('synthetic', schema_error=value).code for value in (
        'invalid_json', 'object_required', 'provider_response_incomplete',
        'delivery_check_required', 'request_frame_required: synthetic', None)}
    assert len(codes) == 6


def test_http_rejection_keeps_redacted_service_detail():
    synthetic_key = 'sk-' + 'synthetic-fixture-for-redaction'
    error = ProviderHttpError(400, f'unsupported_parameter: max_tokens; key {synthetic_key}', {})
    result = provider_diagnostic(error, stage='requirement_review')
    assert result['code'] == 'PROVIDER_BAD_REQUEST'
    assert 'unsupported_parameter: max_tokens' in result['provider_response_excerpt']
    assert synthetic_key not in result['provider_response_excerpt']


def test_explicit_service_model_rejection_is_distinct_from_other_bad_requests():
    error = ProviderHttpError(400, json.dumps({'error':{'code':'400','message':'Unsupported model synthetic-model'}}), {})
    result = provider_diagnostic(error, stage='requirement_review')
    assert result['code'] == 'PROVIDER_MODEL_UNSUPPORTED'
    assert result['rejected_model'] == 'synthetic-model' and result['http_status'] == 400
    assert failure_kind(error) == (True,'model_unsupported')
    # No new certainty can be fabricated for old exception-only evidence.
    legacy = provider_diagnostic(RuntimeError('Unsupported model synthetic-model'),stage='main_response')
    assert legacy['code'] == 'PROVIDER_REQUEST_INVALID'


def test_budget_deadline_is_not_relabelled_as_provider_network_failure(tmp_path_factory):
    import time
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.executor import agent_step
    manager = AgentProcessManager()
    p = manager.fork(parent_id=None,role='supervisor',actor_kind='supervisor',ring_level=RingLevel.RING_0,
        tool_registry=ToolRegistry([]),max_steps=50,task_kind='answer',task_id='deadline-diagnostic',
        task_budget_limits={'max_seconds':30,'max_provider_calls':10})
    p.worktree_path=str(tmp_path_factory)
    class Provider:
        def stream_chat(self,*args,**kwargs):
            p.task_budget.deadline_monotonic = time.monotonic()-1
            raise TimeoutError('synthetic timeout after the deadline expired')
            yield
    result=agent_step(p,Provider(),instruction='Give a concise reply.')
    assert result['status'] == 'timed_out'
    assert result['error']['code'] == 'TASK_TREE_DEADLINE_EXCEEDED'
    details=result['error']['details']['details']
    assert details['phase'] == 'main_response'
    assert details['provider_failure']['code'] == 'PROVIDER_TIMEOUT'
