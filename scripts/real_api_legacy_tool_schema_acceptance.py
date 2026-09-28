"""One-call paid acceptance for legacy authored-tool schema normalization."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.llm_config import LLMConfigManager
from backend.core.loop.agent_tool import AgentTool
from backend.core.loop.llm import LLMProvider


def main() -> int:
    active = LLMConfigManager.get_active()
    if active is None or active.model_id != "deepseek-v4-flash":
        raise RuntimeError("acceptance requires active deepseek-v4-flash")
    if "v4-pro" in active.model_id.casefold():
        raise RuntimeError("v4-pro is forbidden")

    # Exact shape persisted by the early author_tool implementation in project
    # 2920: convenient property shorthand, but not a provider-valid top level.
    tool = AgentTool(
        name="html_to_pdf",
        description="Legacy schema transport acceptance; do not invoke.",
        parameters={
            "html_path": {"type": "string"},
            "pdf_path": {"type": "string"},
        },
        execute=lambda _args: {},
    )
    provider = LLMProvider(
        active.base_url, active.api_key, active.model_id,
        protocol=active.protocol, capabilities=active.runtime_capabilities(),
    )
    response = provider.chat(
        [{"role": "user", "content": "Reply with exactly OK. Do not call any tool."}],
        max_tokens=128, timeout=60, tools=[tool.to_openai_function()], max_retries=0,
    )
    schema = tool.to_openai_function()["function"]["parameters"]
    accepted = schema.get("type") == "object" and isinstance(response, dict)
    print(json.dumps({
        "verified": accepted,
        "model": active.model_id,
        "schema_type": schema.get("type"),
        "property_names": sorted((schema.get("properties") or {}).keys()),
        "provider_returned_message": isinstance(response, dict),
    }, sort_keys=True))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
