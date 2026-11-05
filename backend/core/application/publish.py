"""Read-only Publish projection of Alpha3's existing governance records.

Legacy flags do not prove Git effects. Observation IDs are snapshot-scoped and
must never be accepted as identities for future Publish mutation operations.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from backend.core.sync_session.state import read_state_snapshot
from backend.core.publish.state import read_ledger, projection


class PublishQueryError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def snapshot_identity(project, state, state_ref, ledger_ref):
    identity = {"project": project.name, "workspace": str(Path(project.workspace_path).resolve()),
                "state": state, "reference": state_ref, "publish_reference": ledger_ref}
    return hashlib.sha256(_encoded(identity)).hexdigest()


def get_operation(project, item_id, operation_id):
    """Saved operation facts only; never creates a runtime or platform client."""
    from backend.core.publish.state import find_item, PublishError
    ledger, ref = read_ledger(project.workspace_path)
    item = find_item(ledger, item_id)
    operations = [*item["operations"], *item.get("distribution_history", [])]
    if item.get("distribution_operation"):
        operations.append(item["distribution_operation"])
    operation = next((row for row in operations if row["id"] == operation_id), None)
    if operation is None:
        raise PublishError("PUBLISH_OPERATION_NOT_FOUND")
    return {"item_id": item["id"], "revision": item["revision"], "operation": operation,
        "evidence": [e for e in projection(item, ref)["evidence"] if e["id"].startswith(operation["id"])], "state_ref": ref}


def list_items(project, *, limit=50, cursor=""):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise PublishQueryError("PUBLISH_INVALID_LIMIT")
    if not isinstance(cursor, str) or len(cursor) > 2048:
        raise PublishQueryError("PUBLISH_INVALID_CURSOR")
    state, state_ref = read_state_snapshot(project.workspace_path)
    records = (state or {}).get("formal_commits", [])
    ledger, ledger_ref = read_ledger(project.workspace_path)
    candidates = list((ledger or {}).get("items", {}).values())
    total = len(candidates) + len(records)
    snapshot = snapshot_identity(project, state, state_ref, ledger_ref)
    offset = 0
    if cursor:
        try:
            decoded = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
            if (not isinstance(decoded, dict) or set(decoded) != {"snapshot", "offset"}
                    or not isinstance(decoded["snapshot"], str) or type(decoded["offset"]) is not int
                    or decoded["offset"] < 0):
                raise ValueError
        except (ValueError, UnicodeError) as exc:
            raise PublishQueryError("PUBLISH_INVALID_CURSOR") from exc
        if decoded["snapshot"] != snapshot:
            raise PublishQueryError("PUBLISH_SNAPSHOT_CHANGED")
        offset = decoded["offset"]
        if offset > total:
            raise PublishQueryError("PUBLISH_INVALID_CURSOR")
    items = [projection(item, ledger_ref) for item in candidates[offset:offset + limit]]
    legacy_start = max(0, offset - len(candidates))
    legacy_end = max(0, offset + limit - len(candidates))
    for index, record in enumerate(records[legacy_start:legacy_end], start=legacy_start):
        from backend.core.publish.legacy import key
        imported = (ledger or {}).get("legacy_migrations", {}).get(key(record), {})
        items.append({
            "id": f"legacy:{snapshot}:{index}", "identity_kind": "snapshot_observation",
            "title": record["message"][:512], "label": f'{record["prefix"]}-{record["number"]}',
            "kind": "legacy_formal", "scope": "local", "owner": None,
            "candidate_oid": None, "target": None,
            "status": "needs_reconciliation", "current_node": "verify",
            "reason": "LEGACY_IMPORTED" if imported else "LEGACY_CANDIDATE_UNBOUND", "allowed_actions": [] if imported else ["migrate"],
            "recorded": {key: record.get(key, False) for key in ("synced", "pushed", "is_incoming", "sources_cleared")},
            "source": {"state_ref": state_ref, "legacy_index": index, "created_at": record.get("created_at", ""), "imported_item_id": imported.get("item_id")},
            "nodes": [
                {"id": "record", "status": "recorded", "reason": "LEGACY_RECORD_ONLY"},
                {"id": "verify", "status": "needs_reconciliation", "reason": "LEGACY_CANDIDATE_UNBOUND"},
                {"id": "integrate", "status": "unverified", "reason": "RESULT_OID_UNRECORDED"},
                {"id": "push", "status": "unverified", "reason": "REMOTE_RESULT_UNVERIFIED"},
            ],
        })
    end = offset + len(items)
    next_cursor = base64.urlsafe_b64encode(_encoded({"snapshot": snapshot, "offset": end})).decode("ascii") if end < total else None
    return {"schema_version": 1, "project": project.name, "snapshot_id": snapshot,
            "source": "governance_snapshot", "scope": "local", "items": items,
            "next_cursor": next_cursor, "total": total}
