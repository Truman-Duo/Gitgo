"""Explicit shared metadata, separate from code refs and local execution state."""
import hashlib
import json
from copy import deepcopy

from . import formal, preview
from .remote_git import RemoteGit
from .state import PublishError, digest, encoded, find_item

REF = "refs/heads/gitgo/publish-metadata-v1"
SCHEMA = "gitgo.publish.shared.v1"
NAMESPACE = "governance.publish.shared.v1"
MAX_BYTES = 1024 * 1024


def public_item(item):
    # An explicit allowlist: never copy the ledger, plan, paths, logs or grants.
    sources = item["plan"].get("contributions") or [item["candidate"]]
    return {"id": item["id"], "binding": item["binding"], "title": item["title"], "owner": item["owner"],
        "rework": item["rework"], "sources": [{key: source[key] for key in ("role", "oid", "base_oid", "tree_oid")} for source in sources],
        "reviews": [{key: row[key] for key in ("id", "binding", "decision", "note", "owner")} for row in item.get("reviews", [])],
        "evidence": [{key: row[key] for key in ("id", "binding", "kind", "outcome")} | {"digest": digest(row)} for row in item["evidence"]],
        "result_oids": [row["result_oid"] for row in item["operations"] if row.get("kind") == "formal" and row.get("state") == "observed" and row.get("result_oid")],
        "task_links": [{key: row[key] for key in ("id", "process_id", "task_id", "linked_task_id", "parent_process_id", "parent_task_id", "decision_id", "snapshot_id", "anchor_id", "note") if key in row}
                       for row in item.get("task_links", [])],
        "branch_retention": [{key: row[key] for key in ("id", "kind", "ref", "oid", "owner", "reason") if key in row}
                             for row in item.get("branches", {}).get("retention", [])]}


def observation_key(workspace, item_id, observation_id):
    from .state import identity, identifier
    return digest(identity(workspace)) + "/" + identifier(item_id) + "/" + identifier(observation_id)


def valid_public_item(row):
    scalars = {"id", "binding", "title", "owner", "rework"}
    tables = {"sources": {"role", "oid", "base_oid", "tree_oid"},
        "reviews": {"id", "binding", "decision", "note", "owner"},
        "evidence": {"id", "binding", "kind", "outcome", "digest"},
        "task_links": {"id", "process_id", "task_id", "linked_task_id", "parent_process_id", "parent_task_id", "decision_id", "snapshot_id", "anchor_id", "note"},
        "branch_retention": {"id", "kind", "ref", "oid", "owner", "reason"}}
    if (not isinstance(row, dict) or not (scalars | {"sources", "reviews", "evidence", "result_oids"}) <= set(row)
            or set(row) - scalars - set(tables) - {"result_oids"}
            or any(not isinstance(row.get(key), str) or len(row[key]) > 4000 for key in scalars)):
        return False
    for name, keys in tables.items():
        values = row.get(name, [])
        if (not isinstance(values, list) or len(values) > 1000
                or any(not isinstance(value, dict) or set(value) - keys
                       or any(not isinstance(text, str) or len(text) > 4000 for text in value.values()) for value in values)):
            return False
    return (isinstance(row.get("result_oids"), list) and len(row["result_oids"]) <= 1000
            and all(isinstance(oid, str) and len(oid) in {40, 64} for oid in row["result_oids"]))


def read_document(git, oid):
    if not oid:
        return {"schema": SCHEMA, "items": {}}
    files = formal.tree_files(git, oid)
    if len(files) != 1 or files[0][0] != "publish.json" or len(files[0][3]) > MAX_BYTES:
        raise PublishError("PUBLISH_SHARED_SCHEMA_UNSUPPORTED")
    try:
        value = json.loads(files[0][3])
        if (not isinstance(value, dict) or set(value) != {"schema", "items"} or value["schema"] != SCHEMA
                or not isinstance(value["items"], dict) or len(value["items"]) > 1000
                or any(not valid_public_item(row) or key != row.get("id") for key, row in value["items"].items())):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise PublishError("PUBLISH_SHARED_SCHEMA_UNSUPPORTED") from exc
    return value


