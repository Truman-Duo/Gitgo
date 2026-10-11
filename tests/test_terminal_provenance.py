"""A valid publisher cannot excuse replaced unsigned package dependencies."""
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from backend.core.executable_identity import assert_identity_unchanged, verify_git_bash
from backend.core.package_provenance import GIT_TERMINAL_IMAGES, verify_git_terminal_package
from scripts import stage_terminal_provenance as staging


def package_fixture(base):
    root, references = base / "Git", base / "references"
    references.mkdir()
    files = {}
    for name in (*GIT_TERMINAL_IMAGES, "usr/bin/msys-extra.dll"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = b"MZ" + name.encode()
        path.write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
    reference = {"schema_version": 1, "package": "git-for-windows", "release": "v2.53.0.windows.2",
                 "architecture": "x64", "source": "https://github.com/git-for-windows/git/releases/tag/v2.53.0.windows.2",
                 "archive": {"sha256": "a" * 64}, "files": files}
    manifest = references / "git-for-windows-fixture.json"
    manifest.write_text(json.dumps(reference), encoding="utf-8")
    return root, references, manifest


@pytest.mark.parametrize("replacement", ["git-bash.exe", "usr/bin/mintty.exe", "usr/bin/msys-extra.dll", "usr/bin/winpty-agent.exe"])
def test_official_package_replacement_is_rejected_before_probe(tmp_path_factory, replacement):
    root, references, _ = package_fixture(tmp_path_factory)
    assert verify_git_terminal_package(root, reference_directory=references)["verified"]
    (root / replacement).write_bytes(b"MZfake")
    calls = []
    result = verify_git_bash(root,
        signature_reader=lambda paths: [{"path": str(p), "status": "Valid", "subject": "CN=Johannes Schindelin",
                                       "thumbprint": "same"} for p in paths],
        package_verifier=lambda p: verify_git_terminal_package(p, reference_directory=references),
        probe_runner=lambda *args, **kwargs: calls.append(args))
    assert not result["verified"]
    assert result["code"] == "TERMINAL_PACKAGE_UNVERIFIED"
    assert not calls


def test_added_dll_invalidates_discovery_and_later_launch(tmp_path_factory):
    root, references, _ = package_fixture(tmp_path_factory)
    checked = verify_git_terminal_package(root, reference_directory=references)
    (root / "bin/injected.dll").write_bytes(b"MZunknown")
    result = verify_git_terminal_package(root, reference_directory=references)
    assert not result["verified"]
    assert "Unreferenced" in result["message"]
    with pytest.raises(ValueError, match="directory changed"):
        assert_identity_unchanged(checked)


def test_removed_dependency_cannot_fall_through_to_path(tmp_path_factory):
    root, references, _ = package_fixture(tmp_path_factory)
    (root / "usr/bin/msys-extra.dll").unlink()
    assert not verify_git_terminal_package(root, reference_directory=references)["verified"]


def test_missing_or_unsafe_reference_is_a_public_failure(tmp_path_factory):
    root, references, manifest = package_fixture(tmp_path_factory)
    reference = json.loads(manifest.read_text())
    reference["files"]["../outside.dll"] = "b" * 64
    manifest.write_text(json.dumps(reference))
    assert "Unsafe" in verify_git_terminal_package(root, reference_directory=references)["message"]
    manifest.unlink()
    assert "No bundled" in verify_git_terminal_package(root, reference_directory=references)["message"]


@pytest.mark.parametrize("unsafe", [False, True])
def test_staging_hashes_archive_without_extracting_or_running_images(tmp_path_factory, monkeypatch, unsafe):
    monkeypatch.setattr(staging, "ROOT", tmp_path_factory)
    cache = tmp_path_factory / ".gitgo/terminal-reference"
    cache.mkdir(parents=True)
    archive = cache / "Git-2.53.0.2-64-bit.tar.bz2"
    names = [*staging.IMAGES, *( ["usr/bin/../../outside.dll"] if unsafe else ["usr/bin/extra.dll"])]
    with tarfile.open(archive, "w:bz2") as output:
        for name in names:
            data = b"MZinert-test-reference"
            member = tarfile.TarInfo(name)
            member.size = len(data)
            output.addfile(member, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    payload = {"assets": [{"name": archive.name, "digest": "sha256:" + digest,
                "browser_download_url": f"https://github.com/git-for-windows/git/releases/download/v2.53.0.windows.2/{archive.name}",
                "size": archive.stat().st_size}]}
    monkeypatch.setattr(staging, "request", lambda url: io.BytesIO(json.dumps(payload).encode()))
    if unsafe:
        with pytest.raises(ValueError, match="Unsafe"):
            staging.stage("v2.53.0.windows.2", "x64")
    else:
        staging.stage("v2.53.0.windows.2", "x64")
        reference = json.loads(next((tmp_path_factory / "backend/resources/terminal_provenance").glob("*.json")).read_text())
        assert reference["archive"]["sha256"] == digest
        assert reference["files"]["usr/bin/extra.dll"] == hashlib.sha256(b"MZinert-test-reference").hexdigest()
        assert not (tmp_path_factory / "usr").exists()
