"""The optional control MCP must remain a thin canonical-NativeHost adapter."""

from __future__ import annotations

from types import SimpleNamespace

import mcp_tools.control as control


class CaptureMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, **_kwargs):
        def decorate(function):
            self.tools[function.__name__] = function
            return function
        return decorate


class FakeHost:
    def __init__(self):
        self.calls = []

    def _runtime_status(self, project):
        self.calls.append(("status", project))
        return {"project": project, "canonical": True}

    def _runtime_chat(self, request_id, arguments, *, action="chat"):
        self.calls.append(("chat", request_id, arguments, action))
        return {"project": arguments["project"], "action": action}

    def _runtime_feedback(self, project, process_id, message, *, request_id=""):
        self.calls.append(("feedback", project, process_id, message, request_id))
        return {"continued": True}

    def _runtime_stop(self, project, process_id):
        self.calls.append(("stop", project, process_id))
        return {"cancelled": True}

    def _runtime_compact(self, project, process_id="", *, decision_id="", choice=""):
        self.calls.append(("compact", project, process_id, decision_id, choice))
        return {"status": "completed"}

    def _runtime_trace(self, project, arguments):
        self.calls.append(("trace", project, arguments))
        return {"project": project, "limit": arguments["limit"]}

    def close(self):
        self.calls.append(("close",))


def test_control_mcp_exposes_only_bounded_native_host_operations(monkeypatch):
    fake = FakeHost()
    monkeypatch.setattr(control, "_HOST", fake)
    mcp = CaptureMcp()
    control.register(mcp)

    assert set(mcp.tools) == {
        "gitgo_control_status", "gitgo_control_chat", "gitgo_control_decide",
        "gitgo_control_feedback", "gitgo_control_stop",
        "gitgo_control_compact", "gitgo_control_trace",
    }
    assert mcp.tools["gitgo_control_status"]("demo")["canonical"] is True
    assert mcp.tools["gitgo_control_chat"](
        "demo", "do it", task_kind="action", max_steps=999,
        manual_delegation=True, fresh_session=True,
    )["action"] == "chat"
    chat = next(item for item in fake.calls if item[0] == "chat")
    assert chat[2] == {
        "project": "demo", "message": "do it", "max_steps": 999,
        "manual_delegation": True, "session_mode": "fresh",
        "task_kind": "action",
    }

    assert mcp.tools["gitgo_control_decide"](
        "demo", "t-1", "p-1", "d-1", "Allow once",
    )["action"] == "decision"
    decision = [item for item in fake.calls if item[0] == "chat"][-1]
    assert decision[2]["task_id"] == "t-1"
    assert decision[2]["process_id"] == "p-1"
    assert decision[2]["decision_id"] == "d-1"
    assert mcp.tools["gitgo_control_feedback"]("demo", "b-1", "revise")["continued"]
    assert mcp.tools["gitgo_control_stop"]("demo", "b-1")["cancelled"]
    assert mcp.tools["gitgo_control_compact"](
        "demo", "a-1", "d-2", "force_compact",
    )["status"] == "completed"
    assert mcp.tools["gitgo_control_trace"]("demo", limit=50_000)["limit"] == 1000


def test_control_mcp_shutdown_closes_and_forgets_singleton(monkeypatch):
    fake = FakeHost()
    monkeypatch.setattr(control, "_HOST", fake)
    control.shutdown()
    assert fake.calls == [("close",)]
    assert control._HOST is None
