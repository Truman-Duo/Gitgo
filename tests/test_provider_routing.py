from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.core.application import ApplicationServices, OperationError
from backend.core.llm_config import LLMConfigManager
from backend.core.loop.llm import StreamCancelledError, StreamInterruptedError
from backend.core.loop.provider_policy import provider_endpoint
from backend.core.loop.provider_protocol import ProviderEventType
from backend.core.loop.provider_router import CircuitBreaker, BreakerRegistry, RoutedProvider, ProviderRoutesUnavailable, failure_kind
from backend.core.loop.provider_transport import LocalProviderTransport, ProviderHttpError
from backend.core.loop.session import AgentSession


def config(url):
    return {'active_provider': 'main', 'failover_enabled': True, 'failover_order': ['backup'],
            'circuit_breaker': {'failure_threshold': 1},
            'providers': [{'id': p, 'name': p, 'base_url': url + '/' + p, 'api_key': '',
                           'model_id': p, 'kind': 'local', 'auth_mode': 'none'} for p in ('main', 'backup')]}


@pytest.fixture
def server():
    calls = []
    mode = {'main': 'fail', 'backup': 'success'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            route = self.path.split('/')[1]
            calls.append({'route': route, 'body': body, 'authorization': self.headers.get('Authorization')})
            if mode[route] == 'fail':
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'upstream unavailable')
                return
            if mode[route] == 'bad_request':
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'invalid request')
                return
            if mode[route] in {'redirect', 'rate_limit'}:
                self.send_response(302 if mode[route] == 'redirect' else 429)
                self.send_header('Location', '/backup/chat/completions')
                self.end_headers()
                self.wfile.write(b'rejected')
                return
            if not body.get('stream'):
                semantic = any('Assess requirement readiness' in str(m.get('content', '')) for m in body.get('messages', []))
                if semantic:
                    payload = json.loads(body['messages'][-1]['content'])
                    content = json.dumps({'status': 'ready', 'interpretations': [], 'questions': [],
                        'request_frame': {'kind': 'deliverable', 'source_quote': payload['request'],
                                          'outcome': 'Answer the requested question'}})
                else:
                    content = 'pong'
                wire = json.dumps({'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(wire)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for data in ({'id': 'response', 'choices': [{'delta': {'content': 'pong'}}]},
                         {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}, '[DONE]'):
                value = data if isinstance(data, str) else json.dumps(data)
                self.wfile.write(('data: ' + value + '\n\n').encode())
                self.wfile.flush()
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{http.server_port}', calls, mode
    http.shutdown()
    http.server_close()
    thread.join(3)


def test_real_http_failover_uses_backup_and_never_mutates_primary(server):
    url, calls, _ = server
    raw = config(url)
    runtime = RoutedProvider(raw, registry=BreakerRegistry())
    session = AgentSession()
    session.append_user('ping')
    charged = []
    events = list(runtime.stream_session_events(session, before_attempt=lambda *_: charged.append(True) or 3))
    assert [c['route'] for c in calls] == ['main', 'backup']
    assert len(charged) == 2
    assert all(c['authorization'] is None for c in calls)
    assert ''.join(e.text for e in events) == 'pong'
    assert runtime.status()[0]['breaker_state'] == 'open'
    assert raw['active_provider'] == 'main'
    assert session.active_provider_route == runtime.clients['backup'].route_key
    assert sum(e.type == ProviderEventType.ROUTING_NOTICE and e.artifact['event'] == 'provider_request_started' for e in events) == 2
    # An open primary incurs no HTTP request on the next logical call.
    list(runtime.stream_session_events(session))
    assert [c['route'] for c in calls] == ['main', 'backup', 'backup']


def test_client_error_does_not_try_backup_or_damage_health(server):
    url, calls, mode = server
    mode['main'] = 'bad_request'
    runtime = RoutedProvider(config(url), registry=BreakerRegistry())
    with pytest.raises(StreamInterruptedError):
        list(runtime.stream_session_events(AgentSession()))
    assert [c['route'] for c in calls] == ['main']
    assert runtime.status()[0]['failures'] == 0


def test_effective_route_survives_new_worker_and_failed_recovery_probe(server):
    url, _, mode = server
    runtime = RoutedProvider(config(url), registry=BreakerRegistry())
    session = AgentSession()
    first = list(runtime.stream_session_events(session))
    def activations(events):
        return [e.artifact for e in events if e.type == ProviderEventType.ROUTING_NOTICE
                and e.artifact['event'] == 'provider_route_activated']
    assert activations(first)[0]['route_changed']
    def resumed():
        assert runtime.bind_session(session)._model == 'backup'
        return list(runtime.stream_session_events(session))
    with ThreadPoolExecutor(max_workers=1) as pool:
        second = pool.submit(resumed).result()
    assert not activations(second)[0]['route_changed']
    runtime.breakers['main'].opened_at -= 61
    probe = list(runtime.stream_session_events(session))
    assert not activations(probe)[0]['route_changed']
    assert any(e.artifact.get('event') == 'provider_route_failed' for e in probe)
    runtime.breakers['main'].opened_at -= 61
    mode['main'] = 'success'
    recovered = list(runtime.stream_session_events(session))
    assert activations(recovered)[0]['route_changed']
    assert activations(recovered)[0]['provider_id'] == 'main'


def test_missing_primary_configuration_notifies_initial_backup_once(server):
    url, _, _ = server
    raw = config(url)
    raw['providers'][0]['auth_mode'] = 'api_key'  # credential unavailable
    runtime = RoutedProvider(raw, registry=BreakerRegistry())
    session = AgentSession()
    session.bind_provider_route(runtime.bind_session(session).route_key)
    first = list(runtime.stream_session_events(session))
    second = list(runtime.stream_session_events(session))
    activated = [e.artifact for e in first + second if e.type == ProviderEventType.ROUTING_NOTICE
                 and e.artifact['event'] == 'provider_route_activated']
    assert [event['route_changed'] for event in activated] == [True, False]


def test_executor_recompiles_identity_before_backup_request(server):
    from backend.core.loop.executor import agent_step
    from backend.core.loop.models import RingLevel
    from backend.core.loop.runtime import AgentRuntimeFactory, RuntimeSpec
    from backend.core.loop.tools import ToolRegistry
    url, calls, _ = server
    runtime = RoutedProvider(config(url), registry=BreakerRegistry())
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role='supervisor', actor_kind='supervisor', capability_profile_id='supervisor.answer',
        ring_level=RingLevel.RING_0, tool_registry=ToolRegistry([]),
        max_steps=5, task_kind='answer', model_id='main', provider_id='main'))
    agent_step(process, runtime, instruction='Which model are you?')
    wire = json.dumps(next(c['body']['messages'] for c in reversed(calls) if c['body'].get('stream')))
    assert 'Current model: backup' in wire
    assert '[HOST TASK CONTRACT UPDATE]' in wire
    assert process.model_id == 'backup'
    assert process.runtime_preferences['effective_provider_id'] == 'backup'
    assert process.provider_id == 'main'  # admission authority stays configured


