"""Composed boundaries, not model-quality or GUI acceptance evidence."""
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from backend.core.dispatch import ToolDispatcher
from backend.core.loop.agent_tool import AgentTool, ApprovalMode, ToolEffect
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.gate import RingGate
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry
from backend.core.tools.catalog import build_workspace_tools
from tests.test_lesson_feedback import _lesson, _args


def process(workspace, name):
    return AgentProcess("composed", "worker", RingLevel.RING_3,
        tool_registry=ToolRegistry([name]), active_task_id="task",
        worktree_path=str(workspace), session=AgentSession())


def test_compatibility_dispatch_cannot_skip_exact_user_approval(tmp_path_factory):
    calls=[]
    tool=AgentTool("fixture", "No effects", {"type":"object","properties":{}},
        lambda args: calls.append(args) or {"ok":True},
        execution_contract=data_broker("test.integration"), approval=ApprovalMode.ASK,
        approval_per_invocation=True)
    p=process(tmp_path_factory,tool.name)
    result=ToolDispatcher(RingGate(),{tool.name:tool}).dispatch(p,tool.name,{})
    assert result.error and not calls
    assert p.tool_receipts and p.tool_receipts[-1]["succeeded"] is False


def test_compatibility_dispatch_uses_the_same_checked_file_service(tmp_path_factory):
    _lesson(tmp_path_factory)
    tool=build_workspace_tools(tmp_path_factory)["write_file"]
    p=process(tmp_path_factory,tool.name)
    p.context_snapshot={"project_name":"project"}
    dispatcher=ToolDispatcher(RingGate(),{tool.name:tool})
    result=dispatcher.dispatch(p,tool.name,{"path":"cache.py","content":"start_cache()"})
    assert "KNOWN_LESSON_VIOLATION" in result.error
    assert not (tmp_path_factory/"cache.py").exists()
    assert p.tool_receipts[-1]["effect_state"] == "not_committed"


@pytest.mark.parametrize("case", ["invalid_pattern", "stale"])
def test_explicit_override_never_converts_failed_check_into_a_pass(tmp_path_factory,monkeypatch,case):
    from backend.core.tools.workspace_tools import write_file
    from backend.core.knowledge import prewrite
    _lesson(tmp_path_factory,check={"authority":"registered","mode":"violation_pattern",
        "pattern":"[" if case == "invalid_pattern" else "start_cache"})
    if case == "stale":
        monkeypatch.setattr(prewrite,"assess_lesson",lambda *args:{"state":"needs_recheck"})
    result=write_file(_args(tmp_path_factory,content="start_cache()",
        _lesson_context={"project_name":"project","explicit_user_grant":True}))
    assert result["error"] == "LESSON_CHECK_UNAVAILABLE"
    assert result["effect_state"] == "not_committed"
    assert result["lesson_feedback"]["mandatory_check_state"] == "failed"
    assert not (tmp_path_factory/"cache.py").exists()


def test_native_read_advice_uses_only_trusted_data_not_private_stores(tmp_path_factory,monkeypatch):
    from backend.core.knowledge import prewrite
    from backend.core.tools.workspace_tools import read_file
    _lesson(tmp_path_factory)
    args=_args(tmp_path_factory)
    (tmp_path_factory/"cache.py").write_text("start_cache()",encoding="utf8")
    inventory=prewrite.prepare_read_inventory(tmp_path_factory,args)
    def forbidden(*args,**kwargs):
        raise AssertionError("Native read tried to access Host-owned facts")
    monkeypatch.setattr(prewrite,"_load",forbidden)
    monkeypatch.setattr(prewrite,"_is_registered",forbidden)
    monkeypatch.setattr(prewrite,"assess_lesson",forbidden)
    result=read_file({"_workspace":str(tmp_path_factory),"path":"cache.py",
        "_lesson_read_inventory":inventory,"bind_dependency":True})
    assert result["lesson_recall"]["state"] == "completed"
    assert result["lesson_advisories"][0]["lesson_id"] == "known"
    assert "warnings" not in result
    assert result["input_dependency"]["sha256"] == hashlib.sha256(b"start_cache()").hexdigest()


def test_compatibility_dispatch_refuses_undeclared_callable(tmp_path_factory):
    calls=[]
    p=process(tmp_path_factory,"untyped")
    result=ToolDispatcher(RingGate(),{"untyped":lambda args:calls.append(args)}).dispatch(p,"untyped",{})
    assert result.allowed is False
    assert result.error == "UNREGISTERED_EXECUTION_CONTRACT"
    assert not calls
