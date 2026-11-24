"""Preserve old readers while the portable updater accepts an executable feed."""
from types import SimpleNamespace

import pytest

from backend.core.update import portable
from backend.core.update.portable import ExecutableVerifier, ready_candidate
from backend.core.update.validation import UpdateError
from tests.test_update_manifest import manifest_wire, signed, signing


def test_only_the_portable_reader_accepts_a_signed_executable(signing):
    raw, signature = signed(manifest_wire(platform="windows", architecture="x86_64",
                                          archive_format="exe"), signing)
    _, verifier = signing
    with pytest.raises(UpdateError, match="Unsupported archive format"):
        verifier.verify(raw, signature)
    assert ExecutableVerifier(verifier).verify(raw, signature).archive_format == "exe"
    archive, signature = signed(manifest_wire(platform="windows", architecture="x86_64",
                                              archive_format="zip"), signing)
    with pytest.raises(UpdateError, match="Unsupported archive format"):
        ExecutableVerifier(verifier).verify(archive, signature)


def test_portable_refuses_updates_that_need_data_migration(monkeypatch):
    old = object()
    updated = SimpleNamespace(data_compatibility=object())
    monkeypatch.setattr("backend.core.update.portable.check_for_update",
                        lambda *args, **kwargs: SimpleNamespace(candidate=updated))
    with pytest.raises(UpdateError, match="data compatibility contract"):
        ready_candidate(SimpleNamespace(data_compatibility=old), None, None,
                        SimpleNamespace(fetch_bytes=None))


def test_health_check_preserves_short_extraction_root_and_isolates_profile(monkeypatch, tmp_path):
    profile = tmp_path / "health-profile"
    candidate = tmp_path / "candidate.exe"
    monkeypatch.setattr(portable.ntfs, "create_private_directory", lambda path: path.mkdir())
    monkeypatch.setattr(portable.tempfile, "gettempdir", lambda: str(tmp_path))

    def start(argv, *, cwd, env, **_):
        assert argv == [str(candidate), "--version"]
        assert cwd == profile
        assert env["TMP"] == env["TEMP"] == str(tmp_path)
        assert env["GITGO_STATE_HOME"] == str(profile / "state")
        return SimpleNamespace(returncode=0, stdout="Gitgo 1.0.0-alpha.3.2\n")

    monkeypatch.setattr(portable.subprocess, "run", start)
    portable._health_check(candidate, profile, "1.0.0-alpha.3.2")
    assert not profile.exists()
