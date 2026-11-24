"""Runtime registration must unwind before its owning outer fixture."""
from unittest.mock import patch

import pytest

from backend.core import sandbox
from tests.native_source_runtime import workspace_native_launcher  # noqa: F401


@pytest.fixture
def outer_runtime_registration(tmp_path_factory):
    original = sandbox.trusted_runtime_roots
    registered = lambda: (tmp_path_factory,)
    with patch.object(sandbox, 'trusted_runtime_roots', registered):
        yield tmp_path_factory
        # This runs after workspace_native_launcher but before the shared
        # monkeypatch fixture, reproducing native_box's actual lifetime.
        assert sandbox.trusted_runtime_roots is registered
    assert sandbox.trusted_runtime_roots is original


@pytest.fixture
def isolated_python(outer_runtime_registration, monkeypatch):
    runtime = outer_runtime_registration / 'runtime'
    runtime.mkdir()
    monkeypatch.setattr('tests.native_source_runtime.populate_source_runtime', lambda root: root)
    return runtime


def test_nested_runtime_registration_does_not_outlive_owner(
        outer_runtime_registration, workspace_native_launcher):
    assert sandbox.trusted_runtime_roots() == (
        outer_runtime_registration, workspace_native_launcher)
