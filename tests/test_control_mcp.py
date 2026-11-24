"""MCP transports original Host operations, defaults and identities intact."""
import pytest
import mcp_tools.control as control
from backend.core.application import OperationError

class CaptureMcp:
    def __init__(self):
        self.tools = {}
    def tool(self, **_):
        def decorate(function):
            self.tools[function.__name__] = function
            return function
        return decorate


def test_control_adapter_preserves_native_arguments_and_product_default_budget(monkeypatch):
    calls = []
    def call(operation, arguments=None, **options):
        calls.append((operation, arguments, options))
        return {"canonical": True}
    monkeypatch.setattr(control, "_call", call)
    mcp = CaptureMcp()
    control.register(mcp)
    assert len(mcp.tools) == 14
    assert mcp.tools["gitgo_control_status"]("demo")["canonical"]
    mcp.tools["gitgo_control_chat"]("demo", "hello", confirm=True, request_id="original-id")
    operation, arguments, options = calls[-1]
    assert operation == "runtime.chat" and "max_steps" not in arguments
    assert options == {"confirm": True, "request_id": "original-id"}
    mcp.tools["gitgo_control_chat"]("demo", "do it", task_kind="action", max_steps=50,
                                     manual_delegation=True, fresh_session=True, confirm=True)
    assert calls[-1][1] == {"project": "demo", "message": "do it", "task_kind": "action", "max_steps": 50,
                           "manual_delegation": True, "session_mode": "fresh"}
    mcp.tools["gitgo_control_decide"]("demo", "task", "process", "decision", "real answer", confirm=True)
    assert calls[-1][0:2] == ("runtime.decision", {"project": "demo", "task_id": "task", "process_id": "process",
                                              "decision_id": "decision", "message": "real answer"})
    mcp.tools["gitgo_control_trace"]("demo", action="detail", ref="cas-ref", options={"view": "input_source", "source_index": 3, "offset": 2000})
    assert calls[-1][1]["view"] == "input_source" and calls[-1][1]["offset"] == 2000
    mcp.tools["gitgo_control_switch"]("p", model_id="model", expected_revision="revision", confirm=True)
    assert calls[-1][0:2] == ("provider.model.select", {"provider_id": "p", "model_id": "model", "expected_revision": "revision"})


def test_missing_attachment_never_creates_a_fallback_host(monkeypatch):
    control.shutdown()
    monkeypatch.delenv("GITGO_CONTROL_DESCRIPTOR", raising=False)
    with pytest.raises(OperationError, match="second Host will not be started"):
        control._call("runtime.status", {"project": "demo"})
    assert not hasattr(control, "_HOST")


def test_shutdown_forgets_only_attachment_and_never_closes_the_dashboard(monkeypatch):
    monkeypatch.setattr(control, "_DESCRIPTOR", "private/attach.json")
    control.shutdown()
    assert control._DESCRIPTOR == ""
