"""One versioned Publish ledger in the existing project SQLite/CAS authority."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from backend.core.storage.runtime import read_existing_state_blob

NAMESPACE = "governance.publish.v1"
MAX_BYTES = 4_000_000
PAGE_BYTES = 512 * 1024


class PublishError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def identity(workspace):
    return str(Path(workspace).resolve())


def head_key(workspace):
    return "workspace/" + digest(identity(workspace)) + "/head"


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value):
        raise PublishError("PUBLISH_INVALID_ID")
    return value


def decode(raw, workspace, project_id, read_blob=None):
    value = json.loads(raw)
    if isinstance(value, dict) and value.get("schema_version") == 2:
        if (value.get("workspace") != identity(workspace) or value.get("project_id") != project_id
                or not isinstance(value.get("pages"), list) or not 1 <= len(value["pages"]) <= 65536
                or type(value.get("byte_length")) is not int
                or not 0 < value["byte_length"] <= len(value["pages"]) * PAGE_BYTES
                or read_blob is None):
            raise PublishError("PUBLISH_STATE_UNAVAILABLE")
        pages = []
        for reference in value["pages"]:
            if not isinstance(reference, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", reference):
                raise PublishError("PUBLISH_STATE_UNAVAILABLE")
            page = read_blob(reference)
            if not 0 < len(page) <= PAGE_BYTES or "sha256:" + hashlib.sha256(page).hexdigest() != reference:
                raise PublishError("PUBLISH_STATE_UNAVAILABLE")
            pages.append(page)
        payload = b"".join(pages)
        if len(payload) != value["byte_length"] or hashlib.sha256(payload).hexdigest() != value.get("payload_sha256"):
            raise PublishError("PUBLISH_STATE_UNAVAILABLE")
        value = json.loads(payload)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("workspace") != identity(workspace)
            or value.get("project_id") != project_id
            or not isinstance(value.get("items"), dict)):
        raise PublishError("PUBLISH_STATE_UNAVAILABLE")
    return value


def payload_references(value):
    """Typed Publish payload links, excluding user prose and Git ref names."""
    references, pending = set(), [value]
    fields = {"blob_ref", "document_ref", "plugin_snapshot_ref", "backup_ref"}
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key, entry in node.items():
                if key in fields and isinstance(entry, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", entry):
                    references.add(entry)
                elif isinstance(entry, (dict, list)):
                    pending.append(entry)
        elif isinstance(node, list):
            pending.extend(node)
    return references


def read_ledger(workspace):
    saved = read_existing_state_blob(workspace, NAMESPACE, head_key(workspace),
        decoder=lambda raw, project_id, read: decode(raw, workspace, project_id, read))
    if saved is None:
        return None, None
    return saved["payload"], saved["reference"]


class Repository:
    def __init__(self, workspace, storage):
        self.workspace, self.storage = workspace, storage

    def load(self):
        ref = self.storage.get_state_ref(NAMESPACE, head_key(self.workspace))
        if ref:
            raw = self.storage.read_blob(ref)
            if "sha256:" + hashlib.sha256(raw).hexdigest() != ref:
                raise PublishError("PUBLISH_STATE_UNAVAILABLE")
            return decode(raw, self.workspace, self.storage.paths.project_id, self.storage.read_blob), ref
        return {"schema_version": 1, "workspace": identity(self.workspace),
                "project_id": self.storage.paths.project_id, "items": {}}, None

    def save(self, ledger, expected):
        raw = encoded(ledger)
        if len(ledger["items"]) > 1000:
            raise PublishError("PUBLISH_CAPACITY_REACHED")
        related = tuple(sorted(payload_references(ledger)))
        if len(raw) > MAX_BYTES:
            # Archive immutable bodies; the small authoritative head no longer
            # competes with growing history for room to record a terminal state.
            # Existing project/CAS/disk quotas remain authoritative. A physical
            # write failure leaves the old intent intact, without replay.
            pages = tuple(self.storage.put_blob(raw[i:i + PAGE_BYTES]) for i in range(0, len(raw), PAGE_BYTES))
            related += pages
            raw = encoded({"schema_version": 2, "workspace": identity(self.workspace),
                "project_id": self.storage.paths.project_id, "pages": list(pages),
                "byte_length": len(raw), "payload_sha256": hashlib.sha256(raw).hexdigest()})
            if len(raw) > MAX_BYTES:
                raise PublishError("PUBLISH_CAPACITY_REACHED")
        ref = self.storage.put_blob(raw, media_type="application/json")
        if not self.storage.commit_state_revision(NAMESPACE, head_key(self.workspace), ref,
                expected_ref=expected, revision_key=head_key(self.workspace) + "/revision/" + ref,
                related_refs=related):
            raise PublishError("PUBLISH_REVISION_CONFLICT")
        return ref


def find_item(ledger, item_id, expected_revision=None):
    identifier(item_id)
    item = (ledger or {}).get("items", {}).get(item_id)
    if item is None:
        raise PublishError("PUBLISH_ITEM_NOT_FOUND")
    if expected_revision is not None and (type(expected_revision) is not int
            or item["revision"] != expected_revision):
        raise PublishError("PUBLISH_REVISION_CONFLICT")
    return item


def active_evidence(item):
    current = {}
    for evidence in item["evidence"]:
        if evidence["binding"] == item["binding"]:
            current[evidence["kind"]] = evidence
    return list(current.values())


def formal_ready(item):
    checks = {e["kind"]: e for e in active_evidence(item)}
    reviews = [r for r in item.get("reviews", []) if r["binding"] == item["binding"]]
    return (item["plan"].get("destination") == "formal" and item["status"] == "prepared"
            and (not reviews or reviews[-1]["decision"] == "accept")
            and all(checks.get(kind, {}).get("outcome") == "passed"
                    for kind in ("git_diff_check", "outbound_privacy", "project_test"))
            and (not item["plan"].get("plugins") or checks.get("commit_plugins", {}).get("outcome") == "passed")
            and (not item["plan"].get("environment_policy") or checks.get("environment", {}).get("outcome") == "passed"))


def projection(item, state_ref):
    active = active_evidence(item)
    failed = any(e["outcome"] == "failed" and e["kind"] in {
        "git_diff_check", "outbound_privacy", "project_test", "commit_plugins", "environment", "merge_preview"} for e in active)
    status = item["status"]
    formal = item["plan"].get("destination") == "formal"
    reason = ("ADOPTED_REWORK_REQUIRED" if status == "adopted" else
              "OPERATION_RECONCILIATION_REQUIRED" if status == "executing" else
              "CHECKS_STALE_PREPARE_REQUIRED" if not active else
              "CHECKS_FAILED_ADOPTION_AVAILABLE" if failed else "CANDIDATE_PREPARED")
    actions = ["reconcile"] if status == "executing" else (["adopt"] if active else []) + ["plan", "revise"] if status == "prepared" else ["revise"]
    if formal:
        reason = ("FORMAL_INTEGRATED" if status == "integrated" else "CHECK_OPERATION_INCOMPLETE" if status == "checking" else "OPERATION_RECONCILIATION_REQUIRED" if status == "executing"
                  else "FORMAL_CHECKS_FAILED" if failed else "FORMAL_READY" if formal_ready(item) else "FORMAL_TEST_REQUIRED")
        actions = (["verify", "revise"] + (["execute"] if formal_ready(item) else [])) if status == "prepared" else ["reconcile"] if status == "executing" else [] if status == "checking" else ["revise"]
        if any(e["outcome"] != "passed" for e in active if e["kind"] in {"git_diff_check", "outbound_privacy"}):
            actions = [action for action in actions if action != "verify"]
    if status == "prepared":
        actions.append("pause")
    elif status == "conflicted":
        reason, actions = "PUBLISH_MERGE_CONFLICT", ["revise"]
    elif status == "observed":
        reason, actions = "PUBLISH_LEGACY_HISTORY_OBSERVED", ["revise"]
    elif status == "checking":
        actions = ["pause", "reconcile"]
    elif status == "pausing":
        reason, actions = "PAUSE_PENDING", ["reconcile"]
    elif status == "paused":
        reason, actions = "PUBLISH_PAUSED", ["resume"]
    elif status == "failed":
        reason, actions = "PUBLISH_OPERATION_FAILED", ["revise"]
    from backend.core.loop.publish_queue import ACTIVE
    run = item.get("run", {})
    if run.get("state") in ACTIVE:
        reason, actions = "PUBLISH_RUN_" + run["state"].upper(), ["pause", "reconcile"]
    elif run.get("current"):
        reason, actions = "PUBLISH_RUN_RECONCILIATION_REQUIRED", ["reconcile"]
    elif status == "prepared":
        actions.append("run")
    if status in {"prepared", "adopted", "integrated", "conflicted"} and run.get("state") not in ACTIVE and not run.get("current"):
        actions.append("decide")
        if formal and status in {"prepared", "conflicted"}:
            actions.append("work")
    reviews = [r for r in item.get("reviews", []) if r["binding"] == item["binding"]]
    if status == "prepared" and reviews and reviews[-1]["decision"] in {"rework", "defer"} and run.get("state") not in ACTIVE:
        reason = "PUBLISH_REVIEW_" + reviews[-1]["decision"].upper()
    if status not in {"checking", "pausing", "paused", "executing", "observed"} and run.get("state") not in ACTIVE and not run.get("current"):
        actions.extend(["share", "collaborate"])
        shared = item.get("sharing", {})
        if shared.get("state") == "prepared" and shared.get("binding") == item["binding"]:
            actions.append("share_publish")
        drafts = item.get("collaboration", {}).get("drafts", [])
        if any(d.get("state") == "draft" and d.get("binding") == item["binding"] for d in drafts):
            actions.append("collaborate_send")
        if status == "integrated":
            actions.extend(["package", "export", "tag", "release", "import_package", "download"])
        actions.append("link_task")
        actions.append("branch")
        if (item.get("branches", {}).get("plan", {}).get("state") == "prepared"
                and item["branches"]["plan"].get("binding") == item["binding"]):
            actions.append("branch_apply")
    if item.get("sharing", {}).get("state") == "pending":
        reason, actions = "PUBLISH_SHARED_RESULT_UNVERIFIED", ["share_reconcile"]
    if any(d.get("state") in {"sending", "unknown"} for d in item.get("collaboration", {}).get("drafts", [])):
        reason, actions = "PUBLISH_COLLABORATION_RESULT_UNVERIFIED", ["collaborate_reconcile"]
    if item.get("distribution_operation", {}).get("state") in {"intent", "uncertain"}:
        reason, actions = "PUBLISH_DISTRIBUTION_PENDING", ["distribution_reconcile"]
        if item["distribution_operation"].get("kind") == "package" and not item["distribution_operation"].get("cancel_requested"):
            actions.append("pause")
    elif item.get("distribution_operation", {}).get("state") == "partial":
        actions.append("distribution_reconcile")
    if item.get("branches", {}).get("plan", {}).get("state") == "pending":
        reason, actions = "PUBLISH_BRANCH_RESULT_UNVERIFIED", ["branch_reconcile", "reconcile"]
    if status == "failed":
        reason, actions = "PUBLISH_OPERATION_FAILED", ["revise"]
    extra_nodes, current_node = [], "verify" if status in {"prepared", "checking", "paused", "pausing"} else "integrate"
    if item.get("collaboration"):
        unresolved = any(d.get("state") in {"sending", "unknown"} for d in item["collaboration"].get("drafts", []))
        extra_nodes.append({"id": "collaborate", "status": "needs_reconciliation" if unresolved else "recorded",
            "reason": "PUBLISH_COLLABORATION_RESULT_UNVERIFIED" if unresolved else "PUBLISH_COLLABORATION_RECORDED"})
        if unresolved:
            current_node = "collaborate"
    if status == "integrated" or item.get("distribution_operation"):
        state = item.get("distribution_operation", {}).get("state", "unverified")
        extra_nodes.append({"id": "distribute", "status": "recorded" if state == "observed" else "failed" if state == "failed" else "needs_reconciliation" if state in {"intent", "uncertain", "partial"} else "unverified",
            "reason": "PUBLISH_DISTRIBUTION_PENDING" if state in {"intent", "uncertain", "partial"} else "PUBLISH_DISTRIBUTION_RECORDED"})
        if state in {"intent", "uncertain", "partial"}:
            current_node = "distribute"
    if item.get("branches"):
        state = item["branches"].get("plan", {}).get("state", "recorded")
        extra_nodes.append({"id": "branches", "status": "needs_reconciliation" if state == "pending" else "failed" if state == "conflict" else "recorded",
            "reason": "PUBLISH_BRANCH_RESULT_UNVERIFIED" if state == "pending" else "PUBLISH_BRANCH_RECORDED"})
        if state == "pending":
            current_node = "branches"
    return {**item, "identity_kind": "stable_item", "kind": "candidate", "scope": "local",
        "evidence": [{**e, "current": any(e["id"] == a["id"] for a in active)} for e in item["evidence"]],
        "label": item["id"][:8], "candidate_oid": item["candidate"]["oid"],
        "target": item["plan"]["target_ref"], "current_node": current_node,
        "reason": reason, "formal_eligible": formal_ready(item),
        "allowed_actions": actions,
        "recorded": {"synced": False, "pushed": False, "is_incoming": item["candidate"]["role"] == "trial", "sources_cleared": False},
        "source": {"state_ref": state_ref, "created_at": item["created_at"]},
        "nodes": [
            {"id": "record", "status": "recorded", "reason": "FIXED_CANDIDATE"},
            {"id": "verify", "status": "stale" if not active else "failed" if failed else "passed" if formal and any(e["kind"] == "project_test" for e in active) else "recorded",
             "reason": reason if formal else "CHECKS_STALE_PREPARE_REQUIRED" if not active else "BOUNDED_CHECKS_ONLY"},
            {"id": "integrate", "status": status, "reason": reason},
            {"id": "push", "status": "unverified", "reason": "REMOTE_RESULT_UNVERIFIED" if status == "integrated" else "FORMAL_NOT_ACCEPTED"}, *extra_nodes]}
