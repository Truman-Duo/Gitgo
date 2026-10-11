"""Read-only timing breakdown, without printing provider headers or prompts."""
import json
import sqlite3
import sys
from pathlib import Path

profile = Path(sys.argv[1]).resolve()
for db in (profile / 'state').glob('projects/*/observability.sqlite3'):
    cas = db.parent / 'cas'
    def blob(ref):
        digest = str(ref or '').split(':')[-1]
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            return {}
        path = cas / digest[:2] / digest[2:]
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    connection = sqlite3.connect('file:' + db.as_posix() + '?mode=ro', uri=True)
    first = set()
    for timestamp, raw in connection.execute('SELECT occurred_at, record_json FROM trace_events ORDER BY rowid'):
        event = json.loads(raw)
        name = str(event.get('event', ''))
        if name in {'text_delta', 'reasoning_delta'}:
            key = event.get('trace_id'), name
            if key in first:
                continue
            first.add(key)
        if name.startswith(('provider_', 'alignment_')) or name in {
                'task_admitted', 'agent_complete', 'text_delta', 'reasoning_delta', 'completion_gate'}:
            projected = {k:v for k,v in event.items() if k in {
                'event', 'trace_id', 'seq', 'timestamp', 'stage', 'status', 'duration_ms',
                'agent_elapsed_ms', 'end_to_end_elapsed_ms', 'provider_id', 'model', 'attempt',
                'purpose', 'usage', 'reason', 'public_chars', 'reasoning_chars', 'tool_calls'}}
            projected['stored_at'] = timestamp
            detail = blob(event.get('detail_ref'))
            if isinstance(detail, dict):
                projected['detail'] = {k:v for k,v in detail.items() if k in {
                    'instruction', 'status', 'request_frame', 'reason', 'duration_ms', 'usage'}}
            print(json.dumps(projected, ensure_ascii=True))
    connection.close()
