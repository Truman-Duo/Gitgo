"""Read existing release observations without a writer, account, or network."""
from .state import NAMESPACE, head_key, decode, find_item, PublishError, digest
from .release_journal import control_projection
from backend.core.storage.runtime import read_existing_state_blob
import json


def release_status(project, item_id, *, job_id="", after_sequence=0, limit=50):
    if (type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 100):
        raise PublishError("PUBLISH_RELEASE_CURSOR_INVALID")
    def read(raw, project_id, owned):
        ledger = decode(raw, project.workspace_path, project_id, owned)
        item = find_item(ledger, item_id)
        controls = [item.get("release_controller", {}), *item.get("release_controller_history", [])]
        selected, job = None, None
        for control in controls:
            if not control:
                continue
            candidates = [control.get("job", {}), *control.get("history", [])]
            match = next((value for value in candidates if value and value["id"] == job_id), None) if job_id else control.get("job")
            if match or not job_id and control is controls[0]:
                selected, job = control, match
                break
        if selected is None:
            raise PublishError("PUBLISH_RELEASE_JOB_CHANGED" if job_id else "PUBLISH_RELEASE_PLAN_REQUIRED")
        plan = json.loads(owned(selected["plan"]["blob_ref"]))
        if digest(plan) != selected["plan"]["binding"] or plan.get("item_id") != item_id:
            raise PublishError("PUBLISH_RELEASE_STATE_UNAVAILABLE")
        records = (job or {}).get("records", [])
        if after_sequence > len(records):
            raise PublishError("PUBLISH_RELEASE_CURSOR_INVALID")
        events = [{"sequence": index + 1, **json.loads(owned(pointer["blob_ref"]))}
                  for index, pointer in enumerate(records[after_sequence:after_sequence + limit], start=after_sequence)]
        next_sequence = after_sequence + len(events)
        return {"item_id": item_id, "revision": item["revision"], "recorded_only": True,
                "control": control_projection({**selected, "job": job or {}}),
                "plan": {key: plan[key] for key in ("recipe", "target", "body", "supported_phases", "capabilities")}
                        | {key:plan[key] for key in ("source_oid", "tree_oid", "notes", "ci_artifacts", "delivery_recipe") if key in plan}
                        | ({"native":{key:plan["native"][key] for key in ("channel","sequence","targets","upgrade_route")}
                            | {"signing_key_id":plan["native"]["signing"]["key_id"]}} if plan.get("native") else {}),
                "events": events, "next_sequence": next_sequence, "has_more": next_sequence < len(records),
                "stale": plan["item_binding"] != item["binding"]}
    result = read_existing_state_blob(project.workspace_path, NAMESPACE, head_key(project.workspace_path), decoder=read)
    if result is None:
        raise PublishError("PUBLISH_RELEASE_PLAN_REQUIRED")
    return {**result["payload"], "state_ref": result["reference"]}
