"""Native Host acceptance: invalid primary -> real or local HTTP backup.

--real uses an existing provider's encrypted credential and an existing project
workspace. Configuration, conversations and traces live in an isolated profile;
the installed profile is never overwritten. No credential or request body is
included in the public result. This is protocol acceptance, not visual UI QA.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.core.llm_config import LLMConfigManager
from scripts.smoke_packaged_runtime import ProtocolClient


class ObservedClient(ProtocolClient):
    def __init__(self, *args, **kwargs):
        self.events = []
        super().__init__(*args, **kwargs)

    def _next_message(self, timeout):
        message = super()._next_message(timeout)
        if message.get('type') == 'event':
            self.events.append(message.get('payload', {}))
        return message


def check(host: Path, *, source=False, real=False, project='gitgo', backup_id='', regressions=False):
    original_path = LLMConfigManager._config_path()
    original_bytes = original_path.read_bytes() if original_path.exists() else b''
    original = LLMConfigManager.load() if real else None
    calls = []

    class LocalAPI(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            route = 'main' if self.path.startswith('/main/') else 'backup'
            calls.append(route)
            if route == 'main':
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'upstream unavailable')
                return
            text = 'FAILOVER_OK'
            if body.get('stream'):
                wire = 'data: ' + json.dumps({'id': 'backup', 'choices': [{'delta': {'content': text}}]}) + '\n\n'
                wire += 'data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 50, 'completion_tokens': 5, 'total_tokens': 55}}) + '\n\ndata: [DONE]\n\n'
            else:
                wire = json.dumps({'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
            self.end_headers()
            self.wfile.write(wire.encode())

    http = ThreadingHTTPServer(('127.0.0.1', 0), LocalAPI)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='gitgo-provider-acceptance-') as raw:
            root = Path(raw)
            if real:
                configured = ConfigManager.load()
                chosen = next(p for p in configured.projects if p.name == project)
                cfg = copy.deepcopy(configured)
                cfg.projects = [copy.deepcopy(chosen)]
                providers = original['providers']
                selected = next(p for p in providers if p['id'] == (backup_id or original['active_provider']))
                backup = copy.deepcopy(selected)
                primary = {'id': 'invalid-openai-acceptance', 'name': 'Invalid OpenAI acceptance',
                    'base_url': 'https://api.openai.com/v1', 'api_key': 'sk-invalid-test',
                    'model_id': 'gpt-4o-mini', 'protocol': 'openai_chat', 'context_window': 128000,
                    'max_output_tokens': 1024, 'kind': 'remote', 'auth_mode': 'api_key'}
            else:
                workspace = root / 'workspace'
                workspace.mkdir()
                chosen = ProjectConfig(name=project)
                chosen.workspace_path = str(workspace)
                cfg = Config(projects=[chosen])
                def local(p):
                    return {'id': p, 'name': p, 'base_url': f'http://127.0.0.1:{http.server_port}/{p}/v1',
                            'api_key': '', 'model_id': p, 'protocol': 'openai_chat', 'kind': 'local', 'auth_mode': 'none'}
                primary, backup = local('main'), local('backup')
            config_path = root / 'config.json'
            ConfigManager.save(cfg, config_path, allow_empty=True)
            llm_path, secret_path = root / 'llm_config.json', root / 'provider_secrets.json'
            provider_config = {'providers': [primary, backup], 'active_provider': primary['id'],
                               'failover_enabled': False, 'failover_order': []}
            # Persist with the same encrypted secret store used by the product.
            saved_env = {key: os.environ.get(key) for key in ('GITGO_LLM_CONFIG_PATH', 'GITGO_LLM_SECRET_PATH')}
            try:
                os.environ['GITGO_LLM_CONFIG_PATH'] = str(llm_path)
                os.environ['GITGO_LLM_SECRET_PATH'] = str(secret_path)
                LLMConfigManager.save(provider_config)
            finally:
                for key, value in saved_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            env = os.environ.copy()
            for key in ('GITGO_LLM_BASE_URL', 'GITGO_LLM_API_KEY', 'GITGO_LLM_MODEL', 'GITGO_LLM_PROTOCOL'):
                env.pop(key, None)
            env.update({'GITGO_CONFIG_PATH': str(config_path), 'GITGO_STATE_HOME': str(root / 'state'),
                        'GITGO_LLM_CONFIG_PATH': str(llm_path), 'GITGO_LLM_SECRET_PATH': str(secret_path),
                        'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
            client = ObservedClient(host, env, arguments=['-u', '-m', 'backend.core.native_host'] if source else None)
            def invoke(operation, args=None, timeout=30):
                response = client.call(operation, args, timeout=timeout)
                if not response.get('ok'):
                    raise RuntimeError(f"{operation} failed: {response.get('error', {}).get('code')}")
                return response['result']
            try:
                invoke('provider.failover.configure', {'enabled': True, 'order': [backup['id']],
                       'circuit_breaker': {'failure_threshold': 1, 'cooldown_seconds': 300}})
                results = []
                for prompt in ('请只回答 FAILOVER_OK，不调用任何工具。', '继续只回答 FAILOVER_OK，不调用任何工具。'):
                    response = invoke('runtime.chat', {'project': project, 'message': prompt}, timeout=240)
                    assert response['status'] == 'completed' and response['llm_used'], response.get('status')
                    results.append(response)
                assert results[0]['session_id'] == results[1]['session_id']
                status = invoke('runtime.status', {'project': project})
                state = invoke('provider.status')
                assert state['active_provider'] == primary['id'], 'failover rewrote configured primary'
                health = {p['id']: p for p in status['providers']}
                assert health[primary['id']]['breaker_state'] == 'open', health[primary['id']]
                assert health[backup['id']]['breaker_state'] == 'closed'
                failed = [e for e in client.events if e.get('event') == 'provider_route_failed']
                skipped = [e for e in client.events if e.get('event') == 'provider_route_skipped']
                selected_events = [e for e in client.events if e.get('event') == 'provider_route_selected']
                assert any(e.get('provider_id') == primary['id'] for e in failed), 'failure never reached frontend transport'
                assert any(e.get('provider_id') == primary['id'] for e in skipped), 'open circuit was not surfaced'
                assert any(e.get('provider_id') == backup['id'] for e in selected_events), 'backup selection was not surfaced'
                assert 'FAILOVER_OK' in json.dumps(status.get('main_conversation', [])), 'answer not persisted'
                request_events = [e for e in client.events if e.get('event') == 'provider_request_started']
                assert len(request_events) == 3, len(request_events)
                activated = [e for e in client.events if e.get('event') == 'provider_route_activated']
                assert [e.get('route_changed') for e in activated] == [True, False], activated
                if not real:
                    assert calls == ['main', 'backup', 'backup'], calls
                result = {'passed': True, 'kind': 'real_api' if real else 'local_http',
                          'project': project, 'backup_model': backup['model_id'],
                          'primary_failures': [e.get('reason') for e in failed],
                          'physical_requests': len(request_events), 'same_session': True,
                          'primary_open': True, 'backup_closed': True, 'events_reached_frontend_transport': True}
                if regressions:
                    if not real:
                        raise ValueError('Identity/search regressions require --real')
                    identity = invoke('runtime.chat', {'project': project,
                        'message': '你现在运行的具体模型是什么？请只回答模型 ID。'}, timeout=240)
                    answer = identity.get('response', '')
                    assert backup['model_id'].lower() in answer.lower(), answer
                    assert 'gpt-4o' not in answer.lower(), answer
                    search = invoke('runtime.chat', {'project': project, 'message':
                        '请实际调用 web_search 搜索 GitHub BurntSushi ripgrep releases，给出发布页面链接。'
                        '保持现有 auto 搜索配置，不修改配置，不使用 web_fetch，不回答记忆中的结果。'}, timeout=240)
                    result['identity_answer'] = answer
                    result['search_status'] = search.get('status')
                    result['search_answer'] = search.get('response', '')
                    search_events = [e for e in client.events if e.get('event') == 'tool_result' and e.get('tool_name') == 'web_search']
                    result['search_tool_events'] = len(search_events)
                    result['search_errors'] = [e.get('error') for e in search_events if e.get('is_error')]
                    evidence = []
                    for event in search_events:
                        detail = invoke('runtime.trace', {'project': project, 'action': 'detail', 'ref': event['detail_ref']})['detail']
                        data = detail.get('data') or {}
                        evidence.append({key: data.get(key) for key in ('provider', 'provider_id', 'count', 'sources')})
                    result['search_evidence'] = evidence
                    result['route_change_notices'] = sum(e.get('event') == 'provider_route_activated' and e.get('route_changed') is True for e in client.events)
                    result['total_physical_requests'] = sum(e.get('event') == 'provider_request_started' for e in client.events)
                    assert search.get('status') == 'completed', search.get('status')
                    assert search_events and not result['search_errors'], result
                    assert any(item.get('provider_id') == backup['id'] and item.get('count', 0) > 0 for item in evidence), evidence
                    assert result['route_change_notices'] == 1, result
            finally:
                client.close()
        unchanged = (original_path.read_bytes() if original_path.exists() else b'') == original_bytes
        assert unchanged, 'installed provider configuration changed'
        result['installed_config_unchanged'] = unchanged
        return result
    finally:
        http.shutdown()
        http.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', type=Path, default=Path(sys.executable))
    parser.add_argument('--source', action='store_true')
    parser.add_argument('--real', action='store_true', help='Calls the configured backup API; uses its encrypted credential')
    parser.add_argument('--project', default='gitgo')
    parser.add_argument('--backup-id', default='')
    parser.add_argument('--regressions', action='store_true', help='Verify effective model identity and actual native web_search')
    args = parser.parse_args()
    print(json.dumps(check(args.host, source=args.source, real=args.real, project=args.project,
                           backup_id=args.backup_id, regressions=args.regressions), ensure_ascii=False, indent=2))
