import pytest

from scripts.provision_ci_ripgrep import NOTICE_FILES, archive_notices


def test_official_archive_notices_are_complete_and_preserved():
    entries = [(f"release/{name}", f"notice for {name}".encode()) for name in NOTICE_FILES]
    result = archive_notices(entries)
    for name, data in entries:
        assert data in result


def test_missing_or_duplicate_notices_fail_closed():
    entries = [(name, b"notice") for name in NOTICE_FILES]
    with pytest.raises(ValueError):
        archive_notices(entries[:-1])
    with pytest.raises(ValueError):
        archive_notices(entries + [entries[0]])
