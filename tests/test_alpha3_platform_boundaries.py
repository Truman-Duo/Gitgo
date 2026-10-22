"""Explicit refusal contracts for inherited unsupported platform operations.

These narrow dispatch probes are not native macOS/Linux acceptance. Supported
Windows operations continue to run in the existing real Git and native suites.
"""
from types import SimpleNamespace

import pytest


def test_unknown_protected_git_refuses_before_executable_lookup(monkeypatch, tmp_path_factory):
    from backend.core.loop import git_execution

    def unexpected(*args, **kwargs):
        pytest.fail("Unsupported protected Git must not resolve or launch an executable")

    # Replace only this module's dispatch reference, never global os.name.
    monkeypatch.setattr(git_execution, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(git_execution, "sys", SimpleNamespace(platform='unsupported'))
    monkeypatch.setattr(git_execution, "_executable", unexpected)
    monkeypatch.setattr(git_execution, "_invoke", unexpected)
    with pytest.raises(git_execution.GitBoundaryError) as caught:
        with git_execution.protected_git(tmp_path_factory, ["git", "status"]):
            pytest.fail("Unsupported platform yielded an execution capability")
    assert caught.value.code == "GIT_BOUNDARY_UNAVAILABLE"
    assert list(tmp_path_factory.iterdir()) == []


def test_darwin_stable_read_refuses_without_ordinary_file_fallback(monkeypatch, tmp_path_factory):
    from backend.core import sandbox_read
    from backend.core import sandbox_read_darwin

    # Exercise production dispatch with an existing file, not an OS emulation.
    (tmp_path_factory / "ordinary.txt").write_text("must stay unread", encoding="utf-8")
    monkeypatch.setattr(sandbox_read, "sys", SimpleNamespace(platform="darwin"))
    def missing_kernel(*args):
        raise sandbox_read.ReadBoundaryError('No stable read backend for this kernel')
    monkeypatch.setattr(sandbox_read_darwin, 'root', missing_kernel)
    with pytest.raises(sandbox_read.ReadBoundaryError, match="No stable read backend"):
        with sandbox_read.ReadRoot(tmp_path_factory) as root:
            root.open("ordinary.txt")


def test_darwin_new_workspace_governance_refuses_instead_of_empty_state(monkeypatch, tmp_path_factory):
    from backend.core import sandbox_read
    from backend.core import sandbox_read_darwin
    from backend.core.sync_session.state import GovernanceStateUnavailable, read_state

    monkeypatch.setattr(sandbox_read, "sys", SimpleNamespace(platform="darwin"))
    def missing_kernel(*args):
        raise sandbox_read.ReadBoundaryError('No stable read backend for this kernel')
    monkeypatch.setattr(sandbox_read_darwin, 'root', missing_kernel)
    with pytest.raises(GovernanceStateUnavailable) as caught:
        read_state(tmp_path_factory)
    assert caught.value.code == "GOVERNANCE_STATE_UNAVAILABLE"
    assert isinstance(caught.value.__cause__, sandbox_read.ReadBoundaryError)
    assert list(tmp_path_factory.iterdir()) == []
