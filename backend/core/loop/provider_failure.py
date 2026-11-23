"""Bounded provider diagnostics, shared by routing and task outcomes.

Classification describes observed exceptions; it grants no retry, replay or
permission. Original execution/effect boundaries still decide continuation.
"""
import http.client
import json
import re
import socket
import ssl
import urllib.error


def _causes(error):
    pending, seen = [error], set()
    while pending and len(seen) < 12:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend((current.__cause__, getattr(current, "reason", None)))


def classify_provider_failure(error):
    chain = list(_causes(error))
    attempts = getattr(error, "attempts", None)
    if attempts is not None:
        rows = [dict(item) for item in attempts]
        # A router may retain the broad transport category alongside a more
        # specific diagnostic from the actual exception. Never discard that
        # leaf, or turn different failed routes into one invented root cause.
        reasons = {(item.get("failure") or {}).get("reason") or item.get("reason")
                   or "transport_failure" for item in rows}
        reason = next(iter(reasons)) if len(reasons) == 1 else (
            "mixed_route_failures" if rows else "routes_missing")
        retryable = any((item.get("failure") or {}).get("retryable", False)
                        or item.get("reason") == "circuit_unavailable" for item in rows)
        return {"reason": reason, "causes": sorted(reasons), "retryable": retryable,
                "failover": False, "attempts": rows,
                "exception_types": [type(item).__name__ for item in chain]}
    status = next((getattr(item, "status_code", None) for item in chain
                   if getattr(item, "status_code", None) is not None), None)
    if status is not None:
        result = {"reason": f"HTTP_{status}", "http_status": status,
                "retryable": status in {408, 429} or status >= 500,
                "failover": status in {401, 402, 403, 404, 408, 429} or status >= 500,
                "exception_types": [type(item).__name__ for item in chain]}
        body = next((getattr(item, 'body', '') for item in chain if getattr(item, 'body', '')), '')
        if isinstance(body, str) and body:
            from backend.core.storage.redaction import redact_for_persistence
            bounded = body[:8192]
            try:
                content = json.loads(bounded)
            except (ValueError, TypeError):
                content = bounded
            redacted = redact_for_persistence(content)
            result['provider_response_excerpt'] = (redacted if isinstance(redacted, str) else
                json.dumps(redacted, ensure_ascii=False))[:2000]
            service_error = content.get('error') if isinstance(content, dict) else None
            if isinstance(service_error, dict) and status in {400, 404}:
                # This is an explicit wire-protocol declaration, not a guess
                # from an arbitrary exception message or user task keywords.
                service_code = str(service_error.get('code') or '')
                unsupported = re.fullmatch(r'Unsupported model[ :]+([A-Za-z0-9_./:-]{1,160})',
                    str(service_error.get('message') or ''), re.IGNORECASE)
                if service_code in {'model_not_found', 'unsupported_model', 'model_not_supported'} or unsupported:
                    result.update(reason='model_unsupported', failover=True)
                    if unsupported:
                        result['rejected_model'] = unsupported.group(1)
        return result
    kinds = (
        ((socket.gaierror,), "dns_failure", True),
        ((ssl.SSLCertVerificationError,), "tls_certificate", False),
        ((ssl.SSLError,), "tls_failure", False),
        ((TimeoutError,), "timeout", True),
        ((json.JSONDecodeError, UnicodeDecodeError), "invalid_response", False),
        ((ConnectionResetError,), "connection_reset", True),
        ((ConnectionRefusedError,), "connection_refused", True),
        ((BrokenPipeError, http.client.IncompleteRead, http.client.RemoteDisconnected), "connection_closed", True),
        ((ConnectionError,), "connection_failure", True),
    )
    reason, retryable, failover = "request_error", False, False
    for types, category, retry in kinds:
        if any(isinstance(item, types) for item in chain):
            reason, retryable, failover = category, retry, True
            break
    if reason == "request_error" and any(
            type(item).__name__ == "StreamInterruptedError"
            or isinstance(item, urllib.error.URLError)
            or isinstance(item, RuntimeError) and
                ("connection failed" in str(item).lower() or "stream failed" in str(item).lower())
            for item in chain):
        retryable = bool(getattr(error, "retryable", True))
        reason, failover = "transport_failure", retryable
    result = {"reason": reason, "retryable": retryable, "failover": failover,
              "exception_types": [type(item).__name__ for item in chain]}
    numbers = [item.errno for item in chain if isinstance(item, OSError) and item.errno is not None]
    if numbers:
        result["os_error_numbers"] = numbers
    return result


