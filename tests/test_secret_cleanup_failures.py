"""Native deletion failures use fake services, never developer credentials."""
import json
import subprocess

import pytest

from backend.core.secret_store import EncryptedSecretStore, SecretStoreError, _LinuxSecretToolBackend
from tests.test_secret_store import _ReferenceProtector


class FailingProtector(_ReferenceProtector):
    blocked = False

    def delete(self, reference):
        if self.blocked:
            raise SecretStoreError("service locked")
        super().delete(reference)


@pytest.mark.parametrize("code,stderr", [(1, "service unavailable"), (1, "denied"), (2, "locked"), (1, "")])
def test_secret_tool_failure_is_not_success(monkeypatch, code, stderr):
    backend = _LinuxSecretToolBackend("fake-secret-tool")
    monkeypatch.setattr(backend, "_run", lambda *a, **k: subprocess.CompletedProcess([], code, "", stderr))
    with pytest.raises(SecretStoreError):
        backend.delete("test-service", "test-account")


def test_absence_requires_successful_empty_search(monkeypatch):
    backend = _LinuxSecretToolBackend("fake-secret-tool")
    results = iter([subprocess.CompletedProcess([], 1, "", ""), subprocess.CompletedProcess([], 0, "", "")])
    monkeypatch.setattr(backend, "_run", lambda *a, **k: next(results))
    backend.delete("test-service", "test-account")


def test_timeout_never_reports_success_or_leaks_subprocess_output(monkeypatch):
    backend = _LinuxSecretToolBackend("fake-secret-tool")
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired([], 30, output="private-value")
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(SecretStoreError) as error:
        backend.delete("test-service", "test-account")
    assert "private-value" not in str(error.value)


def test_saved_replacement_keeps_failed_old_reference_for_retry(tmp_path_factory):
    path = tmp_path_factory / "index.json"
    protector = FailingProtector()
    store = EncryptedSecretStore(path, protector)
    store.upsert({"a": "first"})
    old = json.loads(path.read_text())["secrets"]["a"]
    protector.blocked = True
    store.upsert({"a": "second"})
    assert store.read_all() == {"a": "second"}
    assert json.loads(path.read_text())["pending_cleanup"] == [old]
    assert EncryptedSecretStore.pending_count(path) == 1
    assert not store.retry_cleanup()
    protector.blocked = False
    assert EncryptedSecretStore(path, protector).retry_cleanup()
    assert old not in protector.values
    assert EncryptedSecretStore.pending_count(path) == 0


def test_prune_and_uninstall_keep_partial_failure_retry_records(tmp_path_factory):
    path = tmp_path_factory / "index.json"
    protector = FailingProtector()
    store = EncryptedSecretStore(path, protector)
    store.upsert({"a": "first", "b": "second"})
    protector.blocked = True
    store.retain_only({"b"})
    assert store.read_all() == {"b": "second"}
    with pytest.raises(SecretStoreError, match="retry index preserved"):
        store.clear()
    assert EncryptedSecretStore.pending_count(path) == 2
    protector.blocked = False
    store.clear()
    assert not protector.values
    assert EncryptedSecretStore.pending_count(path) == 0


def test_native_creation_is_journaled_before_write(tmp_path_factory, monkeypatch):
    path = tmp_path_factory / "index.json"
    protector = FailingProtector()
    store = EncryptedSecretStore(path, protector)
    protector.blocked = True
    def interrupted_write(reference, value):
        assert reference in json.loads(path.read_text())["pending_cleanup"]
        protector.values[reference] = value
        raise SecretStoreError("native write timed out after creating item")
    monkeypatch.setattr(protector, "protect_into", interrupted_write)
    with pytest.raises(SecretStoreError):
        store.upsert({"a": "test-value"})
    assert not store.read_all()
    assert len(protector.values) == EncryptedSecretStore.pending_count(path) == 1
    protector.blocked = False
    assert store.retry_cleanup()


def test_failed_intent_write_never_creates_native_item(tmp_path_factory, monkeypatch):
    protector = FailingProtector()
    store = EncryptedSecretStore(tmp_path_factory / "index.json", protector)
    monkeypatch.setattr(store, "_write_ciphertexts", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError): store.upsert({"a": "first"})
    assert not protector.values


