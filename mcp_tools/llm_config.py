"""MCP tools — LLM provider configuration (CRUD + switch).

Mirrors cc-switch's provider management pattern:
  - gitgo_llm_status: list all providers + active + failover state
  - gitgo_llm_save: create or update a provider (upsert by id)
  - gitgo_llm_switch: set active provider
  - gitgo_llm_delete: remove a provider
"""

from __future__ import annotations


def register(mcp):
    """Register LLM config tools on FastMCP instance."""

    @mcp.tool(description="获取全局 LLM Provider 配置列表、当前激活、failover 状态")
    def gitgo_llm_status() -> dict:
        """Return all LLM providers + active_provider + failover state."""
        from backend.core.llm_config import LLMConfigManager

        config = LLMConfigManager.load()
        providers = config.get("providers", [])

        # Mask API keys in the response
        masked = []
        for p in providers:
            pd = dict(p)
            key = pd.get("api_key", "")
            if key and len(key) > 8:
                pd["api_key"] = key[:4] + "***" + key[-4:]
            elif key:
                pd["api_key"] = "***"
            masked.append(pd)

        return {
            "providers": masked,
            "active_provider": config.get("active_provider", ""),
            "failover_enabled": config.get("failover_enabled", False),
            "failover_order": config.get("failover_order", []),
        }

    @mcp.tool(description="新建或更新 LLM Provider（根据 id 判断 upsert）；key 用明文传入")
    def gitgo_llm_save(
        provider_id: str = "",
        name: str = "",
        base_url: str = "",
        api_key: str = "",
        model_id: str = "",
    ) -> dict:
        """Create or update a provider. If provider_id is given and exists, update it.
        Otherwise create a new provider. Returns the saved provider (key masked)."""
        from backend.core.llm_config import LLMConfigManager, LLMProvider

        if not name or not base_url or not model_id:
            return {"error": "MISSING_FIELDS", "message": "name, base_url, model_id are required"}

        provider = LLMProvider(
            id=provider_id,
            name=name,
            base_url=base_url,
            api_key=api_key,
            model_id=model_id,
        )

        if provider_id:
            existing = [p for p in LLMConfigManager.get_providers() if p.id == provider_id]
            if existing:
                updated = LLMConfigManager.update(provider)
                if updated is None:
                    return {"error": "UPDATE_FAILED", "provider_id": provider_id}
                d = updated.to_dict()
                key = d.get("api_key", "")
                d["api_key"] = key[:4] + "***" + key[-4:] if key and len(key) > 8 else "***"
                return {"status": "updated", "provider": d}

        # New provider
        created = LLMConfigManager.add(provider)
        d = created.to_dict()
        key = d.get("api_key", "")
        d["api_key"] = key[:4] + "***" + key[-4:] if key and len(key) > 8 else "***"
        return {"status": "created", "provider": d}

    @mcp.tool(description="切换当前激活的 LLM Provider")
    def gitgo_llm_switch(provider_id: str) -> dict:
        """Set a provider as the active one."""
        from backend.core.llm_config import LLMConfigManager

        provider = LLMConfigManager.switch(provider_id)
        if provider is None:
            return {"error": "PROVIDER_NOT_FOUND", "provider_id": provider_id}

        d = provider.to_dict()
        key = d.get("api_key", "")
        d["api_key"] = key[:4] + "***" + key[-4:] if key and len(key) > 8 else "***"
        return {"status": "switched", "active_provider": provider_id, "provider": d}

    @mcp.tool(description="删除 LLM Provider")
    def gitgo_llm_delete(provider_id: str) -> dict:
        """Delete a provider by id."""
        from backend.core.llm_config import LLMConfigManager

        ok = LLMConfigManager.delete(provider_id)
        if not ok:
            return {"error": "PROVIDER_NOT_FOUND", "provider_id": provider_id}

        config = LLMConfigManager.load()
        return {
            "status": "deleted",
            "provider_id": provider_id,
            "active_provider": config.get("active_provider", ""),
        }
