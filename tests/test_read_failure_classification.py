"""Read errors remain distinct from missing objects, including Python 3.14 exists()."""
from pathlib import Path

import pytest

from backend.core.tools.workspace_tools import list_files, search_text


@pytest.mark.parametrize("handler", [list_files, search_text])
@pytest.mark.parametrize("failure,code", [(PermissionError, "READ_ACCESS_DENIED"), (OSError, "READ_ERROR")])
def test_read_status_error_never_becomes_a_successful_missing_directory(tmp_path_factory, monkeypatch, handler, failure, code):
    root = tmp_path_factory
    target = root / "protected"
    target.mkdir()
    real_stat, real_exists = Path.stat, Path.exists

    def denied(path, *args, **kwargs):
        if path == target:
            raise failure(5, "synthetic status failure", str(path))
        return real_stat(path, *args, **kwargs)

    # Python 3.14 suppresses these OS errors in exists(); keep that regression
    # condition explicit even when this unit runs with an older interpreter.
    monkeypatch.setattr(Path, "exists", lambda path: False if path == target else real_exists(path))
    monkeypatch.setattr(Path, "stat", denied)
    result = handler({"_workspace": str(root), "path": str(target), "pattern": "text"})
    assert result["error"] == code
    assert result.get("exists") is not False
    assert result["effect_state"] == "not_committed"


def test_missing_file_parent_is_distinct_from_read_permission(tmp_path_factory):
    result = search_text({"_workspace": str(tmp_path_factory), "path": "missing", "pattern": "text"})
    assert result["error"] == "FILE_NOT_FOUND"
