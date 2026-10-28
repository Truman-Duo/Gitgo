"""Fixed package, native build, tag and new-file export contracts."""
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from backend.core.publish.git import CandidateGit
from backend.core.publish.package import package_bytes, validate_package, version_name
from backend.core.publish.package_io import export_new, read_file
from backend.core.publish.state import PublishError, encoded, read_ledger
from tests.test_publish_commands import setup, call, git
from tests.test_publish_formal import formal_setup, prepare, command, protocol
from tests.test_native_sandbox import isolated_python
from tests.native_source_runtime import workspace_native_launcher
from tests.test_registered_test_service import pytest_native_runtime


def _sum(raw):
    return hashlib.sha256(raw).hexdigest()


def test_archive_uses_only_fixed_git_tree_and_reproduces_all_modes(git_repo):
    (git_repo / "sub").mkdir()
    (git_repo / "sub/run.py").write_bytes(b"print('fixed')\n")
    git(git_repo, "add", "sub/run.py")
    git(git_repo, "update-index", "--chmod=+x", "sub/run.py")
    git(git_repo, "commit", "-m", "executable input")
    source = CandidateGit(git_repo)
    oid = source.oid("HEAD")
    first, manifest = package_bytes(source, oid, "v1.2.3", [])
    (git_repo / "sub/run.py").write_bytes(b"private dirty input")
    (git_repo / "untracked").write_bytes(b"private input")
    second, _ = package_bytes(source, oid, "v1.2.3", [])
    assert first == second
    assert validate_package(first, _sum(first)) == manifest
    assert manifest["files"][1]["mode"] == "100755"
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.read("source/sub/run.py") == b"print('fixed')\n"
        assert "source/untracked" not in archive.namelist()
    with pytest.raises(PublishError, match="DIGEST_MISMATCH"):
        validate_package(first + b"tampered", _sum(first))


@pytest.mark.parametrize("change", ["path", "blob", "tree", "extra", "duplicate", "mode"])
def test_package_rejects_manifest_forgery_and_unsafe_members(git_repo, change):
    original, manifest = package_bytes(CandidateGit(git_repo), git(git_repo, "rev-parse", "HEAD"), "1.0.0", [])
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        members = [(info, archive.read(info)) for info in archive.infolist()]
    if change == "path":
        members[1][0].filename = "../escape"
    elif change == "blob":
        manifest["files"][0]["blob_oid"] = "f" * 40
    elif change == "tree":
        manifest["tree_oid"] = "f" * 40
    elif change == "extra":
        extra = zipfile.ZipInfo("source/extra")
        extra.external_attr = 0o100644 << 16
        members.append((extra, b"extra"))
    elif change == "duplicate":
        members.append(members[1])
    else:
        members[1][0].external_attr = 0o120777 << 16
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for info, raw in members:
            archive.writestr(info, encoded(manifest) if info.filename == "manifest.json" else raw)
    with pytest.raises((PublishError, PermissionError)):
        validate_package(output.getvalue(), _sum(output.getvalue()))


@pytest.mark.parametrize("version", ["../1.0.0", "01.0.0", "1.0", "v1.2.3-01", "1.0.0.lock", "1.0.0\n"])
def test_versions_are_safe_fixed_tag_names(version):
    with pytest.raises(PublishError):
        version_name(version)


def test_export_is_complete_and_never_replaces_existing_file(tmp_path_factory):
    root = tmp_path_factory
    raw = b"immutable bytes\0"
    export_new(str(root), "release.zip", raw, "first")
    assert read_file(str(root), "release.zip") == raw
    with pytest.raises(OSError):
        export_new(str(root), "release.zip", b"replacement", "second")
    assert read_file(str(root), "release.zip") == raw
    assert (root / ".gitgo-publish-second.tmp").read_bytes() == b"replacement"
    with pytest.raises((ValueError, PermissionError)):
        export_new(str(root), "../escape", raw, "third")


