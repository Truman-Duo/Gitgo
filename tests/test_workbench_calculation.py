"""Workbench arithmetic migration without duplicate engines or capability expansion."""
from types import SimpleNamespace

import pytest

from backend.core.loop.agent_tool import ToolEffect
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.protocol_tools.workbench import install
from backend.core.loop.protocol_tools.registry import build_internal_tools
from backend.core.loop.tool_surface import _select_authorized_tools


def surface():
    context = SimpleNamespace(tools={})
    install(context)
    return context.tools


@pytest.mark.parametrize("expression, expected", [
    ("(12 + 3) * 4 == 60", True), ("2 ** 10", 1024),
    ("7 // 2", 3), ("-3 < 0 < 2", True), ("1.5 + 0.5", 2.0)])
def test_current_and_legacy_paths_preserve_bounded_results(expression, expected):
    tools = surface()
    assert tools["workbench"].execute({"operation": "calculate", "expression": expression}) == {
        "operation": "calculate", "expression": expression, "result": expected}
    assert tools["calculate"].execute({"expression": expression}) == {
        "expression": expression, "result": expected}


@pytest.mark.parametrize("expression", ["__import__('os')", "a.b", "f()", "[1,2]", "2 ** 101", "1e101", "1e309"])
def test_neither_adapter_can_execute_code_or_unbounded_expressions(expression):
    for name, args in [("workbench", {"operation": "calculate", "expression": expression}),
                       ("calculate", {"expression": expression})]:
        with pytest.raises(ValueError):
            surface()[name].execute(args)


def test_only_actual_workbench_operations_are_exposed():
    tool = surface()["workbench"]
    assert tool.effect == ToolEffect.READ and not tool.idempotent
    with pytest.raises(ValueError, match="Unsupported workbench operation"):
        tool.execute({"operation": "execute_python", "expression": "1 + 1"})
    assert tool.parameters["properties"]["operation"]["enum"] == ["calculate", "create_sheet", "revise_sheet", "continue_sheet", "open_sheet", "list_sheets", "import_context", "aggregate", "check_sources", "submit_material", "add_data", "import_result"]


def test_current_profiles_and_explicit_old_contract_get_distinct_surfaces(tmp_path_factory):
    process = AgentProcess(process_id="workbench-test", role="worker", ring_level=RingLevel.RING_3,
        active_task_id="task", worktree_path=str(tmp_path_factory), session=AgentSession(),
        capability_profile_id="governance.observe",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("governance.observe")))
    tools = build_internal_tools(process, {}, str(tmp_path_factory), None, None, None)
    current, _ = _select_authorized_tools(process, tools)
    assert "workbench" in current and "calculate" not in current
    assert current["workbench"].execute({"operation": "calculate", "expression": "6 * 7"})["result"] == 42
    process.tool_registry = ToolRegistry(["calculate"])
    legacy, unavailable = _select_authorized_tools(process, tools)
    assert not unavailable and set(legacy) == {"calculate"}
    assert legacy["calculate"].execute({"expression": "6 * 7", "operation": "execute_python"}) == {
        "expression": "6 * 7", "result": 42}
    assert process.tool_registry.list_all() == ["calculate"]


def test_profiles_no_longer_request_legacy_arithmetic_tool():
    for name in CapabilityProfiles.ids():
        assert "calculate" not in CapabilityProfiles.resolve_tools(name)
    assert "workbench" in CapabilityProfiles.pre_contract_tools()


def test_saved_legacy_leaf_requests_explicit_definition_migration_not_impossible_lease():
    from backend.core.loop.tool_surface import component_blocker
    process = SimpleNamespace(actor_kind="supervisor", tool_registry=ToolRegistry(["workbench"]))
    spec = {"name": "old_math_recipe", "execution_mode": "host_pipeline",
            "steps": [{"tool": "calculate", "args": {"expression": "2+2"}}]}
    result = component_blocker(process, spec, surface())
    assert result["missing_tools"] == ["calculate"]
    assert result["next_actions"][0]["action"] == "revise_tool_definition"
    assert result["next_actions"][0]["replacement"]["tool"] == "workbench"
    assert not any(a["action"] == "request_self_execute" for a in result["next_actions"])
    assert spec["steps"][0]["tool"] == "calculate"
    assert process.tool_registry.list_all() == ["workbench"]
