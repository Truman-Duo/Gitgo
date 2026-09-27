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
    from backend.core.application import ApplicationServices, OperationError
    services = ApplicationServices()

    def invoke(operation: str, arguments: dict | None = None):
        try:
            return services.invoke(operation, arguments)
        except OperationError as exc:
            return {"error": exc.code, "message": str(exc), "details": exc.details}

    @mcp.tool(description="获取全局 LLM Provider 配置列表、当前激活、failover 状态")
    def gitgo_llm_status() -> dict:
        """Return all LLM providers + active_provider + failover state."""
        return invoke("provider.status")

    @mcp.tool(description="新建或更新 LLM Provider（根据 id 判断 upsert）；key 用明文传入")
    def gitgo_llm_save(
        provider_id: str = "",
        name: str = "",
        base_url: str = "",
        api_key: str = "",
        model_id: str = "",
        protocol: str = "openai_chat",
        context_window: int = 128000,
        max_output_tokens: int = 4096,
        retain_api_key: bool = False,
    ) -> dict:
        """Create or update a provider. If provider_id is given and exists, update it.
        Otherwise create a new provider. Returns the saved provider (key masked)."""
        return invoke("provider.save", {
            "provider_id": provider_id, "name": name, "base_url": base_url,
            "api_key": api_key, "model_id": model_id,
            "protocol": protocol, "context_window": context_window,
            "max_output_tokens": max_output_tokens,
            "retain_api_key": retain_api_key,
        })

    @mcp.tool(description="切换当前激活的 LLM Provider")
    def gitgo_llm_switch(provider_id: str) -> dict:
        """Set a provider as the active one."""
        return invoke("provider.switch", {"provider_id": provider_id})

    @mcp.tool(description="删除 LLM Provider")
    def gitgo_llm_delete(provider_id: str) -> dict:
        """Delete a provider by id."""
        return invoke("provider.delete", {"provider_id": provider_id})
