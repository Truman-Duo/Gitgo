"""Replay a saved short delivery through the real assessor without live mutation."""
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.llm_config import LLMConfigManager
from backend.core.loop.alignment_runtime import AlignmentRuntime, assess_with_provider
from backend.core.loop.llm import LLMProvider
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession

profile = Path(sys.argv[1]).resolve()
installed_path = LLMConfigManager._config_path()
before = installed_path.read_bytes()
provider = next(p for p in LLMConfigManager.load()['providers'] if 'deepseek' in p.get('model_id', '').lower() and p.get('api_key'))
client = LLMProvider(provider['base_url'], provider['api_key'], provider['model_id'],
                     protocol=provider['protocol'], capabilities=provider.get('capabilities'))
for db in (profile / 'state').glob('projects/*/state.sqlite3'):
    cas = db.parent / 'cas'
    def blob(ref):
        token = str(ref or '').split(':')[-1]
        path = cas / token[:2] / token[2:]
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
    connection = sqlite3.connect('file:' + db.as_posix() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute('SELECT p.*, s.metadata_ref, t.outcome_ref FROM session_processes p JOIN sessions s ON s.session_id=p.session_id JOIN tasks t ON t.task_id=p.task_id').fetchall()
    connection.close()
    for row in rows:
        state = blob(row['runtime_state_ref']) or {}
        metadata = blob(row['metadata_ref']) or {}
        outcome = blob(row['outcome_ref']) or {}
        candidate = outcome.get('response') or ''
        process = AgentProcess(process_id=row['process_id'], active_task_id=row['task_id'], role=row['role'],
            ring_level=RingLevel.RING_3, task_kind='answer', task_description=state.get('task_description', ''),
            context_snapshot=state.get('context_snapshot') or {},
            session=AgentSession(host_ledger=blob(metadata.get('host_ledger_ref')) or []))
        runtime = AlignmentRuntime(process)
        payload = runtime.payload(candidate=candidate)
        assessment = assess_with_provider(client, payload, timeout=90)
        result = runtime.apply(assessment, payload)
        passed = result['status'] == 'repair'
        record = {'candidate_chars': len(candidate), 'status': result['status'], 'delivery_check': assessment.get('delivery_check'),
            'passed': passed, 'installed_config_unchanged': before == installed_path.read_bytes()}
        print(json.dumps(record, ensure_ascii=True), flush=True)
        if not passed:
            sys.exit(1)