def test_auxiliary_search_capability_does_not_change_conversation_wire_capabilities():
    from types import SimpleNamespace
    from backend.core.loop.capability_negotiation import apply_provider_runtime, build_capability_snapshot
    from backend.core.llm_config import LLMProvider as ConfiguredProvider
    from backend.core.tools.web_tools import provider_search_transport
    configured = ConfiguredProvider.from_dict({'id': 'backup', 'name': 'backup',
        'base_url': 'https://api.deepseek.com/v1', 'model_id': 'deepseek-v4-flash',
        'protocol': 'openai_responses', 'capabilities': {'hosted_web_search': False}})
    transport = provider_search_transport(configured)
    assert transport == 'deepseek_messages'
    process = SimpleNamespace(model_id='main', runtime_preferences={})
    apply_provider_runtime(process, provider_id='backup', route_key='backup-route',
        model_id=configured.model_id, protocol=configured.protocol,
        capabilities=configured.runtime_capabilities(), hosted_search_transport=transport)
    snapshot = build_capability_snapshot(model_id=process.model_id, protocol=configured.protocol,
        capabilities=process.runtime_preferences['provider_capabilities'],
        runtime_preferences=process.runtime_preferences, available_tools={'web_search'})
    assert not snapshot['provider_capabilities']['hosted_web_search']
    assert snapshot['web_access']['search_state'] == 'available_native'
    assert snapshot['model']['provider_id'] == 'backup'


