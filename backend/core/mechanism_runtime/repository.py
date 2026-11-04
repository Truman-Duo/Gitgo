"""Mechanism records in the existing project CAS and authoritative KV store.

Definitions have immutable revisions. An instance's state, in-flight journal
and selected revision share one CAS head; callbacks passed to mutate are pure.
No developer writes SQLite or a second session/permission database here.
"""
from __future__ import annotations

from copy import deepcopy
import json
from .contracts import bounded_json, encoded, digest, identifier

NAMESPACE = "harness_mechanisms.v2"


class Repository:
    def __init__(self, storage):
        self.storage = storage
        self.project_id = storage.paths.project_id

    def _read(self, key):
        reference = self.storage.get_state_ref(NAMESPACE, key)
        if reference is None:
            return None, None
        raw = self.storage.read_blob(reference)
        if "sha256:" + digest_bytes(raw) != reference:
            raise ValueError("MECHANISM_STATE_CORRUPT")
        value = json.loads(raw)
        if value.get("project_id") != self.project_id:
            raise ValueError("MECHANISM_PROJECT_MISMATCH")
        return value, reference

    def definition(self, name, revision=None):
        identifier(name)
        return self._read("definition/" + name + ("/head" if revision is None else "/revision/" + str(revision)))

    def definition_ref(self, reference):
        raw = self.storage.read_blob(reference)
        if "sha256:" + digest_bytes(raw) != reference:
            raise ValueError("MECHANISM_DEFINITION_CORRUPT")
        value = json.loads(raw)
        if value.get("project_id") != self.project_id or value.get("schema_version") != 2:
            raise ValueError("MECHANISM_DEFINITION_PROJECT_MISMATCH")
        saved, saved_ref = self.definition(value["id"], value["revision"])
        if saved_ref != reference or saved != value:
            raise ValueError("MECHANISM_IMMUTABLE_DEFINITION_REQUIRED")
        return value

    def publish(self, compiled, sources, *, expected_head):
        name = identifier(compiled["id"])
        current, head = self.definition(name)
        if head != expected_head:
            raise ValueError("MECHANISM_DEFINITION_REVISION_CONFLICT")
        value = {"schema_version": 2, "project_id": self.project_id, "id": name,
            "revision": (current or {}).get("revision", 0) + 1, "compiled": deepcopy(compiled),
            "sources": deepcopy(sources), "prior_ref": head}
        bounded_json(value, maximum=524288)
        reference = self.storage.put_blob(encoded(value), media_type="application/json")
        if not self.storage.commit_state_revision(NAMESPACE, "definition/" + name + "/head", reference,
                expected_ref=head, revision_key="definition/" + name + "/revision/" + str(value["revision"])):
            raise ValueError("MECHANISM_DEFINITION_REVISION_CONFLICT")
        return value, reference

    @staticmethod
    def instance_key(task_id, name):
        if not isinstance(task_id, str) or not task_id or len(task_id) > 180:
            raise ValueError("MECHANISM_TASK_ID_REQUIRED")
        return "instance/" + digest(task_id) + "/" + identifier(name)

    def instance(self, task_id, name):
        value, reference = self._read(self.instance_key(task_id, name))
        if value is not None and value.get("task_id") != task_id:
            raise ValueError("MECHANISM_TASK_MISMATCH")
        return value, reference

    def instance_record(self, task_id, name, definition, reference, *, policy, sources, constraints=()):
        if policy not in {"pinned", "follow_compatible"}:
            raise ValueError("MECHANISM_VERSION_POLICY_INVALID")
        if definition.get("project_id") != self.project_id:
            raise ValueError("MECHANISM_PROJECT_MISMATCH")
        # Sources are supplied by the application authority adapter, never
        # by a model's assertion that the user approved a configuration.
        if not sources:
            raise ValueError("MECHANISM_ACTIVATION_SOURCE_REQUIRED")
        value = {"schema_version": 2, "project_id": self.project_id, "task_id": task_id, "id": identifier(name),
            "definition_id": definition["id"], "definition_revision": definition["revision"],
            "desired_ref": reference, "effective_ref": None, "compiled": deepcopy(definition["compiled"]),
            "version_policy": policy, "phase": "saved", "revision": 1, "epoch": 0,
            "sources": deepcopy(sources), "required_constraints": list(constraints), "states": {}, "runs": {}, "active_run": None,
            "activation": None, "maintenance": {}}
        return value

    def create_instance(self, task_id, name, definition, reference, *, policy, sources, constraints=()):
        value = self.instance_record(task_id, name, definition, reference, policy=policy, sources=sources, constraints=constraints)
        ref = self.storage.put_blob(encoded(value), media_type="application/json")
        if not self.storage.compare_state_ref(NAMESPACE, self.instance_key(task_id, name), ref, expected_ref=None):
            raise ValueError("MECHANISM_INSTANCE_EXISTS")
        return value

    def mutate(self, task_id, name, change, *, expected_revision=None):
        for _ in range(8):
            current, head = self.instance(task_id, name)
            if current is None:
                raise ValueError("MECHANISM_INSTANCE_MISSING")
            if expected_revision is not None and current["revision"] != expected_revision:
                raise ValueError("MECHANISM_INSTANCE_REVISION_CONFLICT")
            candidate = deepcopy(current)
            result = change(candidate)
            if candidate == current:
                return deepcopy(result)
            # Existing project quotas still apply. A pending invocation is
            # durable before any effect, so a full store cannot cause replay.
            bounded_json(candidate, maximum=2 * 1024 * 1024)
            ref = self.storage.put_blob(encoded(candidate), media_type="application/json")
            if self.storage.compare_state_ref(NAMESPACE, self.instance_key(task_id, name), ref, expected_ref=head):
                return deepcopy(result)
        raise ValueError("MECHANISM_STATE_CONTENTION")

    def list_instances(self, task_id, *, after="", limit=50):
        prefix = "instance/" + digest(task_id) + "/"
        rows = self.storage.list_state_refs(NAMESPACE, prefix=prefix, after=after, limit=limit)
        return [self._read(row["key"])[0] for row in rows]

    def scan(self, prefix, *, after="", limit=100):
        rows = self.storage.list_state_refs(NAMESPACE, prefix=prefix, after=after, limit=limit)
        return [(row["key"], *self._read(row["key"])) for row in rows]

    def completed(self, task_id, name, event_key):
        return self._read("receipt/" + digest([task_id, name]) + "/" + event_key)[0]

    def archive_completed(self, task_id, name):
        current, _ = self.instance(task_id, name)
        if not current or len(current["runs"]) < 32:
            return
        archived = {}
        for event_key, run in current["runs"].items():
            if run["phase"] not in {"complete", "reconciled"} or current["active_run"] == event_key:
                continue
            key = "receipt/" + digest([task_id, name]) + "/" + event_key
            receipt = {"project_id": self.project_id, "task_id": task_id, "instance_id": name, "run": run}
            ref = self.storage.put_blob(encoded(receipt), media_type="application/json")
            if not self.storage.compare_state_ref(NAMESPACE, key, ref, expected_ref=None):
                if self._read(key)[0] != receipt:
                    raise ValueError("MECHANISM_RECEIPT_IDENTITY_CONFLICT")
            archived[event_key] = run
            if len(archived) >= 16:
                break
        def compact(value):
            for key, run in archived.items():
                if value["runs"].get(key) == run and value["active_run"] != key:
                    del value["runs"][key]
        if archived:
            self.mutate(task_id, name, compact)


def digest_bytes(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()
