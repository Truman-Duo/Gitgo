"""Task-boundary provider selection must not restart conversation ownership."""
import io
import pytest
from backend.core.application.services import OperationError
from backend.core.daemon.policy_helpers import _refresh_runtime_llm
from backend.core.llm_config import LLMConfigManager, LLMProvider
from backend.core.native_host import NativeHost


def configure():
    first = LLMConfigManager.add(LLMProvider(name='GPT', base_url='http://127.0.0.1:9201/v1',
        api_key='', model_id='first', kind='local', auth_mode='none', protocol='openai_responses'))
    second = LLMConfigManager.add(LLMProvider(name='DeepSeek', base_url='http://127.0.0.1:9202/v1',
        api_key='', model_id='second', kind='local', auth_mode='none', protocol='openai_chat'))
    LLMConfigManager.switch(first.id)
    return first, second


def test_sequential_provider_switch_preserves_daemons_sessions_and_selects_next_client():
    first, second = configure()
    ctx = {}
    old = _refresh_runtime_llm(ctx, '.')
    host = NativeHost(stdin=io.StringIO(), stdout=io.StringIO())
    class Idle:
        def stop(self):
            raise AssertionError('A provider switch must not stop or restore project state')
    daemon = Idle()
    host._daemons['project'] = daemon
    host._sessions['project'] = 'same-session'
    host._conversation_processes['project'] = 'same-process'
    for target in (second, first, second):
        result = host._provider_switch(target.id)
        assert not result['runtime_reload'] and result['retired_projects'] == []
        assert host._daemons['project'] is daemon
        assert host._sessions['project'] == 'same-session'
        assert host._conversation_processes['project'] == 'same-process'
        selected = _refresh_runtime_llm(ctx, '.')
        assert selected._model == target.model_id
        assert _refresh_runtime_llm(ctx, '.') is selected
    # A worker that already captured its client still holds that same route.
    assert old._model == first.model_id
    host._daemons.clear()
    host.close()


def test_busy_or_invalid_switch_leaves_cached_route_and_history_owner_intact():
    first, second = configure()
    ctx = {}
    original = _refresh_runtime_llm(ctx, '.')
    host = NativeHost(stdin=io.StringIO(), stdout=io.StringIO())
    host._active_btw['busy'] = {}
    with pytest.raises(OperationError, match='safe boundary'):
        host._provider_switch(second.id)
    host._active_btw.clear()
    with pytest.raises(OperationError):
        host._provider_switch('missing-provider')
    assert LLMConfigManager.get_active().id == first.id
    assert _refresh_runtime_llm(ctx, '.') is original
    host.close()


def test_injected_client_retains_its_explicit_owner():
    configure()
    injected = object()
    assert _refresh_runtime_llm({'llm': injected}, '.') is injected
