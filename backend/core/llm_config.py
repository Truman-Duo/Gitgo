"""LLM Provider configuration — read/write global llm_config.json.

Data model mirrors cc-switch's provider pattern (simplified):
  - Multiple named providers, each with base_url/api_key/model_id
  - One active_provider at a time
  - Optional failover order (for future use)

Configuration is GLOBAL (not per-project): stored in the user Gitgo directory
so all projects share one provider set. Thread-safe for native/MCP transports via _lock.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


CONFIG_FILENAME = "llm_config.json"
SECRET_FILENAME = "provider_secrets.json"


@dataclass
class LLMProvider:
    """A single LLM provider configuration."""
    name: str
    base_url: str
    api_key: str
    model_id: str
    protocol: str = "openai_chat"
    capabilities: dict = field(default_factory=dict)
    context_window: int = 128000
    max_output_tokens: int = 4096
    limits_source: str = "default"
    id: str = ""
    created_at: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LLMProvider":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            base_url=d.get("base_url", ""),
            api_key=d.get("api_key", ""),
            model_id=d.get("model_id", ""),
            protocol=d.get("protocol", "openai_chat"),
            capabilities=dict(d.get("capabilities", {}) or {}),
            context_window=int(d.get(
                "context_window",
                (d.get("capabilities", {}) or {}).get("context_window", 128000),
            ) or 128000),
            max_output_tokens=int(d.get(
                "max_output_tokens",
                (d.get("capabilities", {}) or {}).get("max_output_tokens", 4096),
            ) or 4096),
            limits_source=str(d.get("limits_source", "default")),
            created_at=d.get("created_at", ""),
        )

    def runtime_capabilities(self) -> dict:
        result = dict(self.capabilities)
        result["context_window"] = self.context_window
        result["max_output_tokens"] = self.max_output_tokens
        return result


class LLMConfigManager:
    """Static methods for reading/writing the global llm_config.json."""

    _lock = threading.RLock()

    @staticmethod
    def _config_path() -> Path:
        override = os.environ.get("GITGO_LLM_CONFIG_PATH", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        # Provider credentials are user-scoped runtime state.  Never derive
        # their default location from cwd, which may be a publishable repo.
        return Path.home() / ".gitgo" / CONFIG_FILENAME

    @staticmethod
    def _legacy_config_paths() -> list[Path]:
        from backend.core.config import ConfigManager
        legacy = ConfigManager.default_path().parent / CONFIG_FILENAME
        canonical = LLMConfigManager._config_path()
        return [legacy] if legacy.resolve() != canonical.resolve() else []

    @staticmethod
    def _secret_path() -> Path:
        override = os.environ.get("GITGO_LLM_SECRET_PATH", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        return LLMConfigManager._config_path().with_name(SECRET_FILENAME)

    @staticmethod
    def _secret_store():
        from backend.core.secret_store import EncryptedSecretStore
        return EncryptedSecretStore(LLMConfigManager._secret_path())

    @staticmethod
    def _empty_config() -> dict:
        return {
            "providers": [], "active_provider": "",
            "failover_enabled": False, "failover_order": [],
        }

    @staticmethod
    def _hydrate(config: dict) -> dict:
        """Resolve secret references into process memory only."""
        result = dict(config)
        providers = [dict(item) for item in (config.get("providers") or [])]
        referenced = {
            str(item.get("secret_ref")) for item in providers
            if item.get("secret_ref")
        }
        secrets = LLMConfigManager._secret_store().read_all() if referenced else {}
        for provider in providers:
            ref = str(provider.get("secret_ref") or "")
            provider["api_key"] = secrets.get(ref, "") if ref else str(provider.get("api_key") or "")
        result["providers"] = providers
        return result

    @staticmethod
    def load() -> dict:
        """Load full config dict. Returns empty default if file doesn't exist."""
        with LLMConfigManager._lock:
            return LLMConfigManager._load_unlocked()

    @staticmethod
    def _load_unlocked() -> dict:
        path = LLMConfigManager._config_path()
        if not path.exists():
            for legacy in LLMConfigManager._legacy_config_paths():
                if not legacy.exists():
                    continue
                try:
                    config = json.loads(legacy.read_text(encoding="utf-8"))
                    LLMConfigManager._save_unlocked(config)
                    legacy.unlink()
                    return LLMConfigManager._hydrate(json.loads(
                        LLMConfigManager._config_path().read_text(encoding="utf-8")
                    ))
                except (OSError, json.JSONDecodeError):
                    # Fail closed: do not silently consume or overwrite a
                    # credential file that could not be migrated safely.
                    return LLMConfigManager._empty_config()
            return LLMConfigManager._empty_config()
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
        # One-time migration for older plaintext provider configurations.
        if any("api_key" in item for item in (config.get("providers") or [])):
            LLMConfigManager._save_unlocked(config)
            config = json.loads(path.read_text(encoding="utf-8"))
        return LLMConfigManager._hydrate(config)

    @staticmethod
    def save(config: dict) -> None:
        """Atomically write the full config dict to disk."""
        with LLMConfigManager._lock:
            LLMConfigManager._save_unlocked(config)

    @staticmethod
    def _save_unlocked(config: dict) -> None:
        config = dict(config)
        providers = [dict(item) for item in (config.get("providers") or [])]
        secret_updates: dict[str, str] = {}
        secret_refs: set[str] = set()
        for provider in providers:
            provider_id = str(provider.get("id") or "")
            if not provider_id:
                provider_id = uuid.uuid4().hex[:12]
                provider["id"] = provider_id
            secret_ref = str(provider.get("secret_ref") or f"provider:{provider_id}:api_key")
            raw_key = str(provider.pop("api_key", "") or "")
            provider["secret_ref"] = secret_ref
            secret_refs.add(secret_ref)
            if raw_key:
                secret_updates[secret_ref] = raw_key
        config["providers"] = providers
        path = LLMConfigManager._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        secret_store = (
            LLMConfigManager._secret_store()
            if secret_updates or secret_refs or LLMConfigManager._secret_path().exists()
            else None
        )
        # Write new secrets first. A crash can leave an inert orphan, but can
        # never commit metadata that points at a missing credential.
        if secret_store is not None:
            secret_store.upsert(secret_updates)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
            # Prune only after provider metadata is durable.
            if secret_store is not None:
                secret_store.retain_only(secret_refs)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def get_providers() -> list[LLMProvider]:
        """Return all configured providers."""
        config = LLMConfigManager.load()
        return [LLMProvider.from_dict(p) for p in config.get("providers", [])]

    @staticmethod
    def get_active() -> LLMProvider | None:
        """Return the currently active provider, or None."""
        config = LLMConfigManager.load()
        active_id = config.get("active_provider", "")
        if not active_id:
            return None
        for p in config.get("providers", []):
            if p.get("id") == active_id:
                return LLMProvider.from_dict(p)
        return None

    @staticmethod
    def add(provider: LLMProvider) -> LLMProvider:
        """Add a new provider. Sets it as active if it's the first one."""
        with LLMConfigManager._lock:
            config = LLMConfigManager._load_unlocked()
            config["providers"].append(provider.to_dict())
            if not config.get("active_provider"):
                config["active_provider"] = provider.id
            LLMConfigManager._save_unlocked(config)
        return provider

    @staticmethod
    def update(provider: LLMProvider) -> LLMProvider | None:
        """Update an existing provider by id. Returns None if not found."""
        with LLMConfigManager._lock:
            config = LLMConfigManager._load_unlocked()
            for i, p in enumerate(config["providers"]):
                if p.get("id") == provider.id:
                    config["providers"][i] = provider.to_dict()
                    LLMConfigManager._save_unlocked(config)
                    return provider
        return None

    @staticmethod
    def delete(provider_id: str) -> bool:
        """Delete a provider by id. Clears active_provider if it was the deleted one.
        Returns False if not found, True if deleted."""
        with LLMConfigManager._lock:
            config = LLMConfigManager._load_unlocked()
            before = len(config["providers"])
            config["providers"] = [
                p for p in config["providers"] if p.get("id") != provider_id
            ]
            if len(config["providers"]) == before:
                return False

            if config.get("active_provider") == provider_id:
                config["active_provider"] = config["providers"][0]["id"] if config["providers"] else ""
            LLMConfigManager._save_unlocked(config)
            return True

    @staticmethod
    def switch(provider_id: str) -> LLMProvider | None:
        """Set a provider as active. Returns the provider or None if not found."""
        with LLMConfigManager._lock:
            config = LLMConfigManager._load_unlocked()
            for p in config["providers"]:
                if p.get("id") == provider_id:
                    config["active_provider"] = provider_id
                    LLMConfigManager._save_unlocked(config)
                    return LLMProvider.from_dict(p)
        return None