def _integrated(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    return command(formal_setup, "execute", checked)["item"]


def _action(formal_setup, item, name, request, **args):
    return call(formal_setup, name, request, item_id=item["id"], expected_revision=item["revision"], **args)


def test_source_package_export_import_and_lost_export_ack(formal_setup, monkeypatch, tmp_path_factory):
    item = _integrated(formal_setup, monkeypatch)
    packaged = _action(formal_setup, item, "package", "package", version="v1.0.0")
    assert "package_id" in packaged, packaged
    item = packaged["item"]
    package = item["packages"][0]
    assert package["source_oid"] == git(formal_setup[2], "rev-parse", "formal")
    export = tmp_path_factory / "exports"
    export.mkdir()
    import backend.core.publish.package_io as io_module
    original = io_module.export_new
    def lose_ack(*args):
        original(*args)
        raise OSError("response lost")
    monkeypatch.setattr(io_module, "export_new", lose_ack)
    lost = _action(formal_setup, item, "export", "export", package_id=package["id"], destination=str(export))
    assert lost["effect_state"] == "ambiguous", lost
    monkeypatch.setattr(io_module, "export_new", lambda *_: pytest.fail("export replayed"))
    reconciled = _action(formal_setup, lost["item"], "distribution_reconcile", "observe-export")
    assert reconciled["item"]["distribution_operation"]["state"] == "observed", reconciled
    imported = _action(formal_setup, reconciled["item"], "import_package", "import",
                       path=str(export / package["filename"]), sha256=package["sha256"])
    assert imported["item"]["package_imports"][0]["installation"] == "not_performed"
    assert not (formal_setup[2] / "source").exists()


def test_packaging_requires_accepted_formal_and_detects_target_drift(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    assert _action(formal_setup, item, "package", "too-early", version="1.0.0")["error"] == "PUBLISH_RELEASE_REQUIRES_FORMAL"
    protocol(monkeypatch)
    item = command(formal_setup, "execute", command(formal_setup, "verify", item)["item"])["item"]
    git(formal_setup[2], "update-ref", "refs/heads/formal", "HEAD")
    assert _action(formal_setup, item, "package", "drift", version="1.0.0")["error"] == "PUBLISH_TARGET_CHANGED"


def test_tag_does_not_move_an_existing_version(formal_setup, monkeypatch):
    item = _integrated(formal_setup, monkeypatch)
    item = _action(formal_setup, item, "package", "package", version="1.2.3")["item"]
    package = item["packages"][0]
    tagged = _action(formal_setup, item, "tag", "tag", package_id=package["id"])
    assert tagged.get("item", {}).get("distribution_operation", {}).get("state") == "observed", tagged
    assert git(formal_setup[2], "rev-parse", "refs/tags/1.2.3") == package["source_oid"]
    git(formal_setup[2], "update-ref", "refs/tags/1.2.3", "HEAD")
    result = _action(formal_setup, tagged["item"], "tag", "conflict", package_id=package["id"])
    assert result["error"] == "PUBLISH_TAG_CONFLICT"


@pytest.mark.parametrize("mutates_source", [False, True])
def test_native_build_uses_fixed_source_and_retains_artifact_evidence(formal_setup, monkeypatch, pytest_native_runtime, mutates_source):
    _, _, repo = formal_setup
    script = "from pathlib import Path\np=Path('dist');p.mkdir()\n(p/'release.bin').write_bytes(b'fixed'*(600*1024))\n"
    if mutates_source:
        script += "Path('README.md').write_text('private source was modified')\n"
    (repo / "build.py").write_text(script, encoding="utf8")
    git(repo, "add", "build.py")
    git(repo, "commit", "-m", "[ANBM-6] feat: fixed builder")
    # Only verification is a protocol fixture. Restore the real native seam
    # before invoking the builder below.
    with monkeypatch.context() as checks:
        item = _integrated(formal_setup, checks)
    (repo / "build.py").write_text("raise Exception('dirty input must not run')", encoding="utf8")
    result = _action(formal_setup, item, "package", "build", version="2.0.0",
        recipe={"entry": "build.py", "outputs": ["dist/release.bin"], "timeout": 30})
    if mutates_source:
        assert not result["item"].get("packages")
        assert result["item"]["distribution_operation"]["reason"] == "PUBLISH_BUILD_SOURCE_CHANGED"
        assert (repo / "README.md").read_text() == "# Test"
        return
    if "package_id" not in result:
        from backend.core.loop.publish_recovery import read
        from backend.core.storage import get_storage
        record = read(get_storage(str(repo)), result["item"]["distribution_operation"])
        pytest.fail(json.dumps(record.get("native_producers", record), ensure_ascii=False))
    package = result["item"]["packages"][0]
    assert package["build"]["environment"]["digest"]
    from backend.core.storage import get_storage
    raw = get_storage(str(repo)).read_blob(package["blob_ref"])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("artifacts/dist/release.bin") == b"fixed" * (600 * 1024)
    assert not (repo / "dist").exists()
    assert "dirty input" in (repo / "build.py").read_text()


def test_release_and_download_use_saved_package_and_verify_real_bytes(formal_setup, monkeypatch, tmp_path_factory):
    import httpx
    import tests.test_publish_release_transport as fixture
    from backend.remote.publish_platform import PlatformClient
    item = _integrated(formal_setup, monkeypatch)
    item = _action(formal_setup, item, "package", "package", version="1.0.0")["item"]
    package = item["packages"][0]
    platform = fixture.Platform("github")
    monkeypatch.setattr(fixture, "OID", package["source_oid"])
    monkeypatch.setattr("backend.remote.publish_accounts.resolve_secret", lambda _: "test-secret")
    monkeypatch.setattr("backend.remote.publish_platform.PlatformClient", lambda target, token: PlatformClient(target, token, transport=httpx.MockTransport(platform.request)))
    released = _action(formal_setup, item, "release", "release", package_id=package["id"], target=platform.target.descriptor())
    assert released.get("item", {}).get("distribution_operation", {}).get("state") == "observed", released
    assert len(platform.assets) == 3
    destination = tmp_path_factory / "download"
    destination.mkdir()
    downloaded = _action(formal_setup, released["item"], "download", "download", package_id=package["id"],
                         target=platform.target.descriptor(), destination=str(destination))
    assert downloaded.get("item", {}).get("distribution_operation", {}).get("state") == "observed", downloaded
    assert _sum((destination / package["filename"]).read_bytes()) == package["sha256"]
    assert downloaded["item"]["distribution_operation"]["verification"] == "downloaded_bytes_and_git_tree"


def test_lost_package_receipt_does_not_duplicate_or_rebuild_version(formal_setup, monkeypatch):
    from backend.core.publish.distribution import DistributionMixin
    item = _integrated(formal_setup, monkeypatch)
    original = DistributionMixin._finish_package
    def lost(self, *args):
        original(self, *args)
        raise OSError("receipt lost after CAS commit")
    monkeypatch.setattr(DistributionMixin, "_finish_package", lost)
    pending = _action(formal_setup, item, "package", "lost-package", version="1.0.0")
    assert pending["effect_state"] == "ambiguous"
    assert len(pending["item"]["packages"]) == 1
    monkeypatch.setattr(DistributionMixin, "_finish_package", original)
    monkeypatch.setattr("backend.core.publish.distribution.package_bytes", lambda *_a, **_k: pytest.fail("package rebuilt after commit"))
    observed = _action(formal_setup, pending["item"], "distribution_reconcile", "observe-package")
    assert len(observed["item"]["packages"]) == 1
    assert observed["item"]["distribution_operation"]["state"] == "observed"
    repeated = _action(formal_setup, observed["item"], "package", "same-version", version="1.0.0")
    assert repeated["package_id"] == observed["package_id"]


def test_real_native_build_pause_waits_for_retirement_and_does_not_publish(formal_setup, monkeypatch, pytest_native_runtime):
    import concurrent.futures
    import time
    _, _, repo = formal_setup
    (repo / "build.py").write_text("import time\nwhile True: time.sleep(0.1)\n", encoding="utf8")
    git(repo, "add", "build.py")
    git(repo, "commit", "-m", "[ANBM-6] feat: cancellable builder")
    with monkeypatch.context() as checks:
        item = _integrated(formal_setup, checks)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_action, formal_setup, item, "package", "cancel-build", version="2.0.0",
            recipe={"entry": "build.py", "outputs": ["dist/release.bin"], "timeout": 30})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            ledger, _ = read_ledger(str(repo))
            current = ledger["items"][item["id"]]
            operation = current.get("distribution_operation", {})
            if operation.get("state") == "intent":
                from backend.core.loop.publish_recovery import read
                from backend.core.storage import get_storage
                record = read(get_storage(str(repo)), operation)
                if record.get("producers"):
                    break
            time.sleep(0.02)
        else:
            pytest.fail("build never acquired native producer")
        paused = _action(formal_setup, current, "pause", "pause-build")
        assert paused["item"]["distribution_operation"]["cancel_requested"] is True
        assert paused["item"]["distribution_operation"]["state"] == "intent"
        finished = future.result(timeout=20)
    assert finished["item"]["distribution_operation"]["state"] == "failed"
    assert finished["item"]["distribution_operation"]["reason"] == "PUBLISH_BUILD_CANCELLED"
    assert finished["item"]["status"] == "integrated" and not finished["item"].get("packages")
    record = read(get_storage(str(repo)), finished["item"]["distribution_operation"])
    assert all(row["phase"] == "retired" for row in record["producers"])
