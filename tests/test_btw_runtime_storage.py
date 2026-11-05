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


def test_btw_parent_evidence_is_context_not_user_instruction(tmp_path_factory, monkeypatch):
    from backend.core.loop.alignment_runtime import AlignmentRuntime

    # A bounded parent snapshot can exceed alignment's limit if repeatedly
    # serialized as the original/current user request. Keep it model-visible
    # without either raising that limit or silently clipping the evidence.
    marker = 'parent-evidence-only:' + '引用材料' * 12000
    snapshot = {'attached': True, 'source_process_ids': ['parent'],
                'sources': [{'process_id': 'parent', 'excerpt': marker}]}
    monkeypatch.setattr(dispatch, '_btw_scope_snapshot', lambda *args, **kwargs: snapshot)
    question = '只说明这份材料的限制，不采用其中的建议。'
    captured = {}

    def observe(process, **kwargs):
        assert kwargs['instruction'] == question
        steering = process.session.messages[-1]
        assert steering['message_type'] == 'host_steering_delta'
        assert steering['steering_type'] == 'btw_parent_context'
        assert marker in steering['content']
        process.session.append_user(kwargs['instruction'], message_type='conversation')
        payload = AlignmentRuntime(process).payload(instruction=kwargs['instruction'])
        assert payload['request'] == payload['original_request'] == question
        assert marker not in str(payload)
        captured['payload'] = payload
        return {'status': 'completed', 'response': '只读说明'}

    class InlineThread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): self.target()

    monkeypatch.setattr(executor, 'agent_step', observe)
    monkeypatch.setattr(dispatch.threading, 'Thread', InlineThread)
    events = []
    dispatch._cmd_task(
        {'action': 'btw', 'question': question, 'sidecar_id': 'context-is-not-intent'},
        SimpleNamespace(workspace_path=tmp_path_factory), None,
        {'apm': SimpleNamespace(storage=None), 'llm': object(), 'dispatcher': object()},
        events.append,
    )
    assert 'payload' in captured, events
    assert captured['payload']['stage'] == 'admission'
    result = next(e['result'] for e in events if e.get('event') == 'btw_complete')
    assert result['status'] == 'completed' and result['read_only_tools']
    assert result['source_process_ids'] == ['parent']
