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
from pathlib import Path
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backend.core.loop.gate import RingGate
from backend.core.tools.file_operations import FILE_MUTATION_TOOLS
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
    suspended: bool = False
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

    def execute(self, tool_call, tool, ctx, execution_id, call_index=0):
        from .read_approval import AdmissionResources
        from .native_invocation_scope import native_invocation_scope
        with AdmissionResources() as admission, native_invocation_scope(ctx, execution_id, call_index) as native_scope:
            result = self._execute(tool_call, tool, ctx, execution_id, call_index, admission=admission)
            records = native_scope["records"]
            if records:
                result.receipt["native_invocations"] = [item.record["invocation_id"] for item in records]
            if any(item.retirement_pending for item in records):
                self._publish_tool_notices(ctx, result.tool_name, execution_id, call_index,
                    {"warnings": [{"code": "INVOCATION_JOURNAL_UPDATE_FAILED",
                                   "message": "Native terminal facts committed; pending-index housekeeping remains incomplete. Do not repeat the operation."}]})
            if any(item.failed for item in records):
                recovery = ctx.process.recovery or {}
                ctx.process.recovery = {**recovery, "resume_forbidden": True, "requires_manual_verification": True,
                    "reasons": list(recovery.get("reasons") or []) + [{
                        "code": "INVOCATION_EVIDENCE_UNAVAILABLE", "execution_id": execution_id,
                        "call_index": call_index, "effect_state": "ambiguous"}]}
                if result.receipt.get("evidence_state") != "failed":
                    result.receipt.update(evidence_state="failed", committed=False)
                    result.diagnostics["evidence_state"] = "failed"
                    result.formatted += "\n[Native invocation evidence update failed; retain results and inspect recovery before native execution.]"
                self._publish_tool_notices(ctx, result.tool_name, execution_id, call_index,
                    {"warnings": [{"code": "INVOCATION_JOURNAL_UPDATE_FAILED",
                                   "message": "Native invocation evidence was not confirmed. Retained results must not be replayed."}]})
            cleanup = result.receipt.get("runtime_cleanup")
            if cleanup is not None and cleanup.get("complete") is not True:
                admission.retain(ctx.process, execution_id, call_index)
            return result

    def _execute(
        self,
        tool_call: dict,
        tool: "AgentTool",
        ctx: "ExecutionContext",
        execution_id: str,
        call_index: int = 0,
        *, admission,
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
        runtime_metadata: dict = {}
        if isinstance(supplied_args, dict) and any(str(key).startswith("_") for key in supplied_args):
            result = self._error_result(tool_name, execution_id, call_index, start,
                "Private execution fields are Host-owned",
                diagnostics={"nature": "governance", "code": "SANDBOX_POLICY_INVALID",
                             "effect_state": "not_committed", "execution_state": "not_started"})
            result.receipt["effect_state"] = "not_committed"
            return result
        from .composite_runtime import call_key, state_for
        continuation_key = call_key(tool_call, execution_id, call_index)
        resume_state = None
        if (getattr(tool, "composite_spec", None) or {}).get("execution_mode") in {"host_pipeline", "authored_host_python"}:
            try:
                resume_state = state_for(ctx.process, continuation_key, tool, raw_args,
                    ctx.artifacts.get("tool_catalog") or {})
            except ValueError as exc:
                return self._error_result(tool_name, execution_id, call_index, start, str(exc),
                    diagnostics={"nature": "governance", "code": "COMPOSITE_CONTINUATION_STALE"})

        if ctx.is_cancelled() or getattr(ctx.process, "cancel_requested", False):
            return self._error_result(
                tool_name, execution_id, call_index, start,
                "tool execution cancelled before start",
                diagnostics={"nature": "system", "code": "TOOL_CANCELLED",
                             "effect_state": "not_committed", "execution_state": "not_started"},
            )

        # Validate Host registration before prepare hooks, approval consumption,
        # or any mutation. Caller-provided arguments never select authority.
        try:
            if tool_name != tool.name:
                raise ValueError("Tool call does not match its Host registration identity")
            execution_contract = tool.validate_execution_contract()
            from .permission_broker import tool_contract_digest
            registered_digest = tool_contract_digest(tool)
            runtime_metadata["registration"] = registered_digest
            runtime_metadata["owner"] = (ctx.process.process_id, ctx.process.active_task_id or ctx.process.process_id)
            from .read_approval import native_cleanup_pending
            if execution_contract.kind.value == "native_process" and native_cleanup_pending(ctx.process):
                raise ValueError("SANDBOX_CLEANUP_PENDING: prior native resources require inspection")
            if execution_contract.kind.value == "native_process":
                from .native_invocation_scope import check_native_recovery
                check_native_recovery(ctx)
        except (AttributeError, ValueError) as exc:
            result = self._error_result(
                tool_name, execution_id, call_index, start, str(exc),
                diagnostics={"nature": "system", "code": "SANDBOX_POLICY_INVALID",
                             "effect_state": "not_committed", "execution_state": "not_started"},
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
        if (not operation_policy.requires_user
                and (tool_name in FILE_MUTATION_TOOLS or
                     (execution_contract.kind.value == "data_broker" and execution_contract.broker.startswith("files.workspace.")))):
            # A real scoped override can exist even for an ordinarily allowed
            # editor (for example after a lesson diagnostic). Use the existing
            # broker, never a model-supplied acknowledgement or private flag.
            from .permission_broker import matching_grant
            public_raw = dict(raw_args or {})
            explicit_user_grant = matching_grant(
                ctx.process, tool_name, public_raw,
                per_invocation=bool(getattr(tool, "approval_per_invocation", False)), tool=tool,
            )
        if operation_policy.requires_user and resume_state is None:
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
            if grant is None:
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
                        args["_web_search_provider_id"] = str(
                            ctx.process.runtime_preferences.get("effective_provider_id")
                            or ctx.process.provider_id or ""
                        )
                from backend.core.loop.permission_broker import (
                    authorize_external_resources,
                    out_of_scope_resources,
                    remap_project_paths,
                )
                args = remap_project_paths(ctx.process, args, str(ctx.workspace_path),
                    lexical=bool(execution_contract.file_action))
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
                    "source": "llm", "effect_state": "not_committed", "execution_state": "not_started",
                },
            )

        # Validate prepared arguments before checking engineering prerequisites.
        # Invalid model arguments remain correctable business errors. Exact
        # approval is independent of prerequisite evidence and is consumed only
        # after both checks succeed. Wrappers defer this guard to their leaves.
        if is_effectful_mutation(tool.effect) and getattr(ctx.process, 'read_context_snapshot', None):
            from .alignment_evidence import alignment_input_digest
            from .alignment_routing import uses_on_demand_review
            context, _ = ctx.process.read_context_snapshot()
            state = context.get('alignment_runtime') or {}
            ready = state.get('admission_review') or {}
            expected_contract = None
            if uses_on_demand_review(state) and tool.alignment_contract is not None:
                try:
                    expected_contract = tool.alignment_contract(dict(args or {}))
                except (ValueError, TypeError, KeyError) as exc:
                    return self._error_result(tool_name, execution_id, call_index, start, str(exc),
                        diagnostics={'nature': 'business', 'code': 'INVALID_TOOL_ARGUMENTS'})
            if (uses_on_demand_review(state)
                    and state.get('task_id') == (ctx.process.active_task_id or ctx.process.process_id)
                    and state.get('process_id') == ctx.process.process_id
                    and (ready.get('status') != 'ready' or ready.get('input_digest') != alignment_input_digest(
                        ctx.process, contract_override=expected_contract))):
                return self._error_result(tool_name, execution_id, call_index, start,
                    'Current requirement evidence must be reviewed before this effect; no effect started and no grant consumed.',
                    diagnostics={'nature': 'governance', 'code': 'ALIGNMENT_REVIEW_REQUIRED', 'effect_state': 'not_committed', 'execution_state': 'not_started'})
        spec = getattr(tool, "composite_spec", None) or {}
        if getattr(ctx.process, "read_context_snapshot", None) and (not spec or spec.get("execution_mode") in {"authored_pure_python", "authored_privileged_python"}):
            from backend.core.loop.engineering_workflow import EngineeringWorkflow
            workflow = ctx.artifacts.get("engineering_workflow") or EngineeringWorkflow(ctx.process)
            try:
                with ctx.process._context_lock:
                    guard = workflow.guard(tool_name, getattr(tool.effect, "value", tool.effect), dict(args or {}))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                workflow.notice("WORKFLOW_RECOVERY_REQUIRED", f"工程前置检查不可用：{exc}；操作尚未开始。")
                guard = {"allowed": False, "code": "ENGINEERING_WORKFLOW_RECOVERY_REQUIRED",
                         "reason": "engineering prerequisite check unavailable",
                         "next_actions": [{"action": "engineering_workflow", "operation": "status"}]}
            if not guard.get("allowed", True):
                return self._error_result(
                    tool_name, execution_id, call_index, start, guard["reason"],
                    diagnostics={"nature": "governance", "code": guard.get("code") or "ENGINEERING_PREREQUISITE_REQUIRED",
                                 "effect_state": "not_committed", "execution_state": "not_started",
                                 "next_actions": list(guard.get("next_actions") or [])},
                )
        # Preparation/prerequisite callbacks must not replace execution authority
        # before an exact grant is spent.
        try:
            tool.validate_execution_contract()
            if runtime_metadata["owner"] != (ctx.process.process_id, ctx.process.active_task_id or ctx.process.process_id):
                raise ValueError("Task scope changed during invocation")
            if tool_contract_digest(tool) != registered_digest:
                raise ValueError("Tool identity or limits changed during invocation")
        except (AttributeError, ValueError) as exc:
            result = self._error_result(
                tool_name, execution_id, call_index, start, str(exc),
                diagnostics={"nature": "system", "code": "SANDBOX_POLICY_INVALID",
                             "effect_state": "not_committed", "execution_state": "not_started"},
            )
            result.receipt["effect_state"] = "not_committed"
            return result
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
                diagnostics={"nature": "governance", "code": gate_result.error or "TOOL_BLOCKED",
                             "effect_state": "not_committed", "execution_state": "not_started"},
            )

        # Governance/before-hook callbacks must not replace execution authority
        # before execution or its journal starts.
        try:
            tool.validate_execution_contract()
            if runtime_metadata["owner"] != (ctx.process.process_id, ctx.process.active_task_id or ctx.process.process_id):
                raise ValueError("Task scope changed during invocation")
            if tool_contract_digest(tool) != registered_digest:
                raise ValueError("Tool identity or limits changed during invocation")
        except (AttributeError, ValueError) as exc:
            result = self._error_result(
                tool_name, execution_id, call_index, start, str(exc),
                diagnostics={"nature": "system", "code": "SANDBOX_POLICY_INVALID",
                             "effect_state": "not_committed", "execution_state": "not_started"},
            )
            result.receipt["effect_state"] = "not_committed"
            return result

        # Preflight only previews authority. Spend the complete grant selection
        # once, after argument/prerequisite/governance/ring checks have passed.
        if ctx.is_cancelled() or getattr(ctx.process, "cancel_requested", False):
            return self._error_result(
                tool_name, execution_id, call_index, start, "tool execution cancelled before admission",
                diagnostics={"nature": "system", "code": "TOOL_CANCELLED"},
            )
        if (tool_name == "read_file" or tool_name in FILE_MUTATION_TOOLS or execution_contract.file_action == "produce_workspace"
                or (execution_contract.kind.value == "data_broker" and execution_contract.broker.startswith("files.workspace."))):
            args["_workspace"] = str(ctx.workspace_path)
            # Business services receive trusted project facts; model private
            # fields were stripped before preparation.
            from backend.core.storage import get_storage
            storage = ctx.storage or get_storage(ctx.workspace_path)
            snapshot, _ = ctx.process.read_context_snapshot()
            args["_lesson_context"] = {
                "project_name": str(snapshot.get("project_name") or ""),
                "state_home": str(storage.paths.state_home),
                "explicit_user_grant": explicit_user_grant is not None and execution_contract.file_action != "produce_workspace",
                "task_id": str(ctx.process.active_task_id or ctx.process.process_id),
                "process_id": ctx.process.process_id,"session_id":ctx.session.session_id,
                "feedback_in_provider_input": __import__("backend.core.knowledge.feedback",fromlist=["exposed_ids"]).exposed_ids(ctx.process),
            }
            args["_mutation_lock_root"] = str(storage.paths.state_home / "mutation-locks")
            if tool_name == "read_file":
                from backend.core.knowledge.prewrite import prepare_read_inventory
                try:
                    args["_lesson_read_inventory"] = prepare_read_inventory(Path(ctx.workspace_path), args)
                except Exception as exc:
                    args["_lesson_read_inventory"] = {"state": "failed", "error_type": type(exc).__name__}
                # A native read receives data, not access to canonical stores/locks.
                args.pop("_lesson_context", None)
                args.pop("_mutation_lock_root", None)

        from backend.core.loop.permission_broker import admit_invocation_grants
        from .execution_contract import NativeInvocation, native_timeout
        native_invocation = None
        try:
            if execution_contract.kind.value == "native_process" and not tool.composite_spec and execution_contract.file_action != "produce_workspace":
                native_invocation = NativeInvocation.compile(tool.runner_name or tool_name,
                    {**args, "_workspace": str(ctx.workspace_path)}, native_timeout(tool, args),
                    owner=runtime_metadata["owner"], registration=registered_digest)
            admitted_roots, admission_error = admit_invocation_grants(
                ctx.process, tool, dict(raw_args or {}), dict(args or {}),
                out_of_scope_resources(args, str(ctx.workspace_path)),
                require_explicit=((operation_policy.requires_user and resume_state is None)
                                  or explicit_user_grant is not None),
                admission=admission,
            )
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            result = self._error_result(
                tool_name, execution_id, call_index, start, f"grant admission failed: {exc}",
                diagnostics={"nature": "system", "code": "SANDBOX_POLICY_INVALID",
                             "effect_state": "not_committed", "execution_state": "not_started"},
            )
            result.receipt["effect_state"] = "not_committed"
            return result
        if admission_error is not None:
            result = self._error_result(
                tool_name, execution_id, call_index, start, admission_error["message"],
                diagnostics={"nature": "governance", "code": admission_error["name"],
                             "source": "host", "error_info": admission_error},
            )
            result.receipt["effect_state"] = "not_committed"
            return result
        args["_allowed_roots"] = admitted_roots

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
                state="running", effect_state="not_committed", evidence=runtime_metadata,
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
        runner_diagnostics = {}
        untrusted_program_result = False
        try:
            # prepare hooks and mutable registry objects cannot substitute a new
            # callable/authority after the initial admission check.
            tool.validate_execution_contract()
            if runtime_metadata["owner"] != (ctx.process.process_id, ctx.process.active_task_id or ctx.process.process_id):
                raise ValueError("Task scope changed during invocation")
            if tool_contract_digest(tool) != registered_digest:
                raise ValueError("Tool identity or limits changed during invocation")
            effective_timeout = native_timeout(tool, args)
            budget = getattr(ctx.process, "task_budget", None)
            if budget is not None:
                allowance = budget.reserve_time(effective_timeout + 5, reason=f"tool_phase:{tool_name}")
                effective_timeout = min(effective_timeout, allowance)
            if getattr(tool, "composite_spec", None):
                dynamic_spec = dict(tool.composite_spec or {})
                if dynamic_spec.get("execution_mode") in {
                    "authored_pure_python", "authored_privileged_python", "authored_host_python",
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
                        if dynamic_spec.get("execution_mode") == "authored_host_python":
                            from .authored_runtime import run
                            result_data = run(self, tool, args, ctx, execution_id, call_index, continuation_key, dynamic_spec,
                                timeout=effective_timeout, diagnostics=runner_diagnostics, runtime_metadata=runtime_metadata)
                        elif dynamic_spec.get("execution_mode") == "authored_privileged_python":
                            from .workspace_candidate_runtime import WorkspaceCandidateRuntime
                            runtime = WorkspaceCandidateRuntime(self, ctx, registered_digest, runtime_metadata["owner"], tool)
                            result_data = runtime.execute("authored_privileged_python", args, effective_timeout,
                                diagnostics=runner_diagnostics, runtime_metadata=runtime_metadata,
                                trusted_arguments={"_dynamic_spec": dynamic_spec}, result_kind="authored")
                        else:
                            # A pure transform needs no formal filesystem ACL.
                            # Its legacy data shape stays unchanged; supervision
                            # facts come from this bound private invocation.
                            from .workspace_candidate_runtime import WorkspaceCandidateRuntime
                            runtime = WorkspaceCandidateRuntime(self, ctx, registered_digest, runtime_metadata["owner"], tool)
                            try:
                                record = runtime.preview("authored_python", args, effective_timeout,
                                    trusted_arguments={"_dynamic_spec": dynamic_spec}, copy_inputs=False, diagnostics=runner_diagnostics)
                                result_data = record["result"]
                                untrusted_program_result = True
                            finally:
                                for key in ("launch_state", "exit_code", "cleanup", "execution_basis"):
                                    if key in runtime.last_render:
                                        runtime_metadata[key] = runtime.last_render[key]
                else:
                    result_data = self._execute_composite_plan(
                        tool, args, ctx, execution_id, call_index, continuation_key, runtime_metadata=runtime_metadata,
                    )
            # Host registration selects native execution independently of business effect.
            elif execution_contract.file_action == "produce_workspace":
                from .workspace_candidate_runtime import WorkspaceCandidateRuntime
                runtime = WorkspaceCandidateRuntime(self, ctx, registered_digest, runtime_metadata["owner"], tool)
                result_data = runtime.execute(tool.runner_name or tool_name, args, effective_timeout,
                    diagnostics=runner_diagnostics, runtime_metadata=runtime_metadata)
            elif execution_contract.kind.value == "native_process":
                result_data = self._execute_isolated(
                    getattr(tool, "runner_name", "") or tool_name,
                    {**args, "_workspace": str(ctx.workspace_path)}, effective_timeout,
                    native_invocation=native_invocation,
                    **({"read_binding": admission.read_binding} if admission.read_binding else {}),
                    cancellation_event=ctx.cancellation,
                    diagnostics=runner_diagnostics,
                    runtime_metadata=runtime_metadata,
                )
            elif execution_contract.kind.value == "data_broker" and execution_contract.broker.startswith("files.workspace."):
                from backend.core.tools.file_mutation import file_service_scope
                from .file_candidate_runtime import FileCandidateRuntime
                runtime = FileCandidateRuntime(self, ctx, registered_digest, runtime_metadata["owner"], tool)
                if tool_name == "document_create":
                    from .document_candidate_runtime import DocumentCandidateRuntime
                    runtime = DocumentCandidateRuntime(self, ctx, registered_digest, runtime_metadata["owner"], tool)
                try:
                    with file_service_scope(lambda: ctx.is_cancelled() or getattr(ctx.process, "cancel_requested", False), runtime):
                        result_data = tool.execute(args)
                finally:
                    if runtime is not None and isinstance(runtime.last_render.get("cleanup"), dict):
                        # Share lifecycle debt with existing admission/recovery.
                        # Producer launch/effects stay nested: it did not write
                        # the formal target, and cache reuse launches nothing.
                        runtime_metadata["cleanup"] = runtime.last_render["cleanup"]
            elif execution_contract.kind.value == "data_broker" and execution_contract.broker == "tests.workspace.run":
                from .registered_test_runtime import RegisteredTestRuntime
                runtime = RegisteredTestRuntime(self, ctx, registered_digest, runtime_metadata["owner"], tool, timeout=effective_timeout)
                try:
                    result_data = runtime.run(args)
                finally:
                    if isinstance(runtime.last_render.get("cleanup"), dict):
                        runtime_metadata["cleanup"] = runtime.last_render["cleanup"]
                    runtime_metadata["native_producers"] = runtime.producers
                    if runtime.phase in {"preflight", "execute"}:
                        runtime_metadata["effect_state"] = "not_committed"
                    elif runtime.phase == "recorded":
                        runtime_metadata["effect_state"] = "committed"
            elif execution_contract.kind.value == "data_broker" and execution_contract.broker == "host.runtime.author_tool":
                from .authoring_runtime import AuthoringRuntime
                from .service_runtime import tool_service_scope
                runtime = AuthoringRuntime(self, ctx, registered_digest, runtime_metadata["owner"], tool, timeout=effective_timeout)
                try:
                    with tool_service_scope(lambda: ctx.is_cancelled() or getattr(ctx.process, "cancel_requested", False), runtime):
                        result_data = tool.execute(args)
                finally:
                    if isinstance(runtime.last_render.get("cleanup"), dict):
                        runtime_metadata["cleanup"] = runtime.last_render["cleanup"]
                    if runtime.phase in {"preflight", "tests"}:
                        runtime_metadata["effect_state"] = "not_committed"
                if runtime.producers and isinstance(result_data, dict):
                    result_data["registration_producers"] = runtime.producers
                    runtime_metadata["native_producers"] = [{"role": "registration_self_test", **item} for item in runtime.producers]
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
                path=invocation_path, evidence=runtime_metadata,
            )
            result = self._error_result(
                tool_name, execution_id, call_index, start, str(exc),
                diagnostics={"nature": "system", "code": "TOOL_CANCELLED",
                             "effect_state": effect_state, "execution_state": "unknown"},
            )
            result.receipt.update({
                "effect": getattr(tool.effect, "value", str(tool.effect)),
                "cancellation": getattr(tool.cancellation, "value", str(tool.cancellation)),
                "effect_state": effect_state,
                "invocation_path": invocation_path,
            })
            return self._attach_runtime_cleanup(self._attach_runner_diagnostics(result, runner_diagnostics), runtime_metadata, ctx)
        except Exception as exc:
            effect_state = runtime_metadata.get("effect_state")
            if effect_state not in {"not_committed", "committed", "partial", "ambiguous"}:
                effect_state = "ambiguous" if is_effectful_mutation(tool.effect) else "not_committed"
            self._write_invocation_state(
                ctx, tool, tool_name, execution_id, call_index,
                state="failed", effect_state=effect_state,
                path=invocation_path, evidence=runtime_metadata,
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
                    "effect_state": effect_state,
                    "execution_state": "unknown",
                },
            )
            result.receipt.update({
                "effect": getattr(tool.effect, "value", str(tool.effect)),
                "cancellation": getattr(tool.cancellation, "value", str(tool.cancellation)),
                "effect_state": effect_state,
                "invocation_path": invocation_path,
            })
            return self._attach_runtime_cleanup(self._attach_runner_diagnostics(result, runner_diagnostics), runtime_metadata, ctx)

        self._publish_tool_notices(ctx, tool_name, execution_id, call_index, result_data)
        if runtime_metadata.get("suspended") is True:
            self._write_invocation_state(ctx, tool, tool_name, execution_id, call_index,
                state="awaiting_user", effect_state="partial", path=invocation_path, evidence=runtime_metadata)
            result = ToolResult(tool_name=tool_name, execution_id=execution_id,
                call_index=call_index, suspended=True, data={"execution_state": "awaiting_user"})
            return self._attach_runtime_cleanup(self._attach_runner_diagnostics(result, runner_diagnostics), runtime_metadata, ctx)

        business_error = ""
        if isinstance(result_data, dict):
            business_error = str(result_data.get("error", ""))
            if not business_error and result_data.get("success") is False:
                business_error = "COMMAND_EXIT_NONZERO"
            if not business_error and result_data.get("passed") is False:
                business_error = "TEST_FAILED"
        if business_error and untrusted_program_result:
            business_error = "CUSTOM_TOOL_EXECUTION_FAILED"
        if ((execution_contract.file_action == "produce_workspace" or
                (execution_contract.kind.value == "data_broker" and (execution_contract.broker.startswith("files.workspace.") or execution_contract.broker in {"governance.formalize", "identity.memory_snapshot"})))
                and isinstance(result_data, dict) and result_data.get("effect_state") in {"not_committed", "committed", "partial", "ambiguous"}):
            runtime_metadata["effect_state"] = result_data["effect_state"]
        if business_error:
            effect_value = getattr(tool.effect, "value", str(tool.effect))
            effect_state = runtime_metadata.get("effect_state") or (
                "not_committed" if not is_effectful_mutation(effect_value)
                else "ambiguous"
            )
            self._write_invocation_state(
                ctx, tool, tool_name, execution_id, call_index,
                state="business_error", effect_state=effect_state,
                path=invocation_path, evidence=runtime_metadata,
            )
            code = business_error
            detail = str(result_data.get("detail", ""))
            if code == "COMMAND_EXIT_NONZERO" and not detail:
                detail = f"exit_code={result_data.get('exit_code')}"
            message = code if not detail else f"{code}: {detail}"
            rollback_required = runtime_metadata.get("rollback_required") is True
            from backend.core.errors import error_payload
            error_info = (error_payload(
                "CUSTOM_TOOL_EXECUTION_FAILED")["error_info"] if untrusted_program_result else result_data.get("error_info"))
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
                    "effect_state": effect_state,
                    "execution_state": "completed" if untrusted_program_result else result_data.get("execution_state", "unknown"),
                    "next_actions": [] if untrusted_program_result else list(result_data.get("next_actions") or []),
                    **({"error_info": error_info} if isinstance(error_info, dict) else {}),
                },
            )
            error_info = result.diagnostics.get("error_info")
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
            self._attach_result_facts(result.receipt, tool_name, result_data, args, execution_contract=execution_contract, bound_definition=tool.composite_spec)
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
            self._attach_child_lineage(result.receipt, result_data, runtime_metadata)
            if not getattr(tool, "composite_spec", None):
                self._record_dependency_observation(ctx, tool_name, args, result_data)
            return self._attach_runtime_cleanup(self._attach_runner_diagnostics(result, runner_diagnostics), runtime_metadata, ctx)

        # Step 5: after_hooks + format_result
        duration = (time.time() - start) * 1000

        # 截断
        formatted, truncated, persist_path, spill = _maybe_truncate(
            tool_name, result_data, ctx.workspace_path,
            storage=ctx.storage,
            process_id=str(getattr(ctx.process, "process_id", "")),
            task_id=str(getattr(ctx.process, "active_task_id", "")),
        )

        receipt = self._receipt(tool, tool_name, execution_id, call_index, True)
        receipt["task_id"] = str(
            getattr(ctx.process, "active_task_id", "")
            or getattr(ctx.process, "process_id", "")
        )
        self._attach_result_facts(receipt, tool_name, result_data, args, execution_contract=execution_contract, bound_definition=tool.composite_spec)
        if spill:
            receipt["tool_result_locator"] = spill["locator"]
            receipt["tool_result_digest"] = spill["digest"]
            receipt["tool_result_chars"] = spill["total_chars"]
        effect_state = self._observed_effect(effect_value, runtime_metadata, succeeded=True)
        self._write_invocation_state(
            ctx, tool, tool_name, execution_id, call_index,
            state="committed" if effect_state == "committed" else "completed", effect_state=effect_state,
            path=invocation_path, evidence=runtime_metadata,
        )
        if invocation_path:
            receipt["invocation_path"] = invocation_path
            receipt["effect_state"] = effect_state
        if is_effectful_mutation(effect_value) and effect_state != "committed":
            receipt["committed"] = False
            receipt["effect_state"] = effect_state
        self._attach_child_lineage(receipt, result_data, runtime_metadata)
        if not getattr(tool, "composite_spec", None):
            self._record_dependency_observation(ctx, tool_name, args, result_data)
        result = ToolResult(
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
        result = self._attach_runtime_cleanup(result, runtime_metadata, ctx)

        result = self._attach_runner_diagnostics(result, runner_diagnostics)
        ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                "ToolResultReady", execution_id, tool_name, call_index,
                data={
                    "truncated": truncated, "duration_ms": duration,
                    "formatted": result.formatted,
                    "diagnostics": dict(result.diagnostics),
                    "allowed": True, "is_error": False, "error": "",
                    "tool_result": spill,
                },
            )
        )

        return result

    @staticmethod
    def _observed_effect(effect, metadata, *, succeeded):
        if not is_effectful_mutation(effect):
            return "not_committed"
        if metadata.get("effect_state") in {"not_committed", "partial", "ambiguous"}:
            return metadata["effect_state"]
        children = metadata.get("child_receipts")
        if children is not None:
            changed = [r for r in children if is_effectful_mutation(r.get("effect", "read"))]
            if not changed:
                return "not_committed"
            if all(r.get("committed") is True and r.get("effect_state") == "committed" for r in changed):
                return "committed"
            return "partial" if any(r.get("committed") for r in changed) else "ambiguous"
        # Exit/protocol success observes the call, not arbitrary child effects.
        if "launch_state" in metadata:
            return "not_committed" if metadata["launch_state"] == "not_started" else "ambiguous"
        return "committed" if succeeded else "ambiguous"

    @classmethod
    def _attach_runtime_cleanup(cls, result, runtime_metadata, ctx):
        """Keep supervisor facts separate from handler data and effect success."""
        result.receipt["process_id"] = ctx.process.process_id
        cls._attach_child_lineage(result.receipt, result.data, runtime_metadata)
        if runtime_metadata.get("native_producers"):
            from copy import deepcopy
            result.receipt["native_producers"] = deepcopy(runtime_metadata["native_producers"])
        result.receipt["business_state"] = "failed" if result.is_error else "awaiting_user" if result.suspended else "succeeded"
        if "launch_state" in runtime_metadata and is_effectful_mutation(result.receipt.get("effect", "read")):
            effect = cls._observed_effect(result.receipt["effect"], runtime_metadata, succeeded=not result.is_error)
            result.receipt["effect_state"] = effect
            if effect != "committed":
                result.receipt["committed"] = False
                result.diagnostics["effect_verification"] = "unverified"
                reported = {}
                for name in ("files", "command_exit_code", "command_completed", "test_passed", "test_completed", "test_seeds"):
                    if name in result.receipt:
                        reported[name] = result.receipt.pop(name)
                if reported:
                    result.receipt["reported_facts"] = reported
        from .native_invocation_scope import current_native_records
        native_records = current_native_records(ctx)
        if native_records:
            result.receipt["native_invocations"] = [item.record["invocation_id"] for item in native_records]
        evidence = "failed" if any(item.failed for item in native_records) else runtime_metadata.get("evidence_state", "not_required")
        result.receipt["evidence_state"] = evidence
        result.diagnostics["evidence_state"] = evidence
        if evidence == "failed":
            # Preserve business output/effects; a stale path is not this commit.
            result.receipt["committed"] = False
            result.formatted += "\n[Evidence update failed; retain this result and inspect recovery before repeating the operation.]"
        if "execution_basis" in runtime_metadata:
            result.receipt["execution_basis"] = runtime_metadata["execution_basis"]
        if "launch_state" in runtime_metadata:
            result.receipt["launch_state"] = runtime_metadata["launch_state"]
            result.receipt["runtime_exit_code"] = runtime_metadata.get("exit_code")
        from .read_approval import runtime_cleanup_report
        report = runtime_cleanup_report(runtime_metadata.get("cleanup"))
        if report is None:
            return result
        result.receipt["runtime_cleanup"] = report
        result.diagnostics["runtime_cleanup"] = report
        if not report["complete"]:
            cls._publish_tool_notices(ctx, result.tool_name, result.execution_id, result.call_index,
                {"warnings": [{"code": "PROCESS_CLEANUP_INCOMPLETE",
                               "message": "Process cleanup was not fully confirmed (" + report["mode"] +
                                   "). Completed effects are retained; inspect remaining processes before repeating the operation."}]})
        return result

    @staticmethod
    def _publish_tool_notices(ctx, tool_name, execution_id, call_index, result_data):
        """Warnings are successful-result facts, never authority grants."""
        if not isinstance(result_data, dict):
            return
        from backend.core.errors import notice_guidance
        from .event_bus import ToolEvent
        warnings = list(result_data.get("warnings") or []) if isinstance(result_data.get("warnings"), list) else []
        for key in ("knowledge_capture_warning","lesson_recall_warning"):
            capture=result_data.get(key)
            if isinstance(capture,dict):warnings.append(capture)
        for warning in warnings[:16]:
            if not isinstance(warning, dict) or not warning.get("message"):
                continue
            warning.update(notice_guidance(str(warning.get("code") or "")))
            notice = {**notice_guidance(str(warning.get("code") or "")), "code": str(warning.get("code") or "TOOL_WARNING")[:80],
                      "message": str(warning["message"])[:1200], "tool_name": tool_name,
                      "execution_id": execution_id, "call_index": call_index,
                      "task_id": str(getattr(ctx.process, "active_task_id", "") or getattr(ctx.process, "process_id", ""))}
            if getattr(ctx.session, "host_ledger", None) is not None:
                ctx.session.host_ledger.append({"event": "tool_notice", **notice})
            ctx.event_bus.emit(ToolEvent("ToolNotice", execution_id, tool_name, call_index, data=notice))

    @staticmethod
    def _attach_result_facts(
        receipt: dict, tool_name: str, result_data: dict | None, arguments: dict | None = None,
        *, execution_contract=None, bound_definition=None,
    ) -> None:
        """Project narrow Host-observed facts into a durable receipt.

        Completion gates must not recover these facts from model prose or from
        mutable historical files.  Keep this projection deliberately small:
        arbitrary tool output remains in Trace/CAS, while receipts carry only
        the identifiers needed for deterministic completion checks.
        """
        if not isinstance(result_data, dict):
            return
        from .execution_contract import ExecutionType
        trusted_files = (getattr(execution_contract, "file_action", "") == "produce_workspace" or
                         (getattr(execution_contract, "kind", None) == ExecutionType.DATA_BROKER
                          and execution_contract.broker.startswith("files.workspace.")))
        if trusted_files:
            receipt["file_service_result"] = True
            # Bounded native evidence survives nested calls and permission
            # checkpoints. Arbitrary program JSON is never projected here.
            if isinstance(result_data.get("knowledge_excerpts"), list):
                from backend.core.knowledge.excerpts import MAX_EXCERPTS, MAX_EXCERPT_CHARS, text_hash
                from copy import deepcopy
                fields = ("excerpt_id", "path", "start_line", "end_line", "text", "sha256",
                          "observed_file_sha256", "unique_at_capture", "origin")
                receipt["knowledge_excerpts"] = [
                    {k: deepcopy(item[k]) for k in fields if k in item}
                    for item in (result_data.get("knowledge_excerpts") or [])[:MAX_EXCERPTS]
                    if isinstance(item, dict) and item.get("origin") == "native_file_diff"
                    and isinstance(item.get("text"), str) and len(item["text"]) <= MAX_EXCERPT_CHARS
                    and item.get("sha256") == text_hash(item["text"])]
        feedback = result_data.get("lesson_feedback") if trusted_files else None
        if isinstance(feedback, dict):
            receipt["candidate_sha256"] = result_data.get("candidate_sha256")
            receipt["before_sha256"] = result_data.get("before_sha256")
            receipt["after_sha256"] = result_data.get("after_sha256")
            receipt["lesson_checks"] = [{key: item.get(key) for key in
                ("lesson_id", "lesson_version", "code", "registered_check")}
                for item in feedback.get("diagnostics", [])]
            receipt["lesson_user_override"] = feedback.get("user_override", False)
            receipt["lesson_feedback_id"] = feedback.get("feedback_id", "")
            receipt["lesson_lookup"] = feedback.get("lookup", {})
            receipt["lesson_advisory_state"] = feedback.get("advisory_state", "unknown")
            receipt["lesson_mandatory_check_state"] = feedback.get("mandatory_check_state", "unknown")
        for key in ("candidate_ref", "candidate_source", "candidate_render", "candidate_view"):
            if trusted_files and key in result_data:
                receipt[key] = result_data[key]
        if trusted_files and isinstance(result_data.get("file_results"), list):
            receipt["file_mutations"] = [{k: row[k] for k in
                ("path", "operation", "before_sha256", "candidate_sha256", "after_sha256",
                 "effect_state", "execution_state", "error", "trash_ref", "recoverable", "recovery_state", "recovery_ref", "recovery_kind") if k in row}
                for row in result_data["file_results"][:128] if isinstance(row, dict)]
        if tool_name in {"search_text", "list_files"} and bound_definition is None:
            receipt.update(search_engine=result_data.get("engine"), search_complete=result_data.get("complete"),
                           search_partial=result_data.get("partial"),
                           search_warning_codes=[str(w.get("code")) for w in result_data.get("warnings", []) if isinstance(w, dict)])
        if getattr(execution_contract, "broker", "") == "governance.formalize" and bound_definition is None:
            for key in ("governance_state_ref", "git_commit_created", "source_indices", "commit"):
                if key in result_data:
                    receipt[key] = result_data[key]
        if receipt.get("effect") == "workspace_write" or trusted_files:
            receipt["files"] = sorted(path.replace("\\", "/") for path in ToolPipeline._result_files(result_data, include_mutations=trusted_files))
        if getattr(execution_contract, "file_action", "") == "produce_workspace":
            for key in ("producer_completed", "producer_exit_code", "producer_observation", "input_applicability", "output_publication"):
                if key in result_data:
                    receipt[key] = result_data[key]
            render = result_data.get("candidate_render") or {}
            for source, target in (("launch_state", "producer_launch_state"), ("exit_code", "producer_runtime_exit_code")):
                if source in render:
                    receipt[target] = render[source]
        if tool_name == "exec_command" and bound_definition is None:
            from .engineering_workflow import command_digest
            receipt["command_digest"] = command_digest(arguments or {})
            receipt["command_exit_code"] = result_data.get("exit_code")
            receipt["command_completed"] = (
                result_data.get("producer_completed") is True and result_data.get("input_applicability") == "current"
                and result_data.get("output_publication") in {"committed", "not_required"}
                if getattr(execution_contract, "file_action", "") == "produce_workspace" else
                type(result_data.get("exit_code")) is int and not result_data.get("error"))
        if tool_name == "read_file" and bound_definition is None and isinstance(result_data.get("input_dependency"), dict):
            receipt["input_dependency"] = dict(result_data["input_dependency"])
        if tool_name == "run_test" and bound_definition is None:
            from .validation_evidence import version_summary
            receipt["validation_version"] = version_summary(result_data.get("validation_version"))
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
        elif tool_name == "define_tool" and bound_definition is None and result_data.get("defined") is True:
            receipt["dynamic_tool_name"] = str(result_data.get("name") or "")
            receipt["definition_digest"] = str(result_data.get("digest") or "")
            receipt["definition_version"] = int(result_data.get("version") or 1)
            receipt["definition_steps"] = [
                str(item) for item in list(result_data.get("steps") or [])
            ]
        elif isinstance(bound_definition, dict) and bound_definition.get("digest"):
            receipt["dynamic_tool_name"] = str(tool_name)
            receipt["definition_digest"] = str(bound_definition["digest"])
            receipt["definition_version"] = int(bound_definition.get("version") or 1)

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

    def _execute_composite_plan(self, tool, args, ctx, execution_id, call_index, key, *, runtime_metadata=None):
        from .composite_runtime import run
        return run(self, tool, args, ctx, execution_id, call_index, key, runtime_metadata=runtime_metadata)

    @staticmethod
    def _attach_child_lineage(receipt: dict, result_data: dict | None, runtime_metadata=None) -> None:
        if not isinstance(result_data, dict):
            return
        # Only the in-process composite scheduler owns this side channel.
        # Tool JSON remains data, even when it copies a real receipt ID.
        from copy import deepcopy
        result_data.pop("_child_receipts", None)
        children = deepcopy((runtime_metadata or {}).get("child_receipts", []))
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
    def _result_files(result: dict, *, include_mutations=False) -> set[str]:
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
        if include_mutations:
            for row in result.get("file_results", []):
                if isinstance(row, dict) and isinstance(row.get("path"), str):
                    files.add(row["path"])
        return files

    def _execute_isolated(self, tool_name: str, args: dict,
                          timeout: float = 60.0,
                          cancellation_event=None, diagnostics=None, runtime_metadata=None, read_binding=None, native_invocation=None) -> dict:
        """通过 ProcessToolRunner 在子进程中执行工具。

        子进程崩溃→异常传播到 Step 4 的 catch 块→被 classify_tool_error 捕获。
        子进程超时→SubprocessResult.timed_out=True→抛出 TimeoutError。
        """
        from backend.core.loop.process_tool_runner import ProcessToolRunner
        from .execution_contract import NativeInvocation
        metadata = runtime_metadata if runtime_metadata is not None else {}
        if native_invocation is None:
            native_invocation = NativeInvocation.compile(tool_name, args, timeout,
                owner=metadata.get("owner", ("", "")), registration=metadata.get("registration", ""))
        native_invocation.check(tool_name, args, timeout)
        metadata["execution_basis"] = native_invocation.digest
        runner = ProcessToolRunner(timeout=timeout)
        result = runner.run(
            tool_name, args, cancellation_event=cancellation_event, native_invocation=native_invocation,
            **({"read_binding": read_binding} if read_binding else {}),
        )
        from backend.core.loop.runner_diagnostics import capture_runner_diagnostics, safe_runner_text
        if diagnostics is not None:
            diagnostics.update(capture_runner_diagnostics(result))
        error = safe_runner_text(getattr(result, "error", "") or "")
        if runtime_metadata is not None:
            runtime_metadata["launch_state"] = "started" if getattr(result, "started", False) is True else getattr(result, "launch_state", "unknown")
            runtime_metadata["exit_code"] = getattr(result, "exit_code", None)
            if isinstance(getattr(result, "cleanup", None), dict):
                runtime_metadata["cleanup"] = dict(result.cleanup)
        if not result.success:
            if "cancelled" in error.lower():
                raise ToolExecutionCancelled(error,
                    effect_state=getattr(result, "effect_state", ""))
            if result.timed_out:
                raise TimeoutError(
                    result.error or f"Tool '{tool_name}' timed out after {timeout}s"
                )
            raise RuntimeError(error or f"Tool '{tool_name}' failed")
        return result.data or {}

    @staticmethod
    def _attach_runner_diagnostics(result: ToolResult, diagnostics: dict) -> ToolResult:
        if diagnostics:
            result.diagnostics["runner"] = dict(diagnostics)
            # The existing model/trace/dashboard result path consumes formatted
            # text and diagnostics. Keep child data intact; these observations
            # are not a trusted effect/lineage/cleanup envelope.
            if result.is_error or diagnostics.get("stderr"):
                result.formatted += "\n[Host-captured runner diagnostics]\n" + json.dumps(
                    diagnostics, ensure_ascii=False, sort_keys=True,
                )
        return result

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
        evidence: dict | None = None,
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
            from .read_approval import runtime_cleanup_report
            cleanup = runtime_cleanup_report((evidence or {}).get("cleanup"))
            if cleanup is not None:
                payload["runtime_cleanup"] = cleanup
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(target))
            if evidence is not None:
                evidence["evidence_state"] = "committed"
            return str(target)
        except (OSError, RuntimeError, ValueError) as exc:
            from .event_bus import ToolEvent
            ctx.event_bus.emit(ToolEvent("ToolNotice", execution_id, tool_name, call_index,
                data={"code": "INVOCATION_JOURNAL_UNAVAILABLE" if not path else "INVOCATION_JOURNAL_UPDATE_FAILED",
                      "message": ("Tool execution was blocked because its invocation journal could not be created."
                                  if not path else "Invocation journal update failed; retained results remain auditable and restart requires inspection."),
                      "severity": "warning", "error_type": type(exc).__name__}))
            if evidence is not None:
                evidence["evidence_state"] = "failed"
            return None

    def _error_result(self, tool_name, execution_id, call_index, start, error,
                       diagnostics: dict | None = None):
        duration = (time.time() - start) * 1000
        diagnostics = dict(diagnostics or {})
        from backend.core.errors import diagnostic_entry
        entry = diagnostic_entry(str(diagnostics.get("code") or ""))
        error_info = diagnostics.get("error_info")
        if entry is not None and entry["kind"] == "error":
            if isinstance(error_info, dict):
                # Producer-supplied occurrences, actions and state remain authoritative.
                error_info = {**error_info, "guidance_ref": entry["guidance_ref"]}
            else:
                error_info = {**entry, "message": str(error),
                    "occurrence_id": f"err_{uuid.uuid4().hex}",
                    "effect_state": diagnostics.get("effect_state", "unknown"),
                    "execution_state": diagnostics.get("execution_state", "unknown"),
                    "next_actions": list(diagnostics.get("next_actions") or [])}
            diagnostics["error_info"] = error_info
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
                **{key: diagnostics[key] for key in ("effect_state", "execution_state") if key in diagnostics},
                **({"error_catalog_id": error_info.get("catalog_id"), "error_occurrence_id": error_info.get("occurrence_id")} if isinstance(error_info, dict) else {}),
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

    验证必需字段、顶层类型及显式禁止的额外公开字段；不是完整 JSON Schema 验证器。
    Host 注入的私有字段在模型入口已被拒绝，不能被当作模型额外参数。
    """
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        import re
        patterns = schema.get("patternProperties", {})
        unknown = [str(key) for key in args if not str(key).startswith("_") and key not in properties
                   and not any(re.search(pattern, str(key)) for pattern in patterns)]
        if unknown:
            raise ValueError("Unsupported public arguments: " + ", ".join(sorted(unknown)))

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

    if tool_name == "run_test":
        from .validation_evidence import version_summary
        result_data = dict(result_data)
        if "validation_version" in result_data:
            result_data["validation_version"] = version_summary(result_data["validation_version"])
        elif isinstance(result_data.get("result"), dict) and "validation_version" in result_data["result"]:
            result_data["result"] = {**result_data["result"], "validation_version": version_summary(result_data["result"]["validation_version"])}
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
