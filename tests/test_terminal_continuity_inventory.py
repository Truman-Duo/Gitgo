"""A lifecycle timer must not be mistaken for split conversation storage."""
import uuid

import pytest

from scripts.check_terminal_continuity import continuity_stores


def store(root, name):
    workspace = root / name
    identity = workspace / ".gitgo" / "project-id"
    identity.parent.mkdir(parents=True)
    value = str(uuid.uuid4())
    identity.write_text(value, encoding="ascii")
    path = root / "state" / "projects" / value
    path.mkdir(parents=True)
    return workspace, path


def test_continuity_before_and_after_lifecycle_timer(tmp_path_factory):
    tmp_path = tmp_path_factory
    workspace, conversation = store(tmp_path, "workspace")
    assert continuity_stores(tmp_path, workspace) == ([conversation], [])
    _, catalog = store(tmp_path, "maintenance-catalog")
    assert continuity_stores(tmp_path, workspace) == ([conversation], [catalog])


def test_unrelated_or_duplicate_conversation_store_still_fails(tmp_path_factory):
    tmp_path = tmp_path_factory
    workspace, _ = store(tmp_path, "workspace")
    store(tmp_path, "maintenance-catalog")
    extra = tmp_path / "state" / "projects" / str(uuid.uuid4())
    extra.mkdir()
    with pytest.raises(AssertionError):
        continuity_stores(tmp_path, workspace)


def test_missing_conversation_identity_is_not_a_successful_empty_store(tmp_path_factory):
    tmp_path = tmp_path_factory
    store(tmp_path, "maintenance-catalog")
    with pytest.raises(FileNotFoundError):
        continuity_stores(tmp_path, tmp_path / "workspace")
