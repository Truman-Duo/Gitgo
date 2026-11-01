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
    if sys.platform == "win32":
        return {"platform": "win32", "name": "Local\\Gitgo.Publish." + recovery_id}
    if sys.platform == "linux":
        root = os.environ.get("GITGO_SANDBOX_CGROUP_ROOT", "")
        return {"platform": "linux", "root": str(Path(root).resolve()) if root else ""}
    return {"platform": sys.platform}


def retire_native(recovery_id, location):
    """Only called while holding the producer's exclusive, noninherited lease."""
    if location != resource_location(recovery_id):
        raise PublishError("PUBLISH_RUNTIME_ENVIRONMENT_CHANGED")
    if sys.platform == "win32":
        import ctypes as C
        from ctypes import wintypes as W
        from backend.core.sandbox_windows import WindowsApi, NativeJobHandle, _PENDING_JOBS
        for job in tuple(_PENDING_JOBS.values()):
            if getattr(job, "recovery_id", "") == recovery_id and not job.close()["complete"]:
                return False
        api = WindowsApi()
        function = api.kernel.OpenJobObjectW
        function.argtypes, function.restype = [W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE
        C.set_last_error(0)
        handle = function(0x0004 | 0x0008, False, location["name"])
        if not handle:
            if C.get_last_error() == 2:
                return True  # Named kernel boundary no longer exists.
            raise C.WinError(C.get_last_error())
        job = NativeJobHandle(handle, api)
        job.recovery_id = recovery_id
        return job.close()["complete"] is True
    if sys.platform == "linux":
        from backend.core.sandbox_linux import LinuxCgroup, _PENDING_CGROUPS
        root = Path(location["root"]).resolve(strict=True)
        if not root.is_relative_to(Path("/sys/fs/cgroup")) or root == Path("/sys/fs/cgroup"):
            raise PublishError("PUBLISH_RUNTIME_ENVIRONMENT_CHANGED")
        path = root / ("gitgo-" + recovery_id)
        for group in tuple(_PENDING_CGROUPS.values()):
            if group.path == path:
                return group.close()["complete"] is True
        if not path.exists():
            return True
        if path.is_symlink() or path.resolve().parent != root:
            raise PublishError("PUBLISH_RUNTIME_RECORD_UNAVAILABLE")
        group = LinuxCgroup.__new__(LinuxCgroup)
        group.path, group.cleanup = path, {"complete": False, "mode": "linux-cgroup", "issues": []}
        return group.close()["complete"] is True
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
