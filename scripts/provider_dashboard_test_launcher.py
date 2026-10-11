"""Repeatable formal Dashboard test using the installed model as backup.

Reuses the terminal-test coordinator, discovered terminal selector, encrypted
provider store and detached cleanup keeper. The user configures the ordered
backup through the actual /config Providers UI. No test RPC renderer is used.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.llm_config import LLMConfigManager
from scripts.terminal_test_launcher import launch


def initialize(root: Path, _session: dict) -> None:
    original = LLMConfigManager.load()
    backup = next((p for p in original['providers'] if p['id'] == original['active_provider']), None)
    if backup is None or not backup.get('api_key'):
        raise RuntimeError('An installed remote provider with a usable credential is required')
    primary = {'id': 'invalid-openai-acceptance', 'name': 'Invalid OpenAI acceptance',
               'base_url': 'https://api.openai.com/v1', 'api_key': 'sk-invalid-test',
               'model_id': 'gpt-4o-mini', 'protocol': 'openai_chat', 'context_window': 128000,
               'max_output_tokens': 1024, 'kind': 'remote', 'auth_mode': 'api_key'}
    saved = {key: os.environ.get(key) for key in ('GITGO_LLM_CONFIG_PATH', 'GITGO_LLM_SECRET_PATH')}
    try:
        os.environ['GITGO_LLM_CONFIG_PATH'] = str(root / 'llm_config.json')
        os.environ['GITGO_LLM_SECRET_PATH'] = str(root / 'provider_secrets.json')
        LLMConfigManager.save({'providers': [primary, copy.deepcopy(backup)],
            'active_provider': primary['id'], 'failover_enabled': False, 'failover_order': [],
            'circuit_breaker': {'failure_threshold': 1, 'cooldown_seconds': 300}})
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print('[gitgo-test] Invalid OpenAI is first; installed model is available as backup.', flush=True)
    print('[gitgo-test] Enter a project, /config -> Providers -> Enter opens two columns.', flush=True)
    print('[gitgo-test] Right -> Enter -> Right(on) -> Enter(save). Down -> Enter -> Right(priority 1) -> Enter(save).', flush=True)
    print('[gitgo-test] Exit /config and ask: Reply with FAILOVER_OK, without tools.', flush=True)
    print('[gitgo-test] Check one failure/switch, then repeated replies without skipped/switch notices.', flush=True)
    print('[gitgo-test] Ask its model identity, then ask it to web_search GitHub ripgrep release and cite sources.', flush=True)


if __name__ == '__main__':
    try:
        for key in ('GITGO_LLM_BASE_URL', 'GITGO_LLM_API_KEY', 'GITGO_LLM_MODEL', 'GITGO_LLM_PROTOCOL'):
            os.environ.pop(key, None)
        sys.exit(launch(profile_initializer=initialize))
    except Exception as exc:
        print(f'[gitgo-test] {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
