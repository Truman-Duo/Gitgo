"""Governance metadata on the existing project SQLite/CAS authority.

Legacy files are bounded import sources, never concurrent writer targets.
A session head and the monotone number allocator are distinct existing facts;
reserving a number can leave a gap when a later session update conflicts.
"""
import json
import hashlib
import os
from pathlib import Path
import uuid

from backend.core.storage import get_storage, peek_storage, resolve_existing_storage_paths
from backend.core.sandbox_read import ReadRoot

_NAMESPACE = "governance.sync.v1"
_HEAD = "session"
_COUNTER = "next_formal_number"


class GovernanceStateChanged(RuntimeError):
    code = "GOVERNANCE_STATE_CHANGED"
    def __init__(self):
        super().__init__(self.code)


class GovernanceStateUnavailable(RuntimeError):
    code = "GOVERNANCE_STATE_UNAVAILABLE"
    def __init__(self):
        super().__init__(self.code)


def _existing_runtime(workspace):
    bound = peek_storage(workspace)
    if bound is not None:
        return bound
    try:
        paths = resolve_existing_storage_paths(workspace)
    except FileNotFoundError:
        return None
    return get_storage(workspace) if paths and paths.state_db.is_file() else None


def _legacy_bytes(workspace, name):
    try:
        with ReadRoot(Path(workspace).absolute()) as root, root.open(".gitgo/" + name) as opened:
            return opened.read_bytes()
    except FileNotFoundError:
        return None


def _decode(runtime, reference):
    value = json.loads(runtime.read_blob(reference, repair=False))
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("project_id") != runtime.paths.project_id):
        raise GovernanceStateUnavailable()
    return value


def _validate(data):
    if not isinstance(data, dict) or not isinstance(data.get("formal_commits", []), list):
        raise GovernanceStateUnavailable()
    from .models import SessionStage
    if not isinstance(data.get("stage", "IDLE"), str) or data.get("stage", "IDLE") not in SessionStage.__members__:
        raise GovernanceStateUnavailable()
    for row in data.get("formal_commits", []):
        if (not isinstance(row, dict) or not isinstance(row.get("message"), str)
                or not isinstance(row.get("prefix"), str)
                or type(row.get("number")) is not int or row["number"] < 0):
            raise GovernanceStateUnavailable()
        if any(type(row[field]) is not bool for field in ("synced", "pushed", "is_incoming", "sources_cleared") if field in row):
            raise GovernanceStateUnavailable()
        indices = row.get("source_indices", [])
        if not isinstance(indices, (list, tuple, set)) or any(type(i) is not int or i < 0 for i in indices):
            raise GovernanceStateUnavailable()
    return data


def _session_key(workspace):
    path = os.path.normcase(str(Path(workspace).resolve()))
    return _HEAD + ":" + hashlib.sha256(path.encode("utf8")).hexdigest()


def read_state(workspace):
    """No identity/DB creation merely to discover an absent saved session."""
    try:
        runtime = _existing_runtime(workspace)
        reference = runtime.get_state_ref(_NAMESPACE, _session_key(workspace)) if runtime else None
        if reference:
            value = _decode(runtime, reference)
            if value.get("workspace") != str(Path(workspace).resolve()):
                raise GovernanceStateUnavailable()
            return _validate(value["state"]), reference
        raw = _legacy_bytes(workspace, "session.json")
        return (_validate(json.loads(raw)), None) if raw is not None else (None, None)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise GovernanceStateUnavailable() from exc


def save_state(workspace, data, *, expected_ref):
    runtime = get_storage(workspace)
    envelope = {"schema_version": 1, "project_id": runtime.paths.project_id,
                "workspace": str(Path(workspace).resolve()),
                "revision": uuid.uuid4().hex, "state": _validate(data)}
    reference = runtime.put_blob(json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf8"))
    if not runtime.compare_state_ref(_NAMESPACE, _session_key(workspace), reference, expected_ref=expected_ref):
        raise GovernanceStateChanged()
    digest = reference.removeprefix("sha256:")
    return reference, runtime.paths.cas_dir / digest[:2] / digest[2:]


def _counter(runtime, workspace):
    reference = runtime.get_state_ref(_NAMESPACE, _COUNTER) if runtime else None
    if reference:
        try:
            value = _decode(runtime, reference).get("next_number")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GovernanceStateUnavailable() from exc
        if type(value) is not int or value < 0:
            raise GovernanceStateUnavailable()
        return value, reference
    raw = _legacy_bytes(workspace, "next_number")
    try:
        return (max(0, int(raw.decode("utf8").strip()) + 1) if raw is not None else 0), None
    except (UnicodeError, ValueError) as exc:
        raise GovernanceStateUnavailable() from exc


def peek_formal_number(workspace):
    runtime = _existing_runtime(workspace)
    number, _ = _counter(runtime, workspace)
    data, _ = read_state(workspace)
    return max(number, max((row["number"] + 1 for row in (data or {}).get("formal_commits", [])), default=0))


def reserve_formal_number(workspace, minimum):
    from backend.core.storage.maintenance import StorageLease
    runtime = get_storage(workspace)
    lock = runtime.paths.project_root / "governance-number-lock"
    lock.mkdir(exist_ok=True)
    with StorageLease(lock, exclusive=True):
        current, reference = _counter(runtime, workspace)
        number = max(int(minimum), current, peek_formal_number(workspace))
        value = {"schema_version": 1, "project_id": runtime.paths.project_id, "next_number": number + 1}
        next_ref = runtime.put_blob(json.dumps(value, sort_keys=True).encode("utf8"))
        if not runtime.compare_state_ref(_NAMESPACE, _COUNTER, next_ref, expected_ref=reference):
            raise GovernanceStateChanged()
        return number
