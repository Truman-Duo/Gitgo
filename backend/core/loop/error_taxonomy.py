"""Error taxonomy for runtime error classification.

Four-dimensional classification: (Source, Severity, Retryability, Nature).
This is distinct from the governance signal severity/category system —
governance signals classify *policy violations*; this classifies *execution errors*.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ErrorSource(enum.Enum):
    LLM = "llm"
    TOOL = "tool"
    SYSTEM = "system"
    DAEMON = "daemon"


class ErrorSeverity(enum.Enum):
    FATAL = "fatal"    # task cannot continue
    ERROR = "error"    # current operation failed
    WARN = "warn"      # can continue with caution
    INFO = "info"      # record only


class Retryability(enum.Enum):
    RETRYABLE = "retryable"          # can retry indefinitely
    LIMITED = "limited"              # limited retries
    NON_RETRYABLE = "non_retryable"  # must not retry

    @classmethod
    def limited_times(cls, n: int) -> str:
        return f"limited({n})"


class ErrorNature(enum.Enum):
    """Distinguishes tool crashes from business failures.

    CRASH:  the tool itself malfunctioned (exception, timeout, OOM).
            Rollback the Execution.
    BUSINESS: the tool executed successfully but the business result
              is a failure (test exit_code != 0, lint error, build failure).
              Do NOT rollback — let the LLM see and fix the failure.
    """
    CRASH = "crash"
    BUSINESS = "business"


@dataclass(frozen=True)
class ClassifiedError:
    source: ErrorSource
    severity: ErrorSeverity
    retryability: Retryability
    nature: ErrorNature
    code: str
    message: str
    original: Exception | None = field(default=None, compare=False)

    @property
    def is_crash(self) -> bool:
        return self.nature == ErrorNature.CRASH

    @property
    def is_business(self) -> bool:
        return self.nature == ErrorNature.BUSINESS

    @property
    def is_retryable(self) -> bool:
        return self.retryability != Retryability.NON_RETRYABLE

    def to_dict(self) -> dict:
        return {
            "source": self.source.value,
            "severity": self.severity.value,
            "retryability": self.retryability.value,
            "nature": self.nature.value,
            "code": self.code,
            "message": self.message,
        }

    def format_for_llm(self) -> str:
        """Format for inclusion in tool_result text shown to the LLM."""
        return f"[{self.source.value.upper()}/{self.nature.value.upper()}/{self.code}] {self.message}"


# --- Classification helpers ---

# HTTP status codes that should never be retried
_NON_RETRYABLE_HTTP = {400, 401, 402, 403}


def classify_http_error(status_code: int, message: str = "",
                        original: Exception | None = None) -> ClassifiedError:
    if status_code == 429:
        return ClassifiedError(
            source=ErrorSource.LLM,
            severity=ErrorSeverity.ERROR,
            retryability=Retryability.LIMITED,
            nature=ErrorNature.CRASH,
            code="RATE_LIMITED",
            message=message or f"HTTP {status_code}",
            original=original,
        )
    if 500 <= status_code < 600:
        return ClassifiedError(
            source=ErrorSource.LLM,
            severity=ErrorSeverity.ERROR,
            retryability=Retryability.RETRYABLE,
            nature=ErrorNature.CRASH,
            code=f"HTTP_{status_code}",
            message=message or f"HTTP {status_code}",
            original=original,
        )
    if status_code in _NON_RETRYABLE_HTTP:
        return ClassifiedError(
            source=ErrorSource.LLM,
            severity=ErrorSeverity.FATAL,
            retryability=Retryability.NON_RETRYABLE,
            nature=ErrorNature.CRASH,
            code=f"HTTP_{status_code}",
            message=message or f"HTTP {status_code}",
            original=original,
        )
    return ClassifiedError(
        source=ErrorSource.LLM,
        severity=ErrorSeverity.ERROR,
        retryability=Retryability.NON_RETRYABLE,
        nature=ErrorNature.CRASH,
        code=f"HTTP_{status_code}",
        message=message or f"HTTP {status_code}",
        original=original,
    )


def classify_network_error(message: str = "",
                           original: Exception | None = None) -> ClassifiedError:
    return ClassifiedError(
        source=ErrorSource.LLM,
        severity=ErrorSeverity.ERROR,
        retryability=Retryability.RETRYABLE,
        nature=ErrorNature.CRASH,
        code="NETWORK_ERROR",
        message=message or "Network error",
        original=original,
    )


def classify_timeout_error(message: str = "",
                           original: Exception | None = None) -> ClassifiedError:
    return ClassifiedError(
        source=ErrorSource.LLM,
        severity=ErrorSeverity.ERROR,
        retryability=Retryability.RETRYABLE,
        nature=ErrorNature.CRASH,
        code="TIMEOUT",
        message=message or "Request timed out",
        original=original,
    )


def classify_context_overflow(message: str = "",
                              original: Exception | None = None) -> ClassifiedError:
    return ClassifiedError(
        source=ErrorSource.LLM,
        severity=ErrorSeverity.ERROR,
        retryability=Retryability.LIMITED,
        nature=ErrorNature.CRASH,
        code="CONTEXT_OVERFLOW",
        message=message or "Context window exceeded",
        original=original,
    )


def classify_tool_error(exception: Exception,
                        tool_name: str = "",
                        timeout: bool = False) -> ClassifiedError:
    """Classify a tool execution error.

    Defaults to TOOL/CRASH — the caller (tool_pipeline) should override
    nature to BUSINESS when the tool completed but returned a business failure.
    """
    if timeout:
        return ClassifiedError(
            source=ErrorSource.TOOL,
            severity=ErrorSeverity.ERROR,
            retryability=Retryability.NON_RETRYABLE,
            nature=ErrorNature.CRASH,
            code="TOOL_TIMEOUT",
            message=f"Tool '{tool_name}' timed out" if tool_name else "Tool timed out",
            original=exception,
        )
    return ClassifiedError(
        source=ErrorSource.TOOL,
        severity=ErrorSeverity.ERROR,
        retryability=Retryability.NON_RETRYABLE,
        nature=ErrorNature.CRASH,
        code="TOOL_CRASH",
        message=str(exception)[:200],
        original=exception,
    )


def classify_business_failure(code: str, message: str) -> ClassifiedError:
    """Classify a business-level failure (test fail, lint error, etc.).

    These are NOT crashes — the tool ran correctly but the business check failed.
    """
    return ClassifiedError(
        source=ErrorSource.TOOL,
        severity=ErrorSeverity.WARN,
        retryability=Retryability.NON_RETRYABLE,
        nature=ErrorNature.BUSINESS,
        code=code,
        message=message,
    )
