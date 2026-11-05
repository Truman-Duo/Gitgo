from copy import deepcopy

from backend.core.loop.context_window import ContextWindow
from backend.core.loop.manager import SessionStore
from backend.core.loop.manual_compaction import compact_parked_session
from backend.core.loop.session import AgentSession


class FailingProvider:
    max_output_tokens = 2048

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        assert kwargs["max_retries"] == 0
        self.calls += 1
        raise RuntimeError("provider context limit exceeded")


def session_with_history():
    session = AgentSession(model_context_limit=4096)
    session.messages = [
        {"role": "system", "message_type": "compiled_system_prompt", "content": "ROM"},
        {"role": "user", "message_type": "compiled_task_contract", "content": "contract"},
    ] + [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}: " + "x" * 1000}
         for i in range(8)]
    return session


def compact(session, provider=None, **kwargs):
    return compact_parked_session(session, provider, process_id="b-1", task_id="task-1", **kwargs)


def fail_three(session, provider):
    assert compact(session, provider)["attempt"] == 1
    assert compact(session, provider)["attempt"] == 2
    result = compact(session, provider)
    assert result["status"] == "awaiting_user"
    assert provider.calls == 3
    return result["pending_decision"]["decision_id"]


def test_three_failures_park_without_mutation_or_hidden_retries():
    session, provider = session_with_history(), FailingProvider()
    before = deepcopy(session.messages)
    decision_id = fail_three(session, provider)
    assert compact(session, provider)["pending_decision"]["decision_id"] == decision_id
    assert provider.calls == 3
    assert session.messages == before
    assert session.context_epoch == 0


def test_summary_budget_respects_configured_ceiling_and_auxiliary_workload():
    class SummaryProvider:
        max_output_tokens = 8192
        supports_generation_purpose = True
        def chat(self, messages, **kwargs):
            self.kwargs = kwargs
            return 'The accepted goal, real decisions and outstanding checks.'
    provider = SummaryProvider()
    assert compact(session_with_history(), provider)['status'] == 'completed'
    assert provider.kwargs['max_tokens'] == provider.max_output_tokens
    assert provider.kwargs['purpose'] == 'context_compaction'
    assert provider.kwargs['max_retries'] == 0
    provider.max_output_tokens = 1024
    assert compact(session_with_history(), provider)['status'] == 'completed'
    assert provider.kwargs['max_tokens'] == 1024


def test_reasoning_summary_uses_configured_budget_and_accounts_invisible_paid_output():
    from types import SimpleNamespace
    charged, usage = [], []
    budget = SimpleNamespace(begin_provider_call=lambda *a: None,
        record_provider_usage=lambda value: usage.append(value),
        consume_output_estimate=lambda count, process: charged.append((count, process)),
        consume_output=lambda *a: (_ for _ in ()).throw(AssertionError('Do not double-charge visible text')))
    class Provider:
        max_output_tokens = 8192
        def chat(self, messages, **kwargs):
            assert kwargs['max_tokens'] == 8192
            return {'content': 'A short factual summary after internal reasoning.',
                'usage': {'output_tokens': 5100, 'reasoning_tokens': 5000}}
    session = session_with_history()
    assert ContextWindow().compact(session, Provider(), task_budget=budget, budget_process_id='unit-summary')
    assert charged == [(5100, 'unit-summary')] and len(usage) == 1
    assert session.provider_usage[-1]['purpose'] == 'context_compaction'
    assert session.context_epoch == 1


def test_incomplete_summary_retains_paid_usage_and_keeps_original_epoch():
    from backend.core.loop.provider_protocol import ProviderResponseIncomplete
    class IncompleteProvider:
        max_output_tokens = 8192
        def chat(self, messages, **kwargs):
            raise ProviderResponseIncomplete('max_output_tokens', {'output_tokens': 4096, 'reasoning_tokens': 4096})
    session = session_with_history()
    before = deepcopy(session.messages)
    result = compact(session, IncompleteProvider())
    assert result['status'] == 'failed' and result['attempt'] == 1
    assert session.context_epoch == 0 and session.messages == before
    assert session.provider_usage[-1]['output_tokens'] == 4096
    assert session.provider_usage[-1]['incomplete'] is True


def test_approval_survives_sqlite_checkpoint_and_force_needs_no_provider(tmp_path_factory):
    tmp_path = tmp_path_factory
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session, provider = session_with_history(), FailingProvider()
    before = deepcopy(session.messages)
    decision_id = fail_three(session, provider)
    store = SessionStore(str(workspace), state_home=tmp_path / "state")
    try:
        store.save_checkpoint("b-1", session)
        restored = AgentSession.from_durable_state(store.load_session_state("b-1"))
        assert restored.compaction_failure_count == 3
        result = compact(restored, decision_id=decision_id, choice="force_compact")
        assert result["status"] == "completed"
        assert restored.context_epoch == 1
        assert restored.epoch_archive[-1]["messages"] == before
        assert restored.pending_compaction_decision is None
        assert restored.messages[:2] == before[:2]
        assert restored.estimate_tokens() < session.estimate_tokens()
    finally:
        store.close()


def test_stale_approval_cannot_discard_new_input():
    session, provider = session_with_history(), FailingProvider()
    decision_id = fail_three(session, provider)
    session.append_user("New direction after the approval was offered")
    before = deepcopy(session.messages)
    result = compact(session, decision_id=decision_id, choice="force_compact")
    assert result["status"] == "failed"
    assert "stale" in result["error_info"]["message"]
    assert session.messages == before
    assert not session.epoch_archive


def test_decline_keeps_context_unchanged():
    session, provider = session_with_history(), FailingProvider()
    decision_id = fail_three(session, provider)
    before = deepcopy(session.messages)
    assert compact(session, decision_id=decision_id, choice="stop")["status"] == "cancelled"
    assert session.messages == before
    assert session.pending_compaction_decision is None


def test_force_cannot_silently_remove_an_oversized_immutable_prefix():
    session = session_with_history()
    session.messages[0]["content"] = "x" * 30000
    before = deepcopy(session.messages)
    window = ContextWindow(1024)
    assert not window.force_compact(session, reason="test approval")
    assert window.last_compaction_error == "immutable_prefix_exceeds_context_budget"
    assert session.messages == before
    assert not session.epoch_archive


def test_empty_summary_is_not_a_success():
    class EmptyProvider:
        def chat(self, *_args, **_kwargs):
            return {"content": "  "}

    session = session_with_history()
    before = deepcopy(session.messages)
    result = compact(session, EmptyProvider())
    assert result["status"] == "failed"
    assert session.last_compaction_error == "empty_compaction_summary"
    assert session.messages == before


def test_short_history_is_an_explained_noop_not_a_false_failure():
    session = AgentSession(model_context_limit=4096)
    session.messages = [
        {"role": "system", "message_type": "compiled_system_prompt", "content": "ROM"},
        {"role": "user", "message_type": "compiled_task_contract", "content": "contract"},
        {"role": "assistant", "content": "done"},
    ]
    result = compact(session, provider=None)
    assert result == {
        "status": "completed", "changed": False,
        "previous_epoch": 0, "context_epoch": 0,
        "reason": "not_enough_foldable_history",
    }