def test_cancelled_call_never_contacts_backup(server):
    url, calls, _ = server
    runtime = RoutedProvider(config(url), registry=BreakerRegistry())
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(StreamCancelledError):
        list(runtime.stream_session_events(AgentSession(), cancel_event=cancelled))
    assert not calls


def test_route_change_keeps_dialogue_and_excludes_foreign_native_state(server):
    url, calls, _ = server
    runtime = RoutedProvider(config(url), registry=BreakerRegistry())
    session = AgentSession()
    session.bind_provider_route(runtime.clients['main'].route_key)
    session.append_user('public question')
    session.append_assistant_provider('public answer', continuation_state={'reasoning_details': ['private-native-reasoning']})
    list(runtime.stream_session_events(session))
    wire = calls[-1]['body']['messages']
    assert any(m.get('content') == 'public answer' for m in wire)
    assert 'private-native-reasoning' not in json.dumps(wire)


def test_partial_stream_never_replays_on_backup():
    raw = config('http://127.0.0.1:9999')
    runtime = RoutedProvider(raw, registry=BreakerRegistry())
    invoked = []
    def partial(*args, **kwargs):
        from backend.core.loop.provider_protocol import ProviderEvent
        invoked.append('main')
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text='already visible')
        raise StreamInterruptedError()
    runtime.clients['main'].stream_events = partial
    runtime.clients['backup'].stream_events = lambda *a, **k: invoked.append('backup')
    observed = []
    with pytest.raises(StreamInterruptedError):
        for event in runtime.stream_session_events(AgentSession()):
            observed.append(event)
    assert invoked == ['main']
    assert any(e.text == 'already visible' for e in observed)


def test_half_open_allows_one_probe_and_neutral_close_releases_it():
    now = [0.0]
    breaker = CircuitBreaker({'failure_threshold': 1, 'cooldown_seconds': 1}, clock=lambda: now[0])
    breaker.finish(breaker.acquire(), False)
    assert breaker.acquire() is None
    now[0] = 2
    with ThreadPoolExecutor(max_workers=8) as pool:
        permits = list(pool.map(lambda _: breaker.acquire(), range(16)))
    granted = [p for p in permits if p is not None]
    assert len(granted) == 1 and granted[0].probe
    breaker.finish(granted[0], None)
    retry = breaker.acquire()
    assert retry is not None
    breaker.finish(retry, True)
    assert breaker.status()['breaker_state'] == 'half_open'
    breaker.finish(breaker.acquire(), True)
    assert breaker.status()['breaker_state'] == 'closed'


def test_stale_success_cannot_close_new_open_breaker():
    breaker = CircuitBreaker({'failure_threshold': 1})
    earlier, failed = breaker.acquire(), breaker.acquire()
    breaker.finish(failed, False)
    breaker.finish(earlier, True)
    assert breaker.status()['breaker_state'] == 'open'


