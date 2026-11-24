"""Preserve old readers while the portable updater accepts an executable feed."""
from types import SimpleNamespace

import pytest

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
