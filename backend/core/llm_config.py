"""LLM Provider configuration — read/write global llm_config.json.

Data model mirrors cc-switch's provider pattern (simplified):
  - Multiple named providers, each with base_url/api_key/model_id
  - One active_provider at a time
  - Optional failover order (for future use)

Configuration is GLOBAL (not per-project): stored next to gitgo_config.json
so all projects share one provider set. Thread-safe for MCP server usage via _lock.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


CONFIG_FILENAME = "llm_config.json"


@dataclass
class LLMProvider:
    """A single LLM provider configuration."""
    name: str
    base_url: str
    api_key: str
    model_id: str
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
            created_at=d.get("created_at", ""),
        )


class LLMConfigManager:
    """Static methods for reading/writing the global llm_config.json."""

    _lock = threading.Lock()

    @staticmethod
    def _config_path() -> Path:
        from backend.core.config import ConfigManager

        return ConfigManager.default_path().parent / CONFIG_FILENAME

    @staticmethod
    def load() -> dict:
        """Load full config dict. Returns empty default if file doesn't exist."""
        path = LLMConfigManager._config_path()
        if not path.exists():
            return {
                "providers": [],
                "active_provider": "",
                "failover_enabled": False,
                "failover_order": [],
            }
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def save(config: dict) -> None:
        """Write full config dict to disk."""
        path = LLMConfigManager._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with LLMConfigManager._lock:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)

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
        config = LLMConfigManager.load()
        config["providers"].append(provider.to_dict())
        if not config.get("active_provider"):
            config["active_provider"] = provider.id
        LLMConfigManager.save(config)
        return provider

    @staticmethod
    def update(provider: LLMProvider) -> LLMProvider | None:
        """Update an existing provider by id. Returns None if not found."""
        config = LLMConfigManager.load()
        for i, p in enumerate(config["providers"]):
            if p.get("id") == provider.id:
                config["providers"][i] = provider.to_dict()
                LLMConfigManager.save(config)
                return provider
        return None

    @staticmethod
    def delete(provider_id: str) -> bool:
        """Delete a provider by id. Clears active_provider if it was the deleted one.
        Returns False if not found, True if deleted."""
        config = LLMConfigManager.load()
        before = len(config["providers"])
        config["providers"] = [
            p for p in config["providers"] if p.get("id") != provider_id
        ]
        if len(config["providers"]) == before:
            return False

        if config.get("active_provider") == provider_id:
            config["active_provider"] = config["providers"][0]["id"] if config["providers"] else ""
        LLMConfigManager.save(config)
        return True

    @staticmethod
    def switch(provider_id: str) -> LLMProvider | None:
        """Set a provider as active. Returns the provider or None if not found."""
        config = LLMConfigManager.load()
        for p in config["providers"]:
            if p.get("id") == provider_id:
                config["active_provider"] = provider_id
                LLMConfigManager.save(config)
                return LLMProvider.from_dict(p)
        return None