def test_closing_generator_releases_half_open_permit(server):
    url, _, _ = server
    runtime = RoutedProvider(config(url), registry=BreakerRegistry())
    breaker = runtime.breakers['main']
    breaker.finish(breaker.acquire(), False)
    breaker.opened_at -= 61
    stream = runtime.stream_session_events(AgentSession())
    next(stream)  # selected, but socket has not yet been opened
    assert breaker.status()['probe_inflight']
    stream.close()
    assert not breaker.status()['probe_inflight']


@pytest.mark.parametrize('endpoint', ['http://example.com/v1', 'http://192.168.1.2/v1', 'http://127.0.0.1@evil.example/v1'])
def test_local_endpoint_rejects_remote_or_embedded_credentials(endpoint):
    with pytest.raises(ValueError):
        provider_endpoint(endpoint, kind='local', auth_mode='none')


def test_local_keyless_provider_and_backup_queue_roundtrip(tmp_path_factory, monkeypatch):
    root = tmp_path_factory / 'routing-config'
    root.mkdir()
    monkeypatch.setenv('GITGO_LLM_CONFIG_PATH', str(root / 'llm_config.json'))
    monkeypatch.setenv('GITGO_LLM_SECRET_PATH', str(root / 'provider_secrets.json'))
    service = ApplicationServices()
    created = service.provider_save(name='Local model', model_id='local', base_url='http://localhost:11434/v1', kind='local', auth_mode='none')
    p = created['provider']
    assert not p['api_key_present'] and p['base_url'].startswith('http://127.0.0.1:')
    assert LLMConfigManager.get_active().auth_mode == 'none'
    service.provider_failover_configure(True, [p['id']])
    assert service.provider_status()['failover_order'] == [p['id']]
    with pytest.raises(OperationError):
        service.provider_failover_configure(True, ['unknown'])
    service.provider_delete(p['id'])
    assert service.provider_status()['failover_order'] == []


def test_context_overflow_is_neutral_for_health():
    assert failure_kind(StreamInterruptedError(status_code=400, error_body='context length exceeded')) == (False, 'context_overflow')


def test_sync_rate_limit_immediately_fails_over_and_counts_each_attempt(server):
    url, calls, mode = server
    mode['main'] = 'rate_limit'
    runtime = RoutedProvider(config(url), registry=BreakerRegistry())
    charged, notices = [], []
    answer = runtime.chat([{'role': 'user', 'content': 'ping'}],
        before_attempt=lambda *_: charged.append(True) or 3, on_notice=notices.append)
    assert answer == 'pong'
    assert len(charged) == 2
    assert [c['route'] for c in calls] == ['main', 'backup']
    assert any(n['event'] == 'provider_route_failed' and n['reason'] == 'HTTP_429' for n in notices)


def test_sync_session_switch_filters_foreign_state(server):
    url, calls, _ = server
    runtime = RoutedProvider(config(url), registry=BreakerRegistry())
    session = AgentSession()
    session.bind_provider_route(runtime.clients['main'].route_key)
    session.append_assistant_provider('public', continuation_state={'reasoning_details': ['private-native']})
    assert runtime.chat([], session=session) == 'pong'
    assert 'private-native' not in json.dumps(calls[-1]['body'])
    assert session.active_provider_route == runtime.clients['backup'].route_key


def test_budget_denial_contacts_no_provider_and_releases_recovery_probe(server):
    from backend.core.loop.budget import TaskBudgetExceeded
    url, calls, _ = server
    runtime = RoutedProvider(config(url), registry=BreakerRegistry())
    breaker = runtime.breakers['main']
    breaker.finish(breaker.acquire(), False)
    breaker.opened_at -= 61
    def deny(*_):
        raise TaskBudgetExceeded('provider_calls', {'provider_calls': 1})
    with pytest.raises(TaskBudgetExceeded):
        list(runtime.stream_session_events(AgentSession(), before_attempt=deny))
    assert not calls and not breaker.status()['probe_inflight']
    assert breaker.status()['failures'] == 1


