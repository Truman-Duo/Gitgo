from types import SimpleNamespace
from dataclasses import replace
import pytest

from backend.core.loop.execution_context import ToolBindings
from backend.core.loop.execution_contract import NATIVE_PROCESS, NativeInvocation
from backend.core.loop.agent_tool import ApprovalMode
from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
from backend.core.sandbox import SandboxDenied
from tests.test_execution_contract import tool, context, execute


def test_mechanism_storage_failure_is_not_a_sandbox_failure_and_does_not_spend_grant(tmp_path_factory, monkeypatch):
    ctx = context(tmp_path_factory)
    target = tool(approval=ApprovalMode.ASK, approval_per_invocation=True)
    create_permission_request(ctx.process, {"purpose": "boundary regression", "tool_name": target.name,
        "arguments": {}}, {target.name: target}, str(tmp_path_factory))
    grant_from_decision(ctx.process, ctx.process.pending_decision, "allow_once")
    def unavailable(*args):
        raise OSError("mechanism storage unavailable")
    monkeypatch.setattr("backend.core.mechanism_runtime.application.bound_application",
        lambda process: SimpleNamespace(before_tool=unavailable))
    result = execute(target, ctx)
    assert result.is_error and result.diagnostics["code"] == "TOOL_RUNTIME_ADMISSION_FAILED"
    assert result.diagnostics["subsystem"] == "task_runtime"
    assert result.receipt["effect_state"] == "not_committed"
    assert ctx.process.approval_grants[0]["remaining_uses"] == 1


def test_authorization_failure_has_its_own_boundary(tmp_path_factory, monkeypatch):
    calls = []
    target = replace(tool(), execute=lambda args: calls.append(args))
    def unavailable(*args, **kwargs):
        raise ValueError("authorization record invalid")
    monkeypatch.setattr("backend.core.loop.permission_broker.admit_invocation_grants", unavailable)
    result = execute(target, context(tmp_path_factory))
    assert result.diagnostics["code"] == "TOOL_AUTHORIZATION_INVALID"
    assert result.diagnostics["subsystem"] == "authorization" and not calls


def test_native_policy_denial_keeps_original_code(tmp_path_factory, monkeypatch):
    def denied(*args, **kwargs):
        raise SandboxDenied("SANDBOX_CAPABILITY_UNSUPPORTED", "test backend lacks capability")
    monkeypatch.setattr(NativeInvocation, "compile", denied)
    result = execute(tool(NATIVE_PROCESS), context(tmp_path_factory))
    assert result.diagnostics["code"] == "SANDBOX_CAPABILITY_UNSUPPORTED"
    assert result.diagnostics["subsystem"] == "native_execution"
    assert result.receipt["effect_state"] == "not_committed"


def test_consumer_refresh_failure_is_a_retained_runtime_result(tmp_path_factory):
    ctx = context(tmp_path_factory)
    calls = []
    def unavailable():
        raise OSError("consumer refresh unavailable")
    ctx.tool_bindings = ToolBindings({}, {}, unavailable)
    result = execute(replace(tool(read_only=False), execute=lambda args: calls.append(args)), ctx)
    assert result.diagnostics["code"] == "TOOL_RUNTIME_ADMISSION_FAILED" and not calls


def test_generic_artifact_bag_cannot_install_a_new_consumer_callback(tmp_path_factory):
    ctx = context(tmp_path_factory)
    calls = []
    ctx.artifacts["synchronize_mechanism_consumers"] = lambda: calls.append("untyped callback")
    result = execute(tool(read_only=False), ctx)
    assert not result.is_error and not calls


def test_removed_native_artifact_during_prepare_preserves_unspent_exact_grant(tmp_path_factory):
    from backend.core.implementation_identity import ImplementationArtifact
    binary = tmp_path_factory / "helper.bin"
    binary.write_bytes(b"implementation")
    target = tool(approval=ApprovalMode.ASK, approval_per_invocation=True,
                  implementation_artifacts=(ImplementationArtifact("helper", binary),))
    ctx = context(tmp_path_factory)
    create_permission_request(ctx.process, {"purpose": "artifact race regression", "tool_name": target.name,
        "arguments": {}}, {target.name: target}, str(tmp_path_factory))
    grant_from_decision(ctx.process, ctx.process.pending_decision, "allow_once")
    def prepare(args):
        binary.unlink()
        return args
    target.prepare_args = prepare
    result = execute(target, ctx)
    assert result.diagnostics["code"] == "TOOL_IMPLEMENTATION_UNAVAILABLE"
    assert ctx.process.approval_grants[0]["remaining_uses"] == 1


def test_typed_composite_catalog_resumes_exact_leaf_without_replaying_prefix(tmp_path_factory):
    from tests.test_composite_continuation import setup, approve
    from backend.core.loop.tool_pipeline import ToolPipeline
    process, ctx, outer, leaf, calls = setup(tmp_path_factory)
    catalog = ctx.artifacts.pop("tool_catalog")
    ctx.tool_bindings = ToolBindings(catalog, catalog)
    call = {"id": "typed-composition", "name": outer.name, "args": {}}
    first = ToolPipeline().execute(call, outer, ctx, "typed-boundary")
    assert first.suspended and calls == ["prefix"]
    approve(process, leaf)
    final = ToolPipeline().execute(call, outer, ctx, "typed-boundary")
    assert not final.is_error and not final.suspended
    assert calls == ["prefix", "derived"]
    assert process.approval_grants[0]["remaining_uses"] == 0


def test_composed_before_tool_callback_cannot_substitute_callable_and_spend_old_approval(tmp_path_factory, monkeypatch):
    calls = []
    target = tool(approval=ApprovalMode.ASK, approval_per_invocation=True)
    ctx = context(tmp_path_factory)
    create_permission_request(ctx.process, {"purpose": "composition identity regression", "tool_name": target.name,
        "arguments": {}}, {target.name: target}, str(tmp_path_factory))
    grant_from_decision(ctx.process, ctx.process.pending_decision, "allow_once")
    def replace_callable(*args):
        target.execute = lambda args: calls.append("substituted")
    app = SimpleNamespace(before_tool=replace_callable, task="task",
                          repo=SimpleNamespace(list_instances=lambda *a, **k: []))
    monkeypatch.setattr("backend.core.mechanism_runtime.application.bound_application", lambda process: app)
    result = execute(target, ctx)
    assert result.diagnostics["code"] == "TOOL_EXECUTION_CONTRACT_INVALID"
    assert ctx.process.approval_grants[0]["remaining_uses"] == 1 and not calls
