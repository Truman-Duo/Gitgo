"""Durable observations for Publish checks; no launch authority or test replay."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys

from backend.core.publish.state import PublishError, digest, encoded, identifier

NAMESPACE = "governance.publish.runtime.v1"


@contextmanager
def item_lease(storage, item_id):
    from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive
    path = storage.paths.project_root / "publish-locks" / identifier(item_id)
    path.mkdir(parents=True, exist_ok=True)
    try:
        lease = StorageLease(path, exclusive=True)
    except StorageMaintenanceActive as exc:
        raise PublishError("PUBLISH_OPERATION_RUNNING") from exc
    with lease:
        yield


def save(storage, key, record):
    from backend.core.storage.redaction import redact_for_persistence
    raw = encoded(redact_for_persistence(record))
    if len(raw) > 512 * 1024:
        raise PublishError("PUBLISH_RUNTIME_RECORD_LIMIT")
    ref = storage.put_blob(raw, media_type="application/json")
    storage.put_state_ref(NAMESPACE, key, ref)


def read(storage, operation):
    key = operation.get("runtime_key")
    if not key or key != "check/" + identifier(operation["id"]):
        raise PublishError("PUBLISH_RUNTIME_RECORD_UNAVAILABLE")
    ref = storage.get_state_ref(NAMESPACE, key)
    if not ref:
        raise PublishError("PUBLISH_RUNTIME_RECORD_UNAVAILABLE")
    record = json.loads(storage.read_blob(ref))
    if (record.get("version") != 1 or record.get("operation_id") != operation["id"]
            or record.get("binding") != operation["binding"]
            or record.get("owner") != operation.get("runtime_owner")):
        raise PublishError("PUBLISH_RUNTIME_RECORD_UNAVAILABLE")
    return record


def resource_location(recovery_id):
    from backend.core.native_resources import recovery_location
    return recovery_location(recovery_id)


def retire_native(recovery_id, location):
    """Only called while holding the producer's exclusive, noninherited lease."""
    if location != resource_location(recovery_id):
        raise PublishError("PUBLISH_RUNTIME_ENVIRONMENT_CHANGED")
    if sys.platform == "win32":
        from backend.core.windows_sandbox_native import recover
        return recover(recovery_id)
    if sys.platform == "linux":
        from backend.core.sandbox_linux import recover_cgroup
        return recover_cgroup(location["root"], recovery_id)
    raise PublishError("PUBLISH_RUNTIME_RECOVERY_UNAVAILABLE")


def discard(storage, value, directory):
    from .workspace_candidate_runtime import WorkspaceCandidateRuntime
    root = (storage.paths.project_root / directory).resolve()
    stage = Path(value)
    if stage.parent != root:
        raise PublishError("PUBLISH_RUNTIME_RECORD_UNAVAILABLE")
    if stage.exists() or stage.is_symlink():
        WorkspaceCandidateRuntime._discard_stage(stage, root)


def recover_check(storage, operation):
    """Retire exact owned resources and return saved results, never infer a pass."""
    record = read(storage, operation)
    for index, producer in enumerate(record["producers"]):
        expected_id = digest({"operation": operation["id"], "producer": index})[:32]
        if producer["recovery_id"] != expected_id:
            raise PublishError("PUBLISH_RUNTIME_RECORD_UNAVAILABLE")
        stage = Path(producer["stage"])
        if stage.parent != (storage.paths.project_root / "c").resolve():
            raise PublishError("PUBLISH_RUNTIME_RECORD_UNAVAILABLE")
        if producer["phase"] != "retired":
            if producer["phase"] != "allocated" and not retire_native(expected_id, producer["location"]):
                raise PublishError("SANDBOX_CLEANUP_PENDING")
            if sys.platform == "win32":
                from backend.core.sandbox_access import revoke_record
                from backend.core.sandbox_access_records import MAX_RECORD_BYTES
                journal = storage.paths.project_root / "sandbox_access" / (stage.name + ".json")
                if journal.exists():
                    if journal.resolve().parent != journal.parent.resolve() or journal.is_symlink():
                        raise PublishError("PUBLISH_RUNTIME_RECORD_UNAVAILABLE")
                    with journal.open("rb") as stream:
                        raw = stream.read(MAX_RECORD_BYTES + 1)
                    payload = json.loads(raw) if len(raw) <= MAX_RECORD_BYTES else {}
                    if (payload.get("version") != 1 or payload.get("kind") != "scoped_workspace_access"
                            or payload.get("workspace") != str(stage)):
                        raise PublishError("PUBLISH_RUNTIME_RECORD_UNAVAILABLE")
                    # A revoked receipt can outlive the disposable workspace.
                    if payload.get("phase") != "revoked":
                        revoke_record(journal)
                elif producer["phase"] != "allocated":
                    # Launch follows a durable access receipt. A missing receipt
                    # is not proof that no runtime grants remain.
                    raise PublishError("PUBLISH_RUNTIME_RECORD_UNAVAILABLE")
            producer.update(phase="retired", recovery="native_boundary_retired")
            save(storage, operation["runtime_key"], record)
        discard(storage, str(stage), "c")
    if record.get("source_stage"):
        discard(storage, record["source_stage"], "publish-inputs")
    return record
