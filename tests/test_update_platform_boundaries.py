"""Non-POSIX rejection and disabled gates run on every CI host, including Windows."""

from types import SimpleNamespace

import pytest

from backend.core.update import UpdatePolicy, prepare_update
from backend.core.update import staging, persistence
from backend.core.update.transaction import SimulationStore, initialize_simulation
from backend.core.update.validation import UpdateError


def test_non_posix_staging_refuses_before_inspecting_paths(monkeypatch):
    monkeypatch.setattr(staging, "os", SimpleNamespace(name="nt"))
    with pytest.raises(UpdateError, match="UPDATE_STAGING_PLATFORM"):
        staging._staging_parent(None, None)


def test_non_posix_simulation_provisioning_is_not_an_implicit_windows_adapter(monkeypatch):
    monkeypatch.setattr(staging, "os", SimpleNamespace(name="nt"))
    with pytest.raises(UpdateError, match="UPDATE_STAGING_PLATFORM"):
        initialize_simulation(None, None, None, protected_paths=None)


def test_non_posix_locks_refuse_before_opening_files(monkeypatch):
    monkeypatch.setattr(persistence, "os", SimpleNamespace(name="nt"))
    with pytest.raises(UpdateError, match="UPDATE_TRANSACTION_PLATFORM"):
        with persistence.lock_file(None):
            pytest.fail("unvalidated Windows lock adapter was used")


def test_disabled_preparation_and_apply_need_no_os_adapter():
    def forbidden():
        pytest.fail("disabled update accessed a dependency")
    assert prepare_update(UpdatePolicy(), None, None, staging_parent=None, protected_paths=None,
                          clock=forbidden, cancelled=forbidden).status == "disabled"
    store = object.__new__(SimulationStore)
    assert store.apply(UpdatePolicy(), None, None, None, None, health_check=None, clock=forbidden).status == "disabled"
