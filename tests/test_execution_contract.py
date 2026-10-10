"""Host registration/admission tests; mocks here do not prove OS isolation."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace

import pytest

from backend.core.loop.agent_tool import AgentTool, ApprovalMode
from backend.core.loop.execution_contract import (
    ExecutionContract, ExecutionType, HOST_COMPUTE, NATIVE_PROCESS, data_broker,
)
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.permission_broker import (
    create_permission_request, grant_from_decision, grant_is_current, tool_contract_digest,
)
from backend.core.tools import runner
from backend.core.loop.process_tool_runner import ProcessToolRunner


def tool(contract=HOST_COMPUTE, **kwargs):
    return AgentTool("registered", "test", {}, lambda args: {"value": 7},
                     execution_contract=contract, **kwargs)


def context(root):
    process = AgentProcess(process_id="contract", role="worker", ring_level=RingLevel.RING_0,
                           active_task_id="task", worktree_path=str(root), session=AgentSession())
    return ExecutionContext(process=process, session=process.session, workspace_path=str(root))


def execute(target, ctx, name=None):
    return ToolPipeline().execute({"name": name or target.name, "args": {}}, target, ctx, "contract-test")


@pytest.mark.parametrize("contract", [HOST_COMPUTE, data_broker("test.data")])
def test_declared_host_call_uses_its_bound_callable(tmp_path_factory, contract):
    result = execute(tool(contract), context(tmp_path_factory))
    assert not result.is_error and result.data == {"value": 7}


@pytest.mark.parametrize("change", ["missing", "callable", "contract", "name"])
def test_invalid_registration_refused_before_prepare(tmp_path_factory, change):
    calls = []
    target = tool(prepare_args=lambda args: calls.append("prepare") or args)
    name = target.name
    if change == "missing":
        target = tool(None, prepare_args=target.prepare_args)
    elif change == "callable":
        target.execute = lambda args: calls.append("execute")
    elif change == "contract":
        target.execution_contract = data_broker("test.replacement")
    else:
        name = "unregistered_alias"
    result = execute(target, context(tmp_path_factory), name)
    assert result.is_error and result.diagnostics["code"] == "SANDBOX_POLICY_INVALID"
    assert result.receipt["effect_state"] == "not_committed"
    assert not calls


@pytest.mark.parametrize("change", ["execute", "execution_contract"])
def test_prepare_cannot_spend_exact_grant_after_swapping_authority(tmp_path_factory, change):
    ctx = context(tmp_path_factory)
    target = tool(approval=ApprovalMode.ASK, approval_per_invocation=True)
    create_permission_request(ctx.process, {"purpose": "registration test", "tool_name": target.name,
                                           "arguments": {}}, {target.name: target}, str(tmp_path_factory))
    assert grant_from_decision(ctx.process, ctx.process.pending_decision, "allow_once")
    calls = []
    def prepare(args):
        setattr(target, change, (lambda args: calls.append("execute")) if change == "execute"
                else data_broker("test.changed"))
        return args
    target.prepare_args = prepare
    result = execute(target, ctx)
    assert result.diagnostics["code"] == "SANDBOX_POLICY_INVALID"
    assert result.receipt["effect_state"] == "not_committed"
    assert ctx.process.approval_grants[0]["remaining_uses"] == 1
    assert not calls


def test_before_hook_cannot_change_host_callable(tmp_path_factory):
    ctx = context(tmp_path_factory)
    target = tool()
    calls = []
    ctx.event_bus.subscribe("ToolBeforeHooksStarted",
                            lambda event: setattr(target, "execute", lambda args: calls.append("escaped")))
    result = execute(target, ctx)
    assert result.diagnostics["code"] == "SANDBOX_POLICY_INVALID"
    assert result.receipt["effect_state"] == "not_committed" and not calls


@pytest.mark.parametrize("spec", [
    {"execution_mode": "authored_pure_python"},
    {"execution_mode": "authored_privileged_python"},
    {"execution_mode": "host_pipeline"},
    {"steps": []},
])
def test_composite_cannot_claim_compute_authority(tmp_path_factory, spec):
    result = execute(tool(composite_spec=spec), context(tmp_path_factory))
    assert result.diagnostics["code"] == "SANDBOX_POLICY_INVALID"


def test_native_direct_call_and_isolated_flag_cannot_bypass_pipeline(tmp_path_factory, monkeypatch):
    target = tool(NATIVE_PROCESS)
    with pytest.raises(RuntimeError, match="requires ToolPipeline"):
        target({})
    target.isolated = False
    calls = []
    def native_run(self, name, args, **kwargs):
        calls.append(name)
        return SimpleNamespace(success=True, data={"native": True})
    monkeypatch.setattr(ProcessToolRunner, "run", native_run)
    result = execute(target, context(tmp_path_factory))
    assert not result.is_error and result.data == {"native": True}
    assert calls == [target.runner_name]


@pytest.mark.parametrize("kind,broker,version", [
    ("native_process", "", 1), (ExecutionType.DATA_BROKER, "", 1),
    (ExecutionType.DATA_BROKER, None, 1), (ExecutionType.HOST_COMPUTE, "unexpected", 1),
    (ExecutionType.NATIVE_PROCESS, "", True), (ExecutionType.NATIVE_PROCESS, "", 2),
])
def test_invalid_contract_shapes_rejected(kind, broker, version):
    with pytest.raises(ValueError):
        ExecutionContract(kind, broker, version)


def test_tool_json_cannot_supply_execution_contract():
    with pytest.raises(ValueError, match="Host contract object"):
        tool({"kind": "host_compute"})


def test_unimplemented_named_service_refused(tmp_path_factory):
    result = execute(tool(ExecutionContract(ExecutionType.EXTERNAL_SERVICE, "test.service")),
                     context(tmp_path_factory))
    assert result.diagnostics["code"] == "SANDBOX_POLICY_INVALID"


@pytest.mark.parametrize("contract", [None, HOST_COMPUTE, data_broker("test.data")])
def test_child_registry_cannot_admit_host_authority(monkeypatch, contract):
    monkeypatch.setattr(runner, "_TOOL_REGISTRY", {})
    with pytest.raises(ValueError):
        runner.register("handler", lambda args: {}, contract)
    assert runner._TOOL_REGISTRY == {}


def test_duplicate_handler_refused(monkeypatch):
    monkeypatch.setattr(runner, "_TOOL_REGISTRY", {})
    runner.register("handler", lambda args: {}, NATIVE_PROCESS)
    with pytest.raises(ValueError, match="Duplicate"):
        runner.register("handler", lambda args: {}, NATIVE_PROCESS)


def test_registry_failure_never_runs_partial_registration(monkeypatch):
    calls = []
    def broken(register):
        register("first", lambda args: calls.append("escaped"), NATIVE_PROCESS)
        raise RuntimeError("registration failed")
    monkeypatch.setattr("backend.core.tools.registrations.register_all", broken)
    monkeypatch.setattr(runner.sys, "stdin", StringIO('{"tool_name":"first","args":{}}'))
    output = StringIO()
    monkeypatch.setattr(runner.sys, "stdout", output)
    runner.main()
    assert "trusted handler registration failed" in output.getvalue() and not calls


def test_environment_cannot_select_registry_module(monkeypatch):
    monkeypatch.setenv("GITGO_TOOL_REGISTRY_MODULE", "nonexistent_test_module")
    assert "exec_command" in runner.handler_bindings()


def test_unknown_handler_never_spawns(tmp_path_factory, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unknown handler attempted to spawn")
    monkeypatch.setattr("backend.core.sandbox.sandbox_popen", forbidden)
    monkeypatch.setattr("backend.core.loop.process_tool_runner.subprocess.Popen", forbidden)
    result = ProcessToolRunner().run("unregistered_handler", {"_workspace": str(tmp_path_factory)})
    assert result.data["error"] == "SANDBOX_POLICY_INVALID"
    assert result.data["effect_state"] == "not_committed"


def test_new_handler_requires_native_launch_without_name_allowlist(tmp_path_factory, monkeypatch):
    calls = []
    monkeypatch.setattr("backend.core.tools.registrations.register_all",
                        lambda register: register("future_handler", lambda args: {}, NATIVE_PROCESS))
    from backend.core.sandbox import SandboxDenied
    def blocked(argv, policy, **kwargs):
        calls.append(policy.workspace)
        raise SandboxDenied("SANDBOX_UNAVAILABLE", "test native launch rejection")
    monkeypatch.setattr("backend.core.sandbox.sandbox_popen", blocked)
    result = ProcessToolRunner().run("future_handler", {"_workspace": str(tmp_path_factory)})
    assert result.data["error"] == "SANDBOX_UNAVAILABLE"
    assert calls == [tmp_path_factory]


def test_legacy_file_aliases_declare_native_execution():
    bindings = runner.handler_bindings()
    for name in ("file_read", "file_write", "file_edit", "file_delete", "read_file", "write_file"):
        assert bindings[name].execution_contract == NATIVE_PROCESS


def test_execution_contract_change_invalidates_bound_grant():
    target = tool(data_broker("test.first"))
    process = SimpleNamespace(process_id="worker")
    grant = {"process_id": "worker", "tool_contract_digest": tool_contract_digest(target),
             "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    assert grant_is_current(grant, process, target)
    for contract in (data_broker("test.second"), NATIVE_PROCESS):
        assert not grant_is_current(grant, process, replace(target, execution_contract=contract))
