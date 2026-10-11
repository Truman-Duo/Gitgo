from types import SimpleNamespace

from backend.core.daemon import dispatch
from backend.core.loop import executor


def test_btw_failure_retains_structured_error_and_notifies_user(tmp_path_factory, monkeypatch):
    storage = object()
    task_error = {"code": "CAPABILITY_TOOL_UNAVAILABLE", "message": "Unavailable result reader"}

    def failed(process, **kwargs):
        assert process.bound_storage is storage
        assert not hasattr(process, "_manager")
        return {"status": "failed", "response": "", "error": task_error}

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(executor, "agent_step", failed)
    monkeypatch.setattr(dispatch.threading, "Thread", InlineThread)
    events = []
    dispatch._cmd_task(
        {"action": "btw", "question": "Inspect this file", "sidecar_id": "isolated"},
        SimpleNamespace(workspace_path=tmp_path_factory), None,
        {"apm": SimpleNamespace(storage=storage), "llm": object(), "dispatcher": object()},
        events.append,
    )
    error = next(e for e in events if e.get("event") == "error")
    assert error["code"] == task_error["code"] and error["btw"] is True
    result = next(e["result"] for e in events if e.get("event") == "btw_complete")
    assert result["status"] == "failed"
    assert result["error"] == task_error
    assert result["answer"] == task_error["message"]