class SharingMixin:
    def _share(self, *, item_id, expected_revision, action, remote=""):
        from .service import now
        if action not in {"prepare", "refresh", "publish", "reconcile"}:
            raise PublishError("PUBLISH_INVALID_OPERATION")
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        state = item.get("sharing", {})
        if item["status"] in {"checking", "pausing", "paused", "executing"}:
            raise PublishError("PUBLISH_OPERATION_PENDING")
        if state.get("state") == "pending" and action != "reconcile":
            raise PublishError("PUBLISH_OPERATION_PENDING")
        if action in {"prepare", "refresh"}:
            transport = RemoteGit(self.workspace, remote)
            observed = transport.observe(REF)
            if observed["oid"]:
                self.effect_started = True
                transport.fetch(observed)
            document = read_document(transport.git, observed["oid"])
            if action == "refresh":
                document_ref = self.repo.storage.put_blob(encoded(document), media_type="application/json")
                key = observation_key(self.workspace, item_id, self.operation_id)
                if not self.repo.storage.commit_state_revision(NAMESPACE, key, document_ref, expected_ref=None, revision_key=key + "/observed"):
                    raise PublishError("PUBLISH_REVISION_CONFLICT")
                item.setdefault("shared_observations", []).append({"id": self.operation_id, "remote": remote,
                    "observation": observed, "document_ref": document_ref,
                    "items": len(document["items"]), "recorded_at": now()})
                if len(item["shared_observations"]) > 32:
                    raise PublishError("PUBLISH_CAPACITY_REACHED")
            else:
                shared = public_item(item)
                if len(document["items"]) >= 1000 and item_id not in document["items"]:
                    raise PublishError("PUBLISH_CAPACITY_REACHED")
                document["items"][item_id] = shared
                raw = encoded(document)
                if len(raw) > MAX_BYTES:
                    raise PublishError("PUBLISH_CAPACITY_REACHED")
                policy = formal.policy(self.project)
                if formal.privacy([], policy, raw.decode())["outcome"] != "passed":
                    raise PublishError("PUBLISH_SHARED_PRIVACY_BLOCKED")
                if state:
                    history = item.setdefault("sharing_history", [])
                    if len(history) >= 32:
                        raise PublishError("PUBLISH_CAPACITY_REACHED")
                    history.append(deepcopy(state))
                self.effect_started = True
                blob = transport.git.binary(["hash-object", "-w", "--stdin"], raw).decode().strip()
                tree = formal.write_tree(transport.git, [("publish.json", "100644", blob, raw)])
                result_oid = preview.commit(transport.git, tree, [observed["oid"]] if observed["oid"] else [], "Gitgo shared Publish metadata v1")
                item["sharing"] = {"state": "prepared", "binding": item["binding"], "policy_binding": digest(policy),
                    "projection_digest": digest(shared),
                    "observation": observed, "result_oid": result_oid, "tree_oid": tree, "sha256": hashlib.sha256(raw).hexdigest(),
                    "document_ref": self.repo.storage.put_blob(raw, media_type="application/json"), "preview": shared}
        else:
            if not state or state.get("binding") != item["binding"]:
                raise PublishError("PUBLISH_PLAN_STALE")
            observed = state["observation"]
            if remote and remote != observed["remote"]:
                raise PublishError("PUBLISH_PLAN_STALE")
            transport = RemoteGit(self.workspace, observed["remote"])
            if action == "publish":
                if (state["state"] != "prepared" or formal.policy_binding(self.project) != state["policy_binding"]
                        or digest(public_item(item)) != state.get("projection_digest")):
                    raise PublishError("PUBLISH_PLAN_STALE")
                current = transport.observe(REF, identity=observed["identity"])
                if current["oid"] != observed["oid"]:
                    raise PublishError("PUBLISH_SHARED_CONFLICT")
                operation = {"id": self.operation_id, "kind": "share", "state": "intent", "binding": item["binding"],
                    "actor": self.actor, "task_id": self.task_id, "result_oid": state["result_oid"],
                    "observation": deepcopy(observed), "created_at": now()}
                item["operations"].append(operation)
                state.update(state="pending", operation_id=self.operation_id)
                item["revision"] += 1
                self._save_item(ledger, item, ref)
                try:
                    transport.update(observed, state["result_oid"])
                except (OSError, RuntimeError):
                    return {"item": self._share_projection(item), "effect_state": "ambiguous", "reason": "PUBLISH_SHARED_RESULT_UNVERIFIED"}
            elif state["state"] != "pending":
                raise PublishError("PUBLISH_NO_PENDING_OPERATION")
            try:
                current = transport.observe(REF, identity=observed["identity"])
            except (OSError, RuntimeError):
                return {"item": self._share_projection(item), "effect_state": "ambiguous", "reason": "PUBLISH_SHARED_RESULT_UNVERIFIED"}
            ledger, ref = self.repo.load()
            item = find_item(ledger, item_id)
            state = item["sharing"]
            operation = next(row for row in item["operations"] if row["id"] == state["operation_id"])
            matched = current["oid"] == state["result_oid"]
            operation.update(state="observed" if matched else "unmatched", observed_oid=current["oid"], observed_at=now())
            state.update(state="published" if matched else "conflict", observed_oid=current["oid"])
        item["revision"] += 1
        return self._save_item(ledger, item, ref)

    @staticmethod
    def _share_projection(item):
        from .state import projection
        return projection(item, None)
