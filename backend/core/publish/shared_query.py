"""Paginate one explicit remote observation without network or writer access."""
import base64
import json

from backend.core.storage.runtime import read_existing_state_blob
from .sharing import NAMESPACE, observation_key
from .state import PublishError, encoded, find_item, read_ledger


def shared_items(project, *, item_id, observation_id="", limit=20, cursor=""):
    if type(limit) is not int or not 1 <= limit <= 50 or not isinstance(cursor, str) or len(cursor) > 2048:
        raise PublishError("PUBLISH_INVALID_LIMIT")
    ledger, _ = read_ledger(project.workspace_path)
    item = find_item(ledger, item_id)
    observations = item.get("shared_observations", [])
    observation = next((o for o in reversed(observations) if not observation_id or o["id"] == observation_id), None)
    if observation is None:
        raise PublishError("PUBLISH_SHARED_REFRESH_REQUIRED")
    saved = read_existing_state_blob(project.workspace_path, NAMESPACE, observation_key(project.workspace_path, item_id, observation["id"]))
    if saved is None or saved["reference"] != observation["document_ref"]:
        raise PublishError("PUBLISH_STATE_UNAVAILABLE")
    snapshot = saved["reference"]
    offset = 0
    if cursor:
        try:
            value = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
            if set(value) != {"snapshot", "offset"} or type(value["offset"]) is not int or value["offset"] < 0:
                raise ValueError()
            if value["snapshot"] != snapshot:
                raise PublishError("PUBLISH_SNAPSHOT_CHANGED")
            offset = value["offset"]
        except (ValueError, UnicodeError, TypeError) as exc:
            if isinstance(exc, PublishError):
                raise
            raise PublishError("PUBLISH_INVALID_CURSOR") from exc
    rows = list(json.loads(saved["payload"])["items"].values())
    if offset > len(rows):
        raise PublishError("PUBLISH_INVALID_CURSOR")
    end = min(offset + limit, len(rows))
    return {"schema_version": 1, "item_id": item_id, "observation_id": observation["id"], "snapshot_id": snapshot,
        "remote": observation["remote"], "observed_oid": observation["observation"]["oid"], "total": len(rows),
        "items": rows[offset:end], "scope": "shared_observation", "read_only": True,
        "next_cursor": base64.urlsafe_b64encode(encoded({"snapshot": snapshot, "offset": end})).decode() if end < len(rows) else None}