def test_failed_cleanup_checkpoint_keeps_last_durable_retry_index(tmp_path_factory, monkeypatch):
    path = tmp_path_factory / "index.json"
    protector = FailingProtector()
    store = EncryptedSecretStore(path, protector)
    store.upsert({"a": "first"})
    protector.blocked = True
    store.retain_only(set())
    protector.blocked = False
    with monkeypatch.context() as patch:
        patch.setattr(store, "_write_ciphertexts", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        assert not store.retry_cleanup()
    assert EncryptedSecretStore.pending_count(path) == 1
    assert store.retry_cleanup()  # confirmed absent is idempotent


def test_clear_partial_deletion_retries_only_unconfirmed_items(tmp_path_factory, monkeypatch):
    path = tmp_path_factory / "index.json"
    protector = FailingProtector()
    store = EncryptedSecretStore(path, protector)
    store.upsert({"a": "first", "b": "second"})
    refs = list(json.loads(path.read_text())["secrets"].values())
    original = protector.delete
    def partly_locked(ref):
        if ref == refs[1]: raise SecretStoreError("locked")
        original(ref)
    monkeypatch.setattr(protector, "delete", partly_locked)
    with pytest.raises(SecretStoreError): store.clear()
    assert json.loads(path.read_text())["pending_cleanup"] == [refs[1]]
    monkeypatch.setattr(protector, "delete", original)
    store.clear()
    assert not protector.values


def test_legacy_index_is_upgraded_without_losing_retry_records(tmp_path_factory):
    path = tmp_path_factory / "index.json"
    path.write_text('{"version":1,"secrets":{}}')
    protector = FailingProtector()
    store = EncryptedSecretStore(path, protector)
    store.upsert({"a": "first"})
    assert json.loads(path.read_text())["version"] == 2
    assert store.read_all() == {"a": "first"}


def test_provider_api_reports_saved_config_separately_from_cleanup(tmp_path_factory, monkeypatch):
    from backend.core.llm_config import LLMConfigManager
    from backend.core.application.services import ApplicationServices
    path = tmp_path_factory / "index.json"
    protector = FailingProtector()
    monkeypatch.setattr(LLMConfigManager, "_secret_path", lambda: path)
    monkeypatch.setattr(LLMConfigManager, "_secret_store", lambda: EncryptedSecretStore(path, protector))
    service = ApplicationServices()
    first = service.provider_save(name="fixture", base_url="https://example.invalid", api_key="first", model_id="test")
    protector.blocked = True
    saved = service.provider_save(provider_id=first["provider"]["id"], name="fixture",
        base_url="https://example.invalid", api_key="second", model_id="test")
    assert saved["status"] == "updated"
    assert saved["credential_cleanup"]["pending"] == 1
    assert LLMConfigManager.get_active().api_key == "second"
    assert service.provider_status()["credential_cleanup"]["complete"] is False


def test_metadata_write_failure_preserves_old_usable_credential(tmp_path_factory, monkeypatch):
    import os
    from backend.core.llm_config import LLMConfigManager
    from backend.core.application.services import ApplicationServices
    path = tmp_path_factory / "index.json"
    protector = FailingProtector()
    monkeypatch.setattr(LLMConfigManager, "_secret_path", lambda: path)
    monkeypatch.setattr(LLMConfigManager, "_secret_store", lambda: EncryptedSecretStore(path, protector))
    service = ApplicationServices()
    first = service.provider_save(name="fixture", base_url="https://example.invalid", api_key="first", model_id="test")
    config_path = LLMConfigManager._config_path()
    before = config_path.read_bytes()
    replace = os.replace
    def disk_full(src, dest):
        if dest == config_path: raise OSError("disk full")
        return replace(src, dest)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", disk_full)
        with pytest.raises(OSError):
            service.provider_save(provider_id=first["provider"]["id"], name="fixture",
                base_url="https://example.invalid", api_key="second", model_id="test")
    assert config_path.read_bytes() == before
    assert LLMConfigManager.get_active().api_key == "first"
    # The uncommitted new credential is still indexed, not an untraceable orphan.
    assert len(protector.values) == 2
    EncryptedSecretStore(path, protector).clear()
    assert not protector.values
