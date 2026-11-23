import hashlib
import io
import pytest
from backend.core.storage import StorageRuntime
from backend.core.storage.models import StoragePolicy
from backend.core.publish.release_artifacts import ReleaseArtifacts, NAMESPACE
from backend.core.publish.state import PublishError
from backend.core.publish.release_engine import ReleaseCancelled


def test_large_release_content_uses_existing_small_cas_objects_and_exact_seekable_bytes(tmp_path_factory):
    workspace = tmp_path_factory / "workspace"; workspace.mkdir()
    policy = StoragePolicy(max_cas_object_bytes=1024)
    with StorageRuntime(workspace, state_home=tmp_path_factory / "state", policy=policy) as storage:
        content = bytes(range(256)) * 12
        artifacts = ReleaseArtifacts(storage, "operation")
        descriptor = artifacts.ingest((content[:1100], content[1100:]), size=len(content), sha256=hashlib.sha256(content).hexdigest())
        assert b"".join(artifacts.chunks(descriptor)) == content
        with artifacts.reader(descriptor) as reader:
            reader.seek(1000); assert reader.read(70) == content[1000:1070]
            reader.seek(-32, io.SEEK_END); assert reader.read() == content[-32:]
            reader.seek(0); assert reader.read(12) == content[:12]
        assert storage.list_state_refs(NAMESPACE, prefix="operation/")
        artifacts.close()
        assert storage.list_state_refs(NAMESPACE, prefix="operation/") == []


def test_bad_download_or_cancellation_never_becomes_a_verified_artifact(tmp_path_factory):
    workspace = tmp_path_factory / "workspace"; workspace.mkdir()
    with StorageRuntime(workspace, state_home=tmp_path_factory / "state") as storage:
        artifacts = ReleaseArtifacts(storage, "operation")
        with pytest.raises(PublishError, match="DIGEST_MISMATCH"):
            artifacts.ingest([b"wrong"], size=5, sha256="0"*64)
        artifacts.close()
        cancelled = ReleaseArtifacts(storage, "cancelled", cancelled=lambda: True)
        with pytest.raises(ReleaseCancelled): cancelled.ingest([b"body"], size=4)
        cancelled.close()


@pytest.mark.parametrize("unsafe", ["../escape", "A/file.bin", "a/FILE.bin", "link"])
def test_ci_zip_uses_bounded_shared_archive_validation_and_detects_flattened_collisions(tmp_path_factory, unsafe):
    import stat
    import zipfile
    workspace=tmp_path_factory/"workspace"; workspace.mkdir()
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,"w") as bundle:
        bundle.writestr("file.bin",b"payload")
        if unsafe == "link":
            info=zipfile.ZipInfo("link"); info.external_attr=(stat.S_IFLNK|0o777)<<16
            bundle.writestr(info,b"target")
        else: bundle.writestr(unsafe,b"unreviewed")
    with StorageRuntime(workspace,state_home=tmp_path_factory/"state") as storage:
        store=ReleaseArtifacts(storage,"operation")
        try:
            value=store.ingest([raw.getvalue()],size=len(raw.getvalue()))
            with pytest.raises(ValueError): store.unpack(value)
        finally: store.close()
