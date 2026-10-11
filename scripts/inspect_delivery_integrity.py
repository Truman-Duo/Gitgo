"""Read-only lengths and public delivery events from an isolated profile."""
import json
import sqlite3
import sys
from pathlib import Path

profile = Path(sys.argv[1]).resolve()
for db in (profile / 'state').glob('projects/*/state.sqlite3'):
    cas = db.parent / 'cas'
    def blob(ref):
        digest = str(ref or '').split(':')[-1]
        path = cas / digest[:2] / digest[2:]
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
    connection = sqlite3.connect('file:' + db.as_posix() + '?mode=ro', uri=True)
    for (ref,) in connection.execute('SELECT outcome_ref FROM tasks'):
        outcome = blob(ref) or {}
        response = str(outcome.get('response') or '')
        print(json.dumps({'outcome_chars': len(response), 'tail': response[-120:]}, ensure_ascii=True))
    for (ref,) in connection.execute('SELECT metadata_ref FROM sessions'):
        metadata = blob(ref) or {}
        for state_ref in (metadata.get('provider_state_refs') or {}).values():
            state = blob(state_ref) or {}
            for item in state.get('response_output_items', []):
                text = ''.join(p.get('text', '') for p in item.get('content', []) if p.get('type') == 'output_text')
                if text:
                    print(json.dumps({'artifact_chars': len(text), 'status': item.get('status'), 'tail': text[-100:]}, ensure_ascii=True))
    for (ref,) in connection.execute('SELECT content_ref FROM messages'):
        message = blob(ref) or {}
        text = str(message.get('content') or '')
        if message.get('role') == 'assistant':
            print(json.dumps({'message_chars': len(text), 'keys': list(message), 'tail': text[-100:]}, ensure_ascii=True))
    connection.close()
    trace = db.parent / 'observability.sqlite3'
    connection = sqlite3.connect('file:' + trace.as_posix() + '?mode=ro', uri=True)
    for (raw,) in connection.execute('SELECT record_json FROM trace_events ORDER BY rowid'):
        event = json.loads(raw)
        name = event.get('event', '')
        if any(word in name for word in ('provider', 'completion', 'complete', 'response')):
            detail = blob(event.get('detail_ref'))
            fields = {k:v for k,v in event.items() if k not in {'detail_ref', 'timestamp', 'trace_id', 'outcome'}}
            if isinstance(detail, dict):
                detail = {k:v for k,v in detail.items() if k in {'status', 'incomplete_details', 'error', 'reason'}}
            print(json.dumps({'event': name, 'fields': fields, 'detail': detail}, ensure_ascii=True))
    connection.close()
