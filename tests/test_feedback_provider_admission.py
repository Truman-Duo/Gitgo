"""Known uncertain feedback checkpoints stop new provider admission, not in-flight work."""
from unittest.mock import patch

import pytest

from tests.test_feedback_call_identity import delivery
from backend.core.loop.executor import agent_step
from backend.core.loop.provider_request_surface import prepare_provider_request
from backend.core.loop.tools import ToolRegistry
from backend.core.storage import StorageCommitUnknown


@pytest.mark.parametrize('routed', [False, True])
@pytest.mark.parametrize('budgeted', [False, True])
def test_unknown_before_provider_admission_spends_no_call(delivery, routed, budgeted):
    root, child, tools, store, ctx, send = delivery
    child.capability_profile_id = 'text.only'
    child.tool_registry = ToolRegistry([])
    if not budgeted:
        child.task_budget = None
    class Provider:
        calls = 0
        max_output_tokens = 128
        def stream_chat(self, *args, **kwargs):
            self.calls += 1
            yield {'choices': [{'delta': {'content': 'This request must not start.'}}]}
    class RoutedProvider(Provider):
        def stream_session_events(self, session, **kwargs):
            kwargs['before_attempt'](0, self)
            self.calls += 1
            pytest.fail('routed provider must not start')
            yield
    provider = RoutedProvider() if routed else Provider()
    def surface_then_peer_unknown(*args, **kwargs):
        request = prepare_provider_request(*args, **kwargs)
        with patch.object(store._storage, 'save_agent_checkpoint',
                side_effect=StorageCommitUnknown('unknown after turn preparation, before new provider admission')):
            assert send().data['code'] == 'FEEDBACK_COMMIT_UNKNOWN'
        return request
    used = root.task_budget.snapshot()['used']['provider_calls']
    with patch('backend.core.loop.provider_request_surface.prepare_provider_request', surface_then_peer_unknown):
        result = agent_step(child, provider, instruction='Report facts', workspace_path=ctx.workspace_path)
    assert provider.calls == 0
    assert root.task_budget.snapshot()['used']['provider_calls'] == used
    assert result['error']['code'] == 'PROCESS_RECOVERY_REQUIRED'
    assert child.recovery['checkpoint_commit'] == 'unknown'
    assert child.mailbox.snapshot()['pending'] == 1


def test_routed_next_attempt_blocks_unknown_without_undoing_prior_admission(delivery):
    root, child, tools, store, ctx, send = delivery
    child.capability_profile_id = 'text.only'
    child.tool_registry = ToolRegistry([])
    class Provider:
        calls = 0
        max_output_tokens = 128
        def stream_session_events(self, session, **kwargs):
            kwargs['before_attempt'](0, self)
            self.calls += 1
            # The first request was already admitted; only its next attempt is
            # blocked. Admission holds no lock while a provider is in flight.
            from concurrent.futures import ThreadPoolExecutor
            with patch.object(store._storage, 'save_agent_checkpoint', side_effect=StorageCommitUnknown('unknown between attempts')):
                with ThreadPoolExecutor(1) as pool:
                    assert pool.submit(send).result(timeout=3).data['code'] == 'FEEDBACK_COMMIT_UNKNOWN'
            kwargs['before_attempt'](1, self)
            self.calls += 1
            pytest.fail('second provider attempt must not start')
            yield
    provider = Provider()
    used = child.task_budget.snapshot()['used']['provider_calls']
    result = agent_step(child, provider, instruction='Report facts', workspace_path=ctx.workspace_path)
    assert provider.calls == 1
    assert child.task_budget.snapshot()['used']['provider_calls'] == used + 1
    assert result['error']['code'] == 'PROCESS_RECOVERY_REQUIRED'
    assert child.mailbox.snapshot()['pending'] == 1


def test_sync_fallback_obeys_the_same_provider_admission_gate(delivery):
    from types import SimpleNamespace
    import time
    from backend.core.loop.provider_turn_runtime import ProviderTurnRuntime
    root, child, tools, store, ctx, send = delivery
    with patch.object(store._storage, 'save_agent_checkpoint', side_effect=StorageCommitUnknown('unknown before fallback')):
        assert send().data['code'] == 'FEEDBACK_COMMIT_UNKNOWN'
    used = child.task_budget.snapshot()['used']['provider_calls']
    provider = SimpleNamespace(chat=lambda *a, **kw: pytest.fail('fallback must not start'))
    runtime = ProviderTurnRuntime(child, child.session, provider, emit=lambda *a, **k: None,
        frontend_emit=None, route_changed=lambda value: None, route=lambda: '', envelope=lambda: '', compact=None)
    runtime.started = time.time()
    outcome = runtime.fallback()
    assert outcome.result['error']['code'] == 'PROCESS_RECOVERY_REQUIRED'
    assert child.task_budget.snapshot()['used']['provider_calls'] == used
    assert child.mailbox.snapshot()['pending'] == 1


def test_budget_wait_rechecks_unknown_after_releasing_the_admission_fence():
    from concurrent.futures import ThreadPoolExecutor
    from tests.test_agent_lifecycle_closeout import _blocked_tree
    from backend.core.loop.budget_runtime import reserve_provider_call
    from backend.core.loop.lifecycle import block_uncertain_checkpoint
    from backend.core.loop.mailbox import MailboxRecoveryRequired
    from backend.core.loop.mechanism_lifecycle import fence
    manager, root, child = _blocked_tree()
    def waiting(*args, **kwargs):
        def peer_checkpoint():
            with fence(root), root.session._mechanism_lock:
                block_uncertain_checkpoint(root)
        with ThreadPoolExecutor(1) as pool:
            pool.submit(peer_checkpoint).result(timeout=3)
        return []
    used = root.task_budget.snapshot()['used']['provider_calls']
    with patch.object(manager, 'wait_many', waiting):
        with pytest.raises(MailboxRecoveryRequired):
            reserve_provider_call(root)
    assert root.task_budget.snapshot()['used']['provider_calls'] == used