def test_provider_credentials_are_never_borrowed_by_backup(server):
    url, calls, _ = server
    raw = config(url)
    for provider in raw['providers']:
        provider.update(auth_mode='api_key', api_key=provider['id'] + '-test-key')
    list(RoutedProvider(raw, registry=BreakerRegistry()).stream_session_events(AgentSession()))
    assert [c['authorization'] for c in calls] == ['Bearer main-test-key', 'Bearer backup-test-key']


def test_local_http_bypasses_proxy_and_rejects_redirect(server, monkeypatch):
    url, calls, mode = server
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    assert LocalProviderTransport().post_json(url + '/backup/chat/completions', {}, {}, timeout=2)['choices']
    mode['main'] = 'redirect'
    with pytest.raises(ProviderHttpError) as caught:
        LocalProviderTransport().post_json(url + '/main/chat/completions', {}, {}, timeout=2)
    assert caught.value.status_code == 302
    assert [c['route'] for c in calls] == ['backup', 'main']


def test_rolling_error_rate_opens_circuit_before_consecutive_threshold():
    breaker = CircuitBreaker({'failure_threshold': 9, 'min_requests': 4, 'window_size': 4,
                              'error_rate_threshold': .5})
    for success in (False, True, False, True):
        breaker.finish(breaker.acquire(), success)
    # Error rate is evaluated on failure; the third failure slides the window.
    breaker.finish(breaker.acquire(), False)
    assert breaker.status()['breaker_state'] == 'open'


def test_envelope_is_rebuilt_for_the_selected_backup(server):
    url, calls, _ = server
    runtime = RoutedProvider(config(url), registry=BreakerRegistry())
    list(runtime.stream_session_events(AgentSession(), current_turn_envelope=lambda: 'selected-model=' + runtime._model))
    assert 'selected-model=main' in json.dumps(calls[0]['body'])
    assert 'selected-model=backup' in json.dumps(calls[1]['body'])
    assert 'selected-model=main' not in json.dumps(calls[1]['body'])


def test_exhausted_routes_surface_failure_without_unbounded_retries(server):
    url, calls, mode = server
    mode['backup'] = 'fail'
    runtime = RoutedProvider(config(url), registry=BreakerRegistry())
    with pytest.raises(ProviderRoutesUnavailable) as caught:
        list(runtime.stream_session_events(AgentSession()))
    assert len(caught.value.attempts) == 2
    assert [c['route'] for c in calls] == ['main', 'backup']
    with pytest.raises(ProviderRoutesUnavailable):
        list(runtime.stream_session_events(AgentSession()))
    assert len(calls) == 2


def test_dashboard_profile_requires_user_to_enable_backup_and_preserves_original(tmp_path_factory, monkeypatch):
    from scripts.provider_dashboard_test_launcher import initialize
    original_config = tmp_path_factory / 'original.json'
    original_secrets = tmp_path_factory / 'original-secrets.json'
    monkeypatch.setenv('GITGO_LLM_CONFIG_PATH', str(original_config))
    monkeypatch.setenv('GITGO_LLM_SECRET_PATH', str(original_secrets))
    LLMConfigManager.save({'providers': [{'id': 'installed', 'name': 'Installed', 'model_id': 'installed',
        'api_key': 'fixture-private-key', 'base_url': 'https://example.invalid/v1'}], 'active_provider': 'installed'})
    originals = (original_config.read_bytes(), original_secrets.read_bytes())
    profile = tmp_path_factory / 'profile'
    profile.mkdir()
    initialize(profile, {})
    public = json.loads((profile / 'llm_config.json').read_text())
    assert public['active_provider'] == 'invalid-openai-acceptance'
    assert public['failover_enabled'] is False and public['failover_order'] == []
    assert [p['id'] for p in public['providers']] == ['invalid-openai-acceptance', 'installed']
    assert 'fixture-private-key' not in (profile / 'provider_secrets.json').read_text()
    assert (original_config.read_bytes(), original_secrets.read_bytes()) == originals