_RECOVERY = {
    'HTTP_400': 'inspect_provider_request', 'HTTP_401': 'inspect_provider_credentials',
    'HTTP_402': 'inspect_provider_account', 'HTTP_403': 'inspect_provider_access',
    'HTTP_404': 'inspect_provider_endpoint', 'HTTP_429': 'respect_provider_retry_after',
    "dns_failure": "inspect_provider_dns",
    "timeout": "resume_at_current_boundary",
    "connection_refused": "inspect_provider_endpoint",
    "tls_certificate": "inspect_provider_certificate",
    "tls_failure": "inspect_provider_tls",
    "invalid_response": "inspect_provider_response_protocol",
    "request_error": "inspect_provider_request",
    "configuration_unavailable": "restore_configured_provider",
    "routes_missing": "configure_provider_route",
    "tools_unsupported": "select_tool_capable_provider",
    "circuit_unavailable": "wait_for_provider_circuit_probe",
    "mixed_route_failures": "inspect_each_provider_attempt",
    'model_unsupported': 'select_supported_provider_model',
}


def provider_failure_code(reason):
    """Stable leaf identity; the enclosing task code and stage stay intact."""
    if reason.startswith("HTTP_"):
        status = reason[5:]
        return {"400": "PROVIDER_BAD_REQUEST", "401": "PROVIDER_AUTHENTICATION_FAILED", "402": "PROVIDER_PAYMENT_REQUIRED",
                "403": "PROVIDER_ACCESS_DENIED", "404": "PROVIDER_ENDPOINT_NOT_FOUND",
                "408": "PROVIDER_TIMEOUT", "429": "PROVIDER_RATE_LIMITED"}.get(status,
                    "PROVIDER_SERVER_ERROR" if status.isdigit() and 500 <= int(status) <= 599
                    else "PROVIDER_HTTP_ERROR")
    return {
        "dns_failure": "PROVIDER_DNS_FAILED", "timeout": "PROVIDER_TIMEOUT",
        "connection_reset": "PROVIDER_CONNECTION_RESET", "connection_refused": "PROVIDER_CONNECTION_REFUSED",
        "connection_closed": "PROVIDER_CONNECTION_CLOSED", "connection_failure": "PROVIDER_CONNECTION_FAILED",
        "tls_certificate": "PROVIDER_TLS_CERTIFICATE_INVALID", "tls_failure": "PROVIDER_TLS_FAILED",
        "invalid_response": "PROVIDER_RESPONSE_INVALID", "request_error": "PROVIDER_REQUEST_INVALID",
        "configuration_unavailable": "PROVIDER_CONFIGURATION_UNAVAILABLE",
        "routes_missing": "PROVIDER_ROUTES_MISSING", "tools_unsupported": "PROVIDER_TOOLS_UNSUPPORTED",
        "circuit_unavailable": "PROVIDER_CIRCUIT_OPEN", "mixed_route_failures": "PROVIDER_ROUTES_MULTIPLE_FAILURES",
        'model_unsupported': 'PROVIDER_MODEL_UNSUPPORTED',
    }.get(reason, "PROVIDER_TRANSPORT_CAUSE_UNKNOWN")


def provider_diagnostic(error, *, stage, execution_state="not_started"):
    result = classify_provider_failure(error)
    result.pop("failover", None)
    result.update(code=provider_failure_code(result["reason"]), stage=stage, execution_state=execution_state,
        effect_scope="reviewed_calls" if stage == "requirement_review" else "current_request",
        recovery_class=_RECOVERY.get(result["reason"], "resume_at_current_boundary"
            if result["retryable"] else "inspect_provider_routes"))
    return result


def provider_failure_message(diagnostic):
    prefix = "需求检查未完成" if diagnostic["stage"] == "requirement_review" else "模型请求未完成"
    label = {
        "timeout": "连接超时", "dns_failure": "域名解析失败",
        "connection_reset": "连接被重置", "connection_refused": "连接被拒绝",
        "connection_closed": "连接提前结束", "connection_failure": "连接失败",
        "tls_certificate": "TLS 证书校验失败", "tls_failure": "TLS 连接失败",
        "invalid_response": "接口返回内容格式无效", "transport_failure": "传输失败，具体原因未记录",
        "routes_unavailable": "当前模型路由不可用", "request_error": "请求未被正常处理",
        "routes_missing": "未配置可用模型路由", "configuration_unavailable": "模型路由配置不可用",
        "tools_unsupported": "模型接口不支持本次工具调用", "circuit_unavailable": "模型路由熔断尚未恢复",
        "mixed_route_failures": "多个模型路由因不同原因失败，请分别查看各路由诊断",
        'model_unsupported': '模型接口明确表示不支持所配置的模型',
    }.get(diagnostic["reason"], diagnostic["reason"])
    return f"{prefix}：{label}。"


def expired_task_budget(process, failure):
    """Prefer an observed expired runtime deadline, retaining transport facts.

    A timeout capped by the task's remaining allowance is not proof of a slow
    network. This never changes an allowance, retries work or refunds usage.
    """
    if process.task_budget is None:
        return None
    from .budget import TaskBudgetExceeded
    try:
        process.task_budget.check_deadline()
    except TaskBudgetExceeded as exc:
        exc.details.update(phase=failure.get('stage'), provider_failure=failure)
        return exc
    return None
