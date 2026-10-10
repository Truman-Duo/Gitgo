"""ToolPipeline —— 五步工具执行管道。

替代 ToolDispatcher：prepare → validate → before → execute → after。

使用 EventBus 发射事件——不直接知道 SignalBus。任何订阅者（治理、转录、遥测）
都可以独立监听，不入侵管道代码。

保留 RingGate 权限检查 + HistoryManager 审计日志。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backend.core.loop.gate import RingGate
from backend.core.loop.operation_policy import (
    is_effectful_mutation, is_observation_effect,
)


class ToolExecutionCancelled(RuntimeError):
    """An isolated/cooperative tool acknowledged cancellation."""

    def __init__(self, message, *, effect_state=""):
        super().__init__(message)
        self.effect_state = effect_state if effect_state in {"not_committed", "ambiguous"} else ""

if TYPE_CHECKING:
    from backend.core.loop.agent_tool import AgentTool
    from backend.core.loop.event_bus import EventBus
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.models import AgentProcess


@dataclass
class ToolResult:
    """工具执行结果。包含 Dashboard 可直接消费的全量字段。"""
    id: str = ""
    tool_name: str = ""
    execution_id: str = ""
    call_index: int = 0
    allowed: bool = True
    is_error: bool = False
    data: dict | None = None
    formatted: str = ""
    error: str = ""
    duration_ms: float = 0.0
    truncated: bool = False
    persist_path: str | None = None
    spill: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    receipt: dict = field(default_factory=dict)


class ToolPipeline:
    """五步管道：prepare_args → validate_schema → before → execute → after。

    与 ToolDispatcher 的关系：ToolPipeline 是 ToolDispatcher 的升级版。
    保留 RingGate.check() 和 HistoryManager 审计，新增 validate + before/after 钩子。
    """

    def __init__(self, gate: RingGate | None = None):
        self._gate = gate or RingGate()

    def execute(
        self,
        tool_call: dict,
        tool: "AgentTool",
        ctx: "ExecutionContext",
        execution_id: str,
        call_index: int = 0,
    ) -> ToolResult:
        """执行单次工具调用。"""
        tool_name = tool_call.get("name", tool.name)
        # Leading-underscore fields are Host authority, never model input.
        # Strip before prepare_args so injected roots/config cannot survive a
        # public relative path such as ../private merely by avoiding preflight.
        supplied_args = tool_call.get("args", {})
        raw_args = {
            key: value for key, value in supplied_args.items()
            if not str(key).startswith("_")
        } if isinstance(supplied_args, dict) else supplied_args

        start = time.time()

        if ctx.is_cancelled() or getattr(ctx.process, "cancel_requested", False):
            return self._error_result(
                tool_name, execution_id, call_index, start,
                "tool execution cancelled before start",
                diagnostics={"nature": "system", "code": "TOOL_CANCELLED"},
            )

        # Validate Host registration before prepare hooks, approval consumption,
        # or any mutation. Caller-provided arguments never select authority.
        try:
            if tool_name != tool.name:
                raise ValueError("Tool call does not match its Host registration identity")
            execution_contract = tool.validate_execution_contract()
        except (AttributeError, ValueError) as exc:
            result = self._error_result(
                tool_name, execution_id, call_index, start, str(exc),
                diagnostics={"nature": "system", "code": "SANDBOX_POLICY_INVALID"},
            )
            result.receipt["effect_state"] = "not_committed"
            return result

        from backend.core.loop.operation_policy import (
            PolicyDisposition, decide_tool_operation,
        )
        operation_policy = decide_tool_operation(tool, dict(raw_args or {}))
        explicit_user_grant = None
        if operation_policy.disposition == PolicyDisposition.DENY:
            return self._error_result(
                tool_name, execution_id, call_index, start,
                "tool is denied by its execution contract",
                diagnostics={"nature": "governance", "code": "TOOL_DENIED"},
            )
        if operation_policy.requires_user:
            approvals = ctx.artifacts.get("approvals", set())
            from backend.core.loop.permission_broker import arguments_digest, matching_grant
            public_raw = {
                key: value for key, value in dict(raw_args or {}).items()
                if not str(key).startswith("_")
            }
            digest = arguments_digest(public_raw)
            grant = matching_grant(
                ctx.process, tool_name, public_raw,
                per_invocation=bool(getattr(tool, "approval_per_invocation", False)), tool=tool,
            )
            legacy_approved = (
                tool_name in approvals
                and not getattr(tool, "approval_per_invocation", False)
            )
            if not legacy_approved and grant is None:
                from backend.core.errors import error_payload
                sensitive = bool(getattr(tool, "approval_per_invocation", False))
                public_args = {
                    key: value for key, value in public_raw.items()
                    if not str(key).startswith("_")
                }
                declared_resources = [
                    str(item) for item in list(getattr(tool, "resources", []) or [])
                    if str(item).strip()
                ]
                info = error_payload(
                    "SENSITIVE_TOOL_APPROVAL_REQUIRED" if sensitive else "APPROVAL_GRANT_INVALID",
                    details={
                        "tool_name": tool_name,
                        "effect": getattr(getattr(tool, "effect", "read"), "value", str(getattr(tool, "effect", "read"))),
                        "arguments_digest": digest,
                        "approval_scope": operation_policy.grant_scope,
                        "risk": operation_policy.risk,
                    },
                    next_actions=[{
                        "action": "request_permission",
                        "tool_name": tool_name,
                        "arguments": public_args,
                        "resource": declared_resources[0] if declared_resources else f"capability://{tool_name}",
                    }],
                )
                return self._error_result(
                    tool_name, execution_id, call_index, start,
                    "tool requires explicit approval",
                    diagnostics={
                        "nature": "governance",
                        "code": info["error"],
                        "source": "host",
                        "error_info": info["error_info"],
                    },
                )
            explicit_user_grant = grant

        # Step 1: prepare_args
        ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                "ToolPrepareStarted", execution_id, tool_name, call_index,
            )
        )
        try:
            args = tool.prepare_args(raw_args) if tool.prepare_args else raw_args
            if isinstance(args, dict):
                # Tool definitions are daemon-global, while an Agent's
                # execution workspace can become a task-scoped worktree after
                # delegation.  Private workspace arguments are Host authority:
                # rebind them at the last common pipeline boundary so isolated
                # runners, dynamic composite components and legacy wrappers
                # cannot accidentally read/write the root checkout.
                args = dict(args)
                if "_workspace" in args:
                    args["_workspace"] = str(ctx.workspace_path)
                if "workspace_path" in args:
                    args["workspace_path"] = str(ctx.workspace_path)
                if tool_name in {"web_search", "web_fetch"}:
                    # Host configuration is private runtime authority.  Bind it
                    # here so A, B and expanded composite tools share one
                    # provider path and the model cannot select another URL.
                    if tool_name == "web_search":
                        args["_web_search_endpoint"] = str(
                            ctx.process.runtime_preferences.get("web_search_endpoint") or ""
                        )
                        args["_web_search_engine"] = str(
                            ctx.process.runtime_preferences.get("web_search_engine") or "duckduckgo"
                        )
                        args["_web_search_mode"] = str(
                            ctx.process.runtime_preferences.get("web_search_mode") or "auto"
                        )
                        args["_web_search_provider_id"] = str(ctx.process.provider_id or "")
                from backend.core.loop.permission_broker import (
                    authorize_external_resources,
                    out_of_scope_resources,
                    remap_project_paths,
                )
                args = remap_project_paths(ctx.process, args, str(ctx.workspace_path))
                outside = out_of_scope_resources(args, str(ctx.workspace_path))
                effect_value = getattr(
                    getattr(tool, "effect", "read"), "value", str(getattr(tool, "effect", "read")),
                )
                allowed_roots, scope_error = authorize_external_resources(
                    ctx.process, tool_name, effect_value,
                    {key: value for key, value in args.items() if not str(key).startswith("_")},
                    outside, tool=tool,
                )
                if scope_error is not None:
                    return self._error_result(
                        tool_name, execution_id, call_index, start,
                        str(scope_error.get("message") or "resource scope approval required"),
                        diagnostics={
                            "nature": "governance",
                            "code": "RESOURCE_SCOPE_APPROVAL_REQUIRED",
                            "source": "host",
                            "error_info": scope_error,
                        },
                    )
                args["_allowed_roots"] = allowed_roots
        except Exception as exc:
            return self._error_result(
                tool_name, execution_id, call_index, start,
                f"prepare_args failed: {exc}",
            )

        # Step 2: validate_schema
        ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                "ToolValidateStarted", execution_id, tool_name, call_index,
            )
        )
        try:
            _validate_args(args, tool.parameters)
        except ValueError as exc:
            return self._error_result(
                tool_name, execution_id, call_index, start, str(exc),
                diagnostics={
                    "nature": "business",
                    "code": "INVALID_TOOL_ARGUMENTS",
                    "source": "llm",
                },
            )

        # Validate prepared arguments before checking engineering prerequisites.
        # Invalid model arguments remain correctable business errors. Exact
        # approval is independent of prerequisite evidence and is consumed only
        # after both checks succeed. Wrappers defer this guard to their leaves.
        spec = getattr(tool, "composite_spec", None) or {}
        if getattr(ctx.process, "read_context_snapshot", None) and (not spec or spec.get("execution_mode") in {"authored_pure_python", "authored_privileged_python"}):
            from backend.core.loop.engineering_workflow import EngineeringWorkflow
            workflow = ctx.artifacts.get("engineering_workflow") or EngineeringWorkflow(ctx.process)
            try:
                with ctx.process._context_lock:
                    guard = workflow.guard(tool_name, getattr(tool.effect, "value", tool.effect), dict(args or {}))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                workflow.notice("WORKFLOW_RECOVERY_REQUIRED", f"工程前置检查不可用：{exc}；操作尚未开始。")
                guard = {"allowed": False, "reason": "engineering prerequisite check unavailable"}
            if not guard.get("allowed", True):
                return self._error_result(
                    tool_name, execution_id, call_index, start, guard["reason"],
                    diagnostics={"nature": "governance", "code": "ENGINEERING_PREREQUISITE_REQUIRED"},
                )
        # Preparation/prerequisite callbacks must not replace execution authority
        # before an exact grant is spent.
        try:
            tool.validate_execution_contract()
        except (AttributeError, ValueError) as exc:
            result = self._error_result(
                tool_name, execution_id, call_index, start, str(exc),
                diagnostics={"nature": "system", "code": "SANDBOX_POLICY_INVALID"},
            )
            result.receipt["effect_state"] = "not_committed"
            return result
        if explicit_user_grant is not None:
            consumed = matching_grant(
                ctx.process, tool_name, public_raw,
                per_invocation=bool(getattr(tool, "approval_per_invocation", False)), tool=tool, consume=True,
            )
            if consumed is None:
                return self._error_result(
                    tool_name, execution_id, call_index, start, "approval is no longer available",
                    diagnostics={"nature": "governance", "code": "APPROVAL_GRANT_INVALID"},
                )

        # One canonical governance path.  Composite wrappers are transparent:
        # their compiled component calls are authorized below, so the same
        # semantic operation is not checked once as a wrapper and again as a
        # base tool.
        # A durable user decision is the highest authority inside its exact
        # tool/task/resource scope. It overrides ordinary governance policy,
        # but not cancellation, argument validation, resource containment or
        # the isolated-process boundary enforced elsewhere in this pipeline.
        if (
            ctx.tool_authorizer is not None
            and not getattr(tool, "composite_spec", None)
            and explicit_user_grant is None
        ):
            public_args = {
                key: value for key, value in args.items()
                if not str(key).startswith("_")
            }
            try:
                authorization = ctx.tool_authorizer(tool_name, public_args)
            except Exception as exc:
                return self._error_result(
                    tool_name, execution_id, call_index, start,
                    f"tool governance check failed: {exc}",
                    diagnostics={"nature": "system", "code": "TOOL_GOVERNANCE_FAILED"},
                )
            if not authorization.get("allowed", True):
                return self._error_result(
                    tool_name, execution_id, call_index, start,
                    str(authorization.get("reason", "blocked by governance")),
                    diagnostics={"nature": "governance", "code": "TOOL_BLOCKED"},
                )

        # Step 3: before_hooks → RingGate
        ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                "ToolBeforeHooksStarted", execution_id, tool_name, call_index,
                data={"args": args},
            )
        )
        gate_result = self._gate.check(ctx.process, tool_name)
        if not gate_result.allowed:
            return self._error_result(
                tool_name, execution_id, call_index, start,
                gate_result.error or "blocked by gate",
            )

        # Governance/before-hook callbacks must not replace execution authority
        # before execution or its journal starts.
        try:
            tool.validate_execution_contract()
        except (AttributeError, ValueError) as exc:
            result = self._error_result(
                tool_name, execution_id, call_index, start, str(exc),
                diagnostics={"nature": "system", "code": "SANDBOX_POLICY_INVALID"},
            )
            result.receipt["effect_state"] = "not_committed"
            return result

        # Step 4: execute
        ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                "ToolExecuteStarted", execution_id, tool_name, call_index,
            )
        )
        invocation_path = None
        effect_value = getattr(tool.effect, "value", str(tool.effect))
        if is_effectful_mutation(effect_value):
            invocation_path = self._write_invocation_state(
                ctx, tool, tool_name, execution_id, call_index,
                state="running", effect_state="not_committed",
            )
            if not invocation_path:
                result = self._error_result(
                    tool_name, execution_id, call_index, start,
                    "INVOCATION_JOURNAL_UNAVAILABLE",
                    diagnostics={
                        "nature": "system",
                        "code": "INVOCATION_JOURNAL_UNAVAILABLE",
                    },
                )
                result.receipt.update({
                    "effect": effect_value,
                    "cancellation": getattr(
                        tool.cancellation, "value", str(tool.cancellation),
                    ),
                    "effect_state": "not_committed",
                    "invocation_path": None,
                })
                return result
        try:
            # prepare hooks and mutable registry objects cannot substitute a new
            # callable/authority after the initial admission check.
            tool.validate_execution_contract()
            effective_timeout = float(tool.timeout)
            if tool.timeout_argument:
                requested_timeout = float(args.get(tool.timeout_argument) or tool.timeout_default)
                effective_timeout = min(effective_timeout, max(1.0, requested_timeout) + 5)
            budget = getattr(ctx.process, "task_budget", None)
            if budget is not None:
                allowance = budget.reserve_time(effective_timeout + 5, reason=f"tool_phase:{tool_name}")
                effective_timeout = min(effective_timeout, allowance)
            if getattr(tool, "composite_spec", None):
                dynamic_spec = dict(tool.composite_spec or {})
                if dynamic_spec.get("execution_mode") in {
                    "authored_pure_python", "authored_privileged_python",
                }:
                    source_ref = str(dynamic_spec.get("source_ref") or "")
                    try:
                        if source_ref:
                            if ctx.storage is None:
                                raise RuntimeError("saved tool storage is unavailable")
                            source = ctx.storage.read_blob(source_ref).decode("utf-8")
                            import hashlib
                            if hashlib.sha256(source.encode("utf-8")).hexdigest() != str(
                                dynamic_spec.get("source_sha256") or ""
                            ):
                                raise RuntimeError("saved tool source digest mismatch")
                            dynamic_spec["_source"] = source
                    except (KeyError, ValueError, RuntimeError, OSError, UnicodeError) as exc:
                        from backend.core.errors import error_payload
                        result_data = error_payload(
                            "CUSTOM_TOOL_SOURCE_INTEGRITY_FAILED", message=str(exc),
                            details={"name": str(dynamic_spec.get("name") or tool_name),
                                     "version": int(dynamic_spec.get("version") or 0)},
                            next_actions=[{"action": "detach_version"},
                                          {"action": "inspect_storage"}],
                        )
                    else:
                        result_data = self._execute_isolated(
                            (
                                "authored_privileged_python"
                                if dynamic_spec.get("execution_mode") == "authored_privileged_python"
                                else "authored_python"
                            ),
                            {**args, "_workspace": str(ctx.workspace_path), "_dynamic_spec": dynamic_spec},
                            effective_timeout,
                            cancellation_event=ctx.cancellation,
                        )
                else:
                    result_data = self._execute_composite_plan(
                        tool, args, ctx, execution_id, call_index,
                    )
            # Host registration selects native execution independently of business effect.
            elif execution_contract.kind.value == "native_process":
                result_data = self._execute_isolated(
                    getattr(tool, "runner_name", "") or tool_name,
                    {**args, "_workspace": str(ctx.workspace_path)}, effective_timeout,
                    cancellation_event=ctx.cancellation,
                )
            else:
                result_data = tool.execute(args)
            if tool.finalize_result is not None:
                result_data = tool.finalize_result(result_data)
        except ToolExecutionCancelled as exc:
            effect_value = getattr(tool.effect, "value", str(tool.effect))
            effect_state = exc.effect_state or (
                "not_committed" if not is_effectful_mutation(effect_value)
                else "ambiguous"
            )
            self._write_invocation_state(
                ctx, tool, tool_name, execution_id, call_index,
                state="cancelled",
                effect_state=effect_state,
                path=invocation_path,
            )
            result = self._error_result(
                tool_name, execution_id, call_index, start, str(exc),
                diagnostics={"nature": "system", "code": "TOOL_CANCELLED"},
            )
            result.receipt.update({
                "effect": getattr(tool.effect, "value", str(tool.effect)),
                "cancellation": getattr(tool.cancellation, "value", str(tool.cancellation)),
                "effect_state": effect_state,
                "invocation_path": invocation_path,
            })
            return result
        except Exception as exc:
            self._write_invocation_state(
                ctx, tool, tool_name, execution_id, call_index,
                state="failed", effect_state="ambiguous",
                path=invocation_path,
            )
            # v0.45: classify error for CRASH vs BUSINESS distinction
            from backend.core.loop.error_taxonomy import classify_tool_error, ErrorNature
            classified = classify_tool_error(exc, tool_name=tool_name)
            label = classified.format_for_llm()
            result = self._error_result(
                tool_name, execution_id, call_index, start,
                f"{label} | {exc}",
                diagnostics={
                    "nature": classified.nature.value,
                    "code": classified.code,
                    "source": classified.source.value,
                },
            )
            result.receipt.update({
                "effect": getattr(tool.effect, "value", str(tool.effect)),
                "cancellation": getattr(tool.cancellation, "value", str(tool.cancellation)),
                "effect_state": "ambiguous",
                "invocation_path": invocation_path,
            })
            return result

        self._publish_tool_notices(ctx, tool_name, execution_id, call_index, result_data)
        business_error = ""
        if isinstance(result_data, dict):
            business_error = str(result_data.get("error", ""))
            if not business_error and result_data.get("success") is False:
                business_error = "COMMAND_EXIT_NONZERO"
            if not business_error and result_data.get("passed") is False:
                business_error = "TEST_FAILED"
        if business_error:
            effect_value = getattr(tool.effect, "value", str(tool.effect))
            effect_state = str(result_data.get("effect_state", "")) or (
                "not_committed" if not is_effectful_mutation(effect_value)
                else "ambiguous"
            )
            self._write_invocation_state(
                ctx, tool, tool_name, execution_id, call_index,
                state="business_error", effect_state=effect_state,
                path=invocation_path,
            )
            code = business_error
            detail = str(result_data.get("detail", ""))
            if code == "COMMAND_EXIT_NONZERO" and not detail:
                detail = f"exit_code={result_data.get('exit_code')}"
            message = code if not detail else f"{code}: {detail}"
            rollback_required = bool(result_data.get("_rollback_required"))
            error_info = result_data.get("error_info")
            if code == "COMMAND_EXIT_NONZERO" and not isinstance(error_info, dict):
                from backend.core.errors import error_payload
                command_error = error_payload(
                    "COMMAND_EXIT_NONZERO",
                    details={
                        "exit_code": result_data.get("exit_code"),
                        "stdout_present": bool(result_data.get("stdout")),
                        "stderr_present": bool(result_data.get("stderr")),
                    },
                    next_actions=[
                        {
                            "action": "inspect_captured_output",
                            "effect": "Use the stdout/stderr included in this result; do not repeat the same command merely to recover diagnostics.",
                        },
                        {
                            "action": "fix_or_change_strategy",
                            "effect": "Revise the implementation, test, arguments, or execution strategy according to the concrete diagnostic.",
                        },
                    ],
                )
                error_info = command_error["error_info"]
            result = self._error_result(
                tool_name, execution_id, call_index, start, message,
                diagnostics={
                    "nature": "system" if rollback_required else "business",
                    "code": code,
                    "rollback_required": rollback_required,
                    **({"error_info": error_info} if isinstance(error_info, dict) else {}),
                },
            )
            result.data = result_data
            # Business failures are successful tool executions with an
            # unsuccessful domain result.  Preserve the bounded stdout/stderr,
            # test report, or validation details in the model-visible result;
            # reducing them to only ``exit_code=1`` forces the model to guess
            # and repeat the same command, which is both slow and expensive.
            model_payload = {
                "error": code,
                "message": message,
                **({"error_info": error_info} if isinstance(error_info, dict) else {}),
                "result": result_data,
            }
            formatted, truncated, persist_path, spill = _maybe_truncate(
                tool_name, model_payload, ctx.workspace_path,
                storage=ctx.storage,
                process_id=str(getattr(ctx.process, "process_id", "")),
                task_id=str(getattr(ctx.process, "active_task_id", "")),
            )
            result.formatted = formatted.replace(
                f"[工具 {tool_name} 结果]",
                f"[工具 {tool_name} 可恢复错误]",
                1,
            )
            result.truncated = truncated
            result.persist_path = persist_path
            result.spill = spill
            result.artifacts = {"tool_result": spill} if spill else {}
            if isinstance(error_info, dict):
                result.diagnostics.update({
                    "catalog_id": str(error_info.get("catalog_id") or ""),
                    "occurrence_id": str(error_info.get("occurrence_id") or ""),
                    "retryable": bool(error_info.get("retryable", False)),
                    "recovery_class": str(error_info.get("recovery_class") or ""),
                })
            result.receipt.update({
                "effect": effect_value,
                "cancellation": getattr(tool.cancellation, "value", str(tool.cancellation)),
                "effect_state": effect_state,
                "invocation_path": invocation_path,
                "task_id": str(getattr(ctx.process, "active_task_id", "") or getattr(ctx.process, "process_id", "")),
            })
            self._attach_result_facts(result.receipt, tool_name, result_data, args)
            if isinstance(error_info, dict):
                result.receipt.update({
                    "error_catalog_id": str(error_info.get("catalog_id") or ""),
                    "error_occurrence_id": str(error_info.get("occurrence_id") or ""),
                    "retryable": bool(error_info.get("retryable", False)),
                })
            if spill:
                result.receipt["tool_result_locator"] = spill["locator"]
                result.receipt["tool_result_digest"] = spill["digest"]
                result.receipt["tool_result_chars"] = spill["total_chars"]
            self._attach_child_lineage(result.receipt, result_data)
            if not getattr(tool, "composite_spec", None):
                self._record_dependency_observation(ctx, tool_name, args, result_data)
            return result

        # Step 5: after_hooks + format_result
        duration = (time.time() - start) * 1000

        # 截断
        formatted, truncated, persist_path, spill = _maybe_truncate(
            tool_name, result_data, ctx.workspace_path,
            storage=ctx.storage,
            process_id=str(getattr(ctx.process, "process_id", "")),
            task_id=str(getattr(ctx.process, "active_task_id", "")),
        )

        ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                "ToolResultReady", execution_id, tool_name, call_index,
                data={
                    "truncated": truncated, "duration_ms": duration,
                    "formatted": formatted,
                    "allowed": True, "is_error": False, "error": "",
                    "tool_result": spill,
                },
            )
        )

        receipt = self._receipt(tool, tool_name, execution_id, call_index, True)
        receipt["task_id"] = str(
            getattr(ctx.process, "active_task_id", "")
            or getattr(ctx.process, "process_id", "")
        )
        self._attach_result_facts(receipt, tool_name, result_data, args)
        if spill:
            receipt["tool_result_locator"] = spill["locator"]
            receipt["tool_result_digest"] = spill["digest"]
            receipt["tool_result_chars"] = spill["total_chars"]
        self._write_invocation_state(
            ctx, tool, tool_name, execution_id, call_index,
            state="committed", effect_state="committed",
            path=invocation_path,
        )
        if invocation_path:
            receipt["invocation_path"] = invocation_path
            receipt["effect_state"] = "committed"
        self._attach_child_lineage(receipt, result_data)
        if not getattr(tool, "composite_spec", None):
            self._record_dependency_observation(ctx, tool_name, args, result_data)
        return ToolResult(
            id=f"{execution_id}_{call_index}",
            tool_name=tool_name,
            execution_id=execution_id,
            call_index=call_index,
            allowed=True,
            data=result_data,
            formatted=formatted,
            duration_ms=duration,
            truncated=truncated,
            persist_path=persist_path,
            spill=spill,
            artifacts={"tool_result": spill} if spill else {},
            receipt=receipt,
        )

    @staticmethod
    def _publish_tool_notices(ctx, tool_name, execution_id, call_index, result_data):
        """Warnings are successful-result facts, never authority grants."""
        if not isinstance(result_data, dict) or not isinstance(result_data.get("warnings"), list):
            return
        from .event_bus import ToolEvent
        for warning in result_data["warnings"][:16]:
            if not isinstance(warning, dict) or not warning.get("message"):
                continue
            notice = {"code": str(warning.get("code") or "TOOL_WARNING")[:80],
                      "message": str(warning["message"])[:1200], "tool_name": tool_name,
                      "execution_id": execution_id, "call_index": call_index,
                      "task_id": str(getattr(ctx.process, "active_task_id", "") or getattr(ctx.process, "process_id", ""))}
            if getattr(ctx.session, "host_ledger", None) is not None:
                ctx.session.host_ledger.append({"event": "tool_notice", **notice})
            ctx.event_bus.emit(ToolEvent("ToolNotice", execution_id, tool_name, call_index, data=notice))

    @staticmethod
    def _attach_result_facts(
        receipt: dict, tool_name: str, result_data: dict | None, arguments: dict | None = None,
    ) -> None:
        """Project narrow Host-observed facts into a durable receipt.

        Completion gates must not recover these facts from model prose or from
        mutable historical files.  Keep this projection deliberately small:
        arbitrary tool output remains in Trace/CAS, while receipts carry only
        the identifiers needed for deterministic completion checks.
        """
        if not isinstance(result_data, dict):
            return
        if tool_name in {"search_text", "list_files"}:
            receipt.update(search_engine=result_data.get("engine"), search_complete=result_data.get("complete"),
                           search_partial=result_data.get("partial"),
                           search_warning_codes=[str(w.get("code")) for w in result_data.get("warnings", []) if isinstance(w, dict)])
        if receipt.get("effect") == "workspace_write":
            receipt["files"] = sorted(path.replace("\\", "/") for path in ToolPipeline._result_files(result_data))
        if tool_name == "exec_command":
            from .engineering_workflow import command_digest
            receipt["command_digest"] = command_digest(arguments or {})
            receipt["command_exit_code"] = result_data.get("exit_code")
            receipt["command_completed"] = type(result_data.get("exit_code")) is int and not result_data.get("error")
        if tool_name == "run_test":
            receipt["test_id"] = str(result_data.get("test_id") or "")
            receipt["test_target"] = str(result_data.get("target") or "")
            receipt["test_passed"] = result_data.get("passed") is True
            codes = [item.get("exit_code") for item in (result_data.get("seed_results") or []) if isinstance(item, dict)]
            receipt["test_completed"] = (type(result_data.get("passed")) is bool and bool(codes)
                                         and all(type(code) is int and code in (0, 1) for code in codes)
                                         and result_data["passed"] == all(code == 0 for code in codes))
            receipt["test_seeds"] = [
                int(item.get("seed", 0))
                for item in list(result_data.get("seed_results") or [])
                if isinstance(item, dict)
            ]
        elif tool_name == "define_tool" and result_data.get("defined") is True:
            receipt["dynamic_tool_name"] = str(result_data.get("name") or "")
            receipt["definition_digest"] = str(result_data.get("digest") or "")
            receipt["definition_version"] = int(result_data.get("version") or 1)
            receipt["definition_steps"] = [
                str(item) for item in list(result_data.get("steps") or [])
            ]
        elif result_data.get("definition_digest"):
            receipt["dynamic_tool_name"] = str(tool_name)
            receipt["definition_digest"] = str(
                result_data.get("definition_digest") or ""
            )
            receipt["definition_version"] = int(
                result_data.get("definition_version") or 1
            )

    def error_result(
        self,
        tool_name: str,
        execution_id: str,
        call_index: int,
        error: str,
        *,
        diagnostics: dict | None = None,
    ) -> ToolResult:
        """Create an error result through the canonical receipt/wire path.

        Pre-dispatch failures are still provider-visible tool results. Keeping
        them on this path prevents silent/empty outputs that a model can mistake
        for success.
        """
        return self._error_result(
            tool_name, execution_id, call_index, time.time(), error,
            diagnostics=diagnostics,
        )

    def _execute_composite_plan(
        self, tool, args: dict, ctx, execution_id: str, call_index: int,
    ) -> dict:
        """Expand a declarative tool through this same canonical pipeline."""
        from backend.core.tools.dynamic_tools import resolve_step_arguments

        spec = dict(getattr(tool, "composite_spec", None) or {})
        catalog = ctx.artifacts.get("tool_catalog") or {}
        inputs = {
            key: value for key, value in args.items()
            if not str(key).startswith("_")
        }
        step_data: dict[str, dict] = {}
        step_receipts: list[dict] = []
        files: set[str] = set()
        child_execution_id = f"{execution_id}__composite_{call_index}"

        for step_index, step in enumerate(spec.get("steps", [])):
            step_id = str(step.get("id", f"step_{step_index + 1}"))
            base_name = str(step.get("tool", ""))
            base_tool = catalog.get(base_name)
            if base_tool is None or base_tool is tool:
                return {
                    "error": "DYNAMIC_COMPONENT_UNAVAILABLE",
                    "failed_step": step_id,
                    "tool": base_name,
                    "steps": step_data,
                    "_child_receipts": step_receipts,
                }
            try:
                step_args = resolve_step_arguments(
                    step.get("arguments", {}), inputs=inputs, steps=step_data,
                )
            except ValueError as exc:
                return {
                    "error": "DYNAMIC_ARGUMENT_RESOLUTION_FAILED",
                    "failed_step": step_id,
                    "detail": str(exc),
                    "steps": step_data,
                    "_child_receipts": step_receipts,
                }
            child = self.execute(
                {"name": base_name, "args": step_args},
                base_tool,
                ctx,
                child_execution_id,
                step_index,
            )
            child_receipt = dict(child.receipt or {})
            child_receipt["composite_tool"] = tool.name
            child_receipt["composite_step"] = step_id
            step_receipts.append(child_receipt)
            data = dict(child.data or {})
            step_data[step_id] = data
            files.update(self._result_files(data))
            ctx.event_bus.emit(
                __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                    "CompositeStepCompleted", child_execution_id, base_name, step_index,
                    data={
                        "composite_tool": tool.name,
                        "step_id": step_id,
                        "is_error": child.is_error,
                        "error": child.error,
                        "duration_ms": child.duration_ms,
                        "receipt": child_receipt,
                    },
                )
            )
            if child.is_error:
                committed_effect = any(
                    is_effectful_mutation(str(receipt.get("effect", "read")))
                    and bool(receipt.get("committed"))
                    for receipt in step_receipts
                )
                return {
                    "error": "DYNAMIC_STEP_FAILED",
                    "failed_step": step_id,
                    "step_error": child.error,
                    "step_diagnostics": dict(child.diagnostics or {}),
                    "steps": step_data,
                    "_child_receipts": step_receipts,
                    "files": sorted(files),
                    "_rollback_required": committed_effect,
                }
        return {
            "completed": True,
            "definition_digest": spec.get("digest", ""),
            "definition_version": spec.get("version", 1),
            "steps": step_data,
            "_child_receipts": step_receipts,
            "files": sorted(files),
        }

    @staticmethod
    def _attach_child_lineage(receipt: dict, result_data: dict | None) -> None:
        if not isinstance(result_data, dict):
            return
        children = result_data.pop("_child_receipts", [])
        if not isinstance(children, list) or not children:
            return
        child_ids = [
            str(item.get("receipt_id")) for item in children
            if isinstance(item, dict) and item.get("receipt_id")
        ]
        result_data["step_receipt_ids"] = child_ids
        receipt["child_receipt_ids"] = child_ids
        receipt["child_receipts"] = [
            dict(item) for item in children if isinstance(item, dict)
        ]

    @staticmethod
    def _result_files(result: dict) -> set[str]:
        files: set[str] = set()
        for key in ("path", "file"):
            if isinstance(result.get(key), str):
                files.add(result[key])
        if isinstance(result.get("files"), list):
            for item in result["files"]:
                if isinstance(item, str):
                    files.add(item)
                elif isinstance(item, dict) and isinstance(item.get("path"), str):
                    files.add(item["path"])
        return files

    def _execute_isolated(self, tool_name: str, args: dict,
                          timeout: float = 60.0,
                          cancellation_event=None) -> dict:
        """通过 ProcessToolRunner 在子进程中执行工具。

        子进程崩溃→异常传播到 Step 4 的 catch 块→被 classify_tool_error 捕获。
        子进程超时→SubprocessResult.timed_out=True→抛出 TimeoutError。
        """
        from backend.core.loop.process_tool_runner import ProcessToolRunner
        runner = ProcessToolRunner(timeout=timeout)
        result = runner.run(
            tool_name, args, cancellation_event=cancellation_event,
        )
        if not result.success:
            if "cancelled" in (result.error or "").lower():
                raise ToolExecutionCancelled(result.error,
                    effect_state=getattr(result, "effect_state", ""))
            if result.timed_out:
                raise TimeoutError(
                    f"Tool '{tool_name}' timed out after {timeout}s"
                )
            raise RuntimeError(result.error or f"Tool '{tool_name}' failed")
        return result.data or {}

    @staticmethod
    def _record_dependency_observation(ctx, tool_name: str, args: dict,
                                       result_data: dict | None) -> None:
        """Turn ordinary tool use into weak, reviewable dependency evidence."""
        if not ctx.workspace_path:
            return
        files: set[str] = set()
        for key in ("path", "file", "source", "target", "cwd"):
            value = args.get(key)
            if isinstance(value, str) and value:
                files.add(value)
        for key in ("paths", "files", "changed_files"):
            value = args.get(key)
            if isinstance(value, list):
                files.update(item for item in value if isinstance(item, str))
        data = result_data or {}
        for key in ("path", "file"):
            value = data.get(key)
            if isinstance(value, str):
                files.add(value)
        value = data.get("files")
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    files.add(item)
                elif isinstance(item, dict) and isinstance(item.get("path"), str):
                    files.add(item["path"])
        matches = data.get("matches")
        if isinstance(matches, list):
            for item in matches:
                if isinstance(item, dict) and isinstance(item.get("file"), str):
                    files.add(item["file"])
        if not files:
            return
        process = ctx.process
        observed = getattr(process, "_dependency_observed_files", set())
        observed.update(files)
        process._dependency_observed_files = observed
        try:
            from backend.core.dependency_graph import record_tool_observation
            record_tool_observation(
                __import__("pathlib").Path(ctx.workspace_path),
                task_id=process.active_task_id or process.process_id,
                tool_name=tool_name,
                files=observed,
                outcome=(
                    "failure" if data.get("success") is False
                    or data.get("passed") is False else "observed"
                ),
            )
        except Exception:
            pass

    @staticmethod
    def _write_invocation_state(
        ctx,
        tool,
        tool_name: str,
        execution_id: str,
        call_index: int,
        *,
        state: str,
        effect_state: str,
        path: str | None = None,
    ) -> str | None:
        """Persist the truth boundary for isolated side effects atomically."""
        if not ctx.workspace_path:
            return None
        if is_observation_effect(getattr(tool.effect, "value", str(tool.effect))):
            return path
        try:
            from pathlib import Path
            from backend.core.storage.invocation_journal import invocation_journal_root
            root = invocation_journal_root(ctx.workspace_path,
                paths=getattr(getattr(ctx, "storage", None), "paths", None))
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = (Path(path) if path else root / f"{execution_id}_{call_index}.json").resolve(strict=False)
            if target.parent != root or target.suffix != ".json":
                raise OSError("Invocation evidence path escapes the bound Host journal")
            previous = {}
            if target.exists():
                try:
                    previous = json.loads(target.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    previous = {}
            now = time.time()
            payload = {
                "execution_id": execution_id,
                "call_index": call_index,
                "process_id": str(getattr(ctx.process, "process_id", "")),
                "task_id": str(
                    getattr(ctx.process, "active_task_id", "")
                    or getattr(ctx.process, "process_id", "")
                ),
                "tool_name": tool_name,
                "state": state,
                "effect_state": effect_state,
                "effect": getattr(tool.effect, "value", str(tool.effect)),
                "cancellation": getattr(tool.cancellation, "value", str(tool.cancellation)),
                "started_at": float(previous.get("started_at", now) or now),
                "updated_at": now,
            }
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(target))
            return str(target)
        except (OSError, RuntimeError, ValueError):
            return path

    def _error_result(self, tool_name, execution_id, call_index, start, error,
                       diagnostics: dict | None = None):
        duration = (time.time() - start) * 1000
        diagnostics = diagnostics or {}
        error_info = diagnostics.get("error_info")
        if isinstance(error_info, dict):
            formatted = json.dumps({
                "error": str(diagnostics.get("code") or error),
                "message": str(error_info.get("message") or error),
                "error_info": error_info,
            }, ensure_ascii=False, indent=2)
        else:
            formatted = f"[工具 {tool_name} 错误: {error}]"
        return ToolResult(
            id=f"{execution_id}_{call_index}",
            tool_name=tool_name,
            execution_id=execution_id,
            call_index=call_index,
            allowed=False,
            is_error=True,
            error=error,
            formatted=formatted,
            duration_ms=duration,
            diagnostics=diagnostics,
            receipt={
                "receipt_id": str(uuid.uuid4()),
                "execution_id": execution_id,
                "call_index": call_index,
                "tool_name": tool_name,
                "succeeded": False,
                "error_code": diagnostics.get("code", "TOOL_ERROR"),
            },
        )

    @staticmethod
    def _receipt(tool, tool_name, execution_id, call_index, succeeded: bool) -> dict:
        effect = getattr(getattr(tool, "effect", "read"), "value", None) \
            or str(getattr(tool, "effect", "read"))
        cancellation = getattr(getattr(tool, "cancellation", "cooperative"), "value", None) \
            or str(getattr(tool, "cancellation", "cooperative"))
        return {
            "receipt_id": str(uuid.uuid4()),
            "execution_id": execution_id,
            "call_index": call_index,
            "tool_name": tool_name,
            "succeeded": succeeded,
            "effect": effect,
            "cancellation": cancellation,
            "resources": list(getattr(tool, "resources", None) or []),
            "committed": bool(succeeded),
            "completed_at": time.time(),
        }


def _validate_args(args: dict, schema: dict) -> None:
    """用 JSON Schema 做运行时类型验证。

    只验证 required 字段存在 + 类型匹配。不做 full JSON Schema 验证（保持轻量）。
    """
    required = schema.get("required", [])
    properties = schema.get("properties", {})

    for field_name in required:
        if field_name not in args:
            raise ValueError(
                f"缺少必需参数 '{field_name}'。"
                f"Schema 要求: {required}"
            )

    for field_name, value in args.items():
        if field_name in properties:
            expected = properties[field_name].get("type", "")
            actual = _typeof(value)
            compatible = expected == actual or (
                expected == "number" and actual == "integer"
            )
            if expected and not compatible:
                raise ValueError(
                    f"参数 '{field_name}' 类型错误: 期望 {expected}, "
                    f"收到 {actual} (值: {str(value)[:60]})"
                )


def _typeof(value) -> str:
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _maybe_truncate(tool_name: str, result_data: dict | None,
                    workspace_path: str, *, storage=None,
                    process_id: str = "", task_id: str = "",
                    ) -> tuple[str, bool, str | None, dict]:
    """截断工具结果。超限时持久化完整 JSON，返回预览。"""
    from backend.core.dispatch.truncation import (
        MAX_OUTPUT_CHARS, PREVIEW_CHARS, format_tool_result as trunc_format,
    )
    if result_data is None:
        return f"[工具 {tool_name} 完成，无输出]", False, None, {}

    output = json.dumps(result_data, ensure_ascii=False, indent=2)
    if len(output) <= MAX_OUTPUT_CHARS:
        return f"[工具 {tool_name} 结果]\n{output}", False, None, {}

    if storage is not None:
        spill = storage.put_tool_result(
            output,
            process_id=process_id,
            task_id=task_id,
            tool_name=tool_name,
            media_type="application/json",
        )
        preview = output[:PREVIEW_CHARS]
        notice = {
            "locator": spill["locator"],
            "media_type": spill["media_type"],
            "total_chars": spill["total_chars"],
            "preview_range": {"start": 0, "end": len(preview)},
            "next_offset": len(preview),
            "retrieval_tool": "tool_result_open",
        }
        formatted = (
            f"[工具 {tool_name} 结果]\n{preview}\n\n"
            "[TOOL_RESULT_SPILL]\n"
            + json.dumps(notice, ensure_ascii=False, sort_keys=True)
        )
        return formatted, True, None, spill

    formatted = trunc_format(tool_name, result_data, workspace_path)
    # extract persist_path from formatted
    truncated = True
    persist_path = None
    if "完整内容已保存至" in formatted:
        import re
        m = re.search(r"保存至\s+(\S+\.json)", formatted)
        if m:
            persist_path = m.group(1)
    return formatted, truncated, persist_path, {}
