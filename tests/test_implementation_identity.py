from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from types import SimpleNamespace
import pytest

from backend.core import implementation_identity as identity
from backend.core.loop.permission_broker import tool_contract_digest, tool_contract_digests, grant_is_current
from tests.test_execution_contract import tool


@pytest.fixture
def source_tree(tmp_path_factory, monkeypatch):
    package = tmp_path_factory / "backend"
    (package / "core").mkdir(parents=True)
    current = package / "core" / "implementation_identity.py"
    current.write_text("value = 1\n", encoding="utf8")
    monkeypatch.setattr(identity, "__file__", str(current))
    monkeypatch.delattr(identity.sys, "frozen", raising=False)
    return package


def test_new_component_source_is_covered_without_a_handwritten_import_list(source_tree):
    before = identity.runtime_implementation_digest()
    extension = source_tree / "core" / "new_component.py"
    extension.write_text("effect = 1\n", encoding="utf8")
    added = identity.runtime_implementation_digest()
    extension.write_text("effect = 2\n", encoding="utf8")
    changed = identity.runtime_implementation_digest()
    extension.unlink()
    assert len({before, added, changed}) == 3
    assert identity.runtime_implementation_digest() == before


def test_source_read_failure_never_reuses_old_implementation_identity(source_tree, monkeypatch):
    identity.runtime_implementation_digest()
    path = source_tree / "core" / "implementation_identity.py"
    path.write_text("value = 22\n", encoding="utf8")
    def denied(self):
        raise PermissionError("source unavailable")
    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(PermissionError):
        identity.runtime_implementation_digest()


def test_same_size_source_change_with_restored_mtime_still_changes_identity(source_tree):
    path = source_tree / "core" / "implementation_identity.py"
    before = identity.runtime_implementation_digest()
    info = path.stat()
    path.write_text("value = 2\n", encoding="utf8")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert identity.runtime_implementation_digest() != before


def test_writable_hardlink_cannot_alias_trusted_execution_source(source_tree):
    source = source_tree / "core" / "implementation_identity.py"
    alias = source_tree.parent / "alias.py"
    alias.hardlink_to(source)
    with pytest.raises(RuntimeError, match="multiply linked"):
        identity.runtime_implementation_digest()


def test_frozen_identity_reads_actual_loader_code(monkeypatch):
    monkeypatch.setattr(identity.sys, "frozen", True, raising=False)
    monkeypatch.setattr(identity, "_frozen_digest", None)
    monkeypatch.setattr(identity.pkgutil, "walk_packages", lambda *a, **k: [SimpleNamespace(name="backend.component")])
    version = [1]
    loader = SimpleNamespace(get_code=lambda name: compile("value=" + str(version[0]), name, "exec"))
    monkeypatch.setattr(identity.importlib.util, "find_spec", lambda name: SimpleNamespace(loader=loader))
    before = identity.runtime_implementation_digest()
    version[0] = 2
    monkeypatch.setattr(identity, "_frozen_digest", None)  # a new installed frozen generation
    assert identity.runtime_implementation_digest() != before


def test_unenumerable_frozen_runtime_is_refused(monkeypatch):
    monkeypatch.setattr(identity.sys, "frozen", True, raising=False)
    monkeypatch.setattr(identity, "_frozen_digest", None)
    monkeypatch.setattr(identity.pkgutil, "walk_packages", lambda *a, **k: [])
    with pytest.raises(RuntimeError, match="catalog is unavailable"):
        identity.runtime_implementation_digest()


def test_native_binary_content_and_abi_invalidate_prior_approval(tmp_path_factory, source_tree):
    binary = tmp_path_factory / "helper.bin"
    binary.write_bytes(b"first native implementation")
    artifact = identity.ImplementationArtifact("native-helper", binary, 1)
    target = tool(implementation_artifacts=(artifact,))
    process = SimpleNamespace(process_id="worker")
    grant = {"process_id": "worker", "tool_contract_digest": tool_contract_digest(target),
             "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    assert grant_is_current(grant, process, target)
    binary.write_bytes(b"replacement native implementation")
    assert not grant_is_current(grant, process, target)
    replacement = tool(implementation_artifacts=(identity.ImplementationArtifact("native-helper", binary, 2),))
    assert tool_contract_digest(replacement) != tool_contract_digest(target)
    binary.unlink()
    with pytest.raises(OSError):
        tool_contract_digest(target)


def test_model_shaped_native_registration_is_refused(tmp_path_factory):
    with pytest.raises(ValueError, match="trusted registrations"):
        tool(implementation_artifacts=({"name": "helper", "path": str(tmp_path_factory)},))


def test_artifact_substitution_is_refused_by_existing_execution_contract(tmp_path_factory):
    target = tool()
    target.implementation_artifacts = (identity.ImplementationArtifact("replacement", tmp_path_factory / "helper"),)
    with pytest.raises(ValueError, match="native implementation changed"):
        target.validate_execution_contract()


def test_one_catalog_comparison_uses_one_runtime_observation(source_tree, monkeypatch):
    from backend.core.loop import permission_broker
    reads = []
    current = permission_broker._runtime_implementation_digest
    def observe():
        reads.append(1)
        return current()
    monkeypatch.setattr(permission_broker, "_runtime_implementation_digest", observe)
    versions = tool_contract_digests({"first": tool(), "second": tool()})
    assert len(versions) == 2 and reads == [1]
