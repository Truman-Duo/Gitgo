"""Durable command admission. Unknown effects are never automatically replayed."""
from __future__ import annotations
import re
import sqlite3
from ..models import StorageCommitUnknown
from ..support import _utc_now
from ..transactions import authoritative_transaction


def _validate_identity(key, fingerprint):
    if (type(key) is not str or not 0 < len(key) <= 256 or '\0' in key
            or type(fingerprint) is not str or re.fullmatch(r'[0-9a-f]{64}', fingerprint) is None):
        raise ValueError("COMMAND_IDENTITY_INVALID")


def lookup_command(runtime, key: str, fingerprint: str) -> dict:
    """Use a fresh read-only snapshot, including after writer quarantine.

    No write reservation, CAS repair, migration, claim or event is performed.
    An absent snapshot cannot exclude a different writer's pending transaction.
    """
    _validate_identity(key, fingerprint)
    runtime._ensure_open()
    try:
        with runtime._committed_state_reader() as connection:
            row = connection.execute(
                'SELECT fingerprint,result_ref,deleted FROM command_receipts WHERE command_key=?',
                (key,)).fetchone()
    except (sqlite3.Error, OSError):
        return {'status': 'unknown'}
    if row is None:
        return {'status': 'absent'}
    if row[0] != fingerprint:
        return {'status': 'conflict'}
    return {'status': 'existing', 'result_ref': row[1], 'deleted': bool(row[2])}


def claim_command(runtime, key: str, fingerprint: str, *, process_id: str = "") -> dict:
    _validate_identity(key, fingerprint)
    runtime._ensure_open()
    existing = lookup_command(runtime, key, fingerprint)
    if existing['status'] == 'conflict':
        raise ValueError("COMMAND_IDENTITY_CONFLICT")
    if existing['status'] == 'unknown':
        raise StorageCommitUnknown('lookup')
    if existing['status'] == 'existing':
        result = {'admitted': False, 'result_ref': existing['result_ref'], 'deleted': existing['deleted']}
        if result['result_ref']:
            result['result'] = runtime._read_json_blob(result['result_ref'])
        return result
    runtime._reserve_authoritative_write(512)
    with runtime._state_lock:
        with authoritative_transaction(runtime._state):
            row = runtime._state.execute("SELECT fingerprint,result_ref,deleted FROM command_receipts WHERE command_key=?", (key,)).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("COMMAND_IDENTITY_CONFLICT")
                result = {"admitted": False, "result_ref": row["result_ref"], "deleted": bool(row["deleted"])}
            else:
                if runtime._state.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] >= 50_000:
                    raise ValueError("COMMAND_HISTORY_CAPACITY_REACHED")
                runtime._state.execute("INSERT INTO command_receipts(command_key,fingerprint,created_at,process_id) VALUES(?,?,?,?)",
                                       (key, fingerprint, _utc_now(), process_id))
                result = {"admitted": True, "result_ref": None}
    if result["result_ref"]:
        result["result"] = runtime._read_json_blob(result["result_ref"])
    return result


def complete_command(runtime, key: str, fingerprint: str, result: dict) -> None:
    _validate_identity(key, fingerprint)
    runtime._state.require_available()
    # Immutable CAS may precede the atomic result pointer. A crash before commit
    # leaves the command uncertain rather than granting a repeat of its effect.
    descriptor = runtime._write_blob_file(runtime._encoded_object(result), media_type="application/json")
    runtime._reserve_authoritative_write(512)
    with runtime._state_lock:
        with authoritative_transaction(runtime._state):
            process_id = str((result.get("result") or {}).get("process_id") or "") if isinstance(result.get("result"), dict) else ""
            updated = runtime._state.execute("""UPDATE command_receipts SET result_ref=?,
                process_id=CASE WHEN process_id='' THEN ? ELSE process_id END
                WHERE command_key=? AND fingerprint=? AND result_ref IS NULL AND deleted=0""",
                (descriptor["ref"], process_id, key, fingerprint)).rowcount
            if updated != 1:
                raise ValueError("COMMAND_COMPLETION_CONFLICT")
            runtime._register_object(runtime._state, descriptor)
            runtime._state.execute("""INSERT INTO object_refs(owner_type,owner_id,field_name,digest,created_at)
                VALUES('command_receipt',?,'result',?,?)""", (key, descriptor["digest"], _utc_now()))


def purge_process_receipts(connection, process_id):
    # Keep only identity tombstones so privacy deletion cannot re-enable replay.
    connection.execute("""DELETE FROM object_refs WHERE owner_type='command_receipt'
        AND owner_id IN (SELECT command_key FROM command_receipts WHERE process_id=?)""", (process_id,))
    connection.execute("UPDATE command_receipts SET result_ref=NULL,deleted=1 WHERE process_id=?", (process_id,))
