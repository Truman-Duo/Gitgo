"""Paid acceptance for repairing a persisted unanswered Responses tool call.

The fixture is session-local and never executes the synthetic tool. Credentials
are read from Gitgo's user-scoped provider configuration; output is metadata only.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.llm_config import LLMConfigManager
from backend.core.loop.llm import LLMProvider
from backend.core.loop.models import RingLevel
from backend.core.loop.outcome import OutcomeStatus, TaskOutcome
from backend.core.loop.runtime import AgentRuntimeFactory, RuntimeSpec
from backend.core.loop.executor import agent_step
from backend.core.loop.tools import ToolRegistry


CALL_ID = "call_gitgo_interrupted_recovery_acceptance"
MARKER = "GITGO_INTERRUPTED_TOOL_RECOVERY_OK"


def main() -> int:
    active = LLMConfigManager.get_active()
    if active is None or active.model_id != "deepseek-v4-flash":
        raise RuntimeError("acceptance requires active deepseek-v4-flash")
    if "v4-pro" in active.model_id.casefold():
        raise RuntimeError("v4-pro is forbidden")
    provider = LLMProvider(
        active.base_url,
        active.api_key,
        active.model_id,
        protocol=active.protocol,
        capabilities=active.runtime_capabilities(),
    )
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="supervisor",
        actor_kind="supervisor",
        capability_profile_id="supervisor.answer",
        ring_level=RingLevel.RING_0,
        tool_registry=ToolRegistry([]),
        max_steps=3,
        task_kind="answer",
        task_id="paid-interrupted-tool-recovery",
    ))
    process.session.append_assistant_provider(
        "",
        continuation_state={"response_output_items": [{
            "type": "function_call",
            "call_id": CALL_ID,
            "name": "legacy_interrupted_tool",
            "arguments": "{}",
            "status": "completed",
        }]},
    )
    events: list[dict] = []
    result = agent_step(
        process,
        provider,
        instruction=(
            f"Reply with the exact marker {MARKER} and no other claim."
        ),
        workspace_path=str(ROOT),
        on_stream_event=lambda event: events.append(dict(event)),
    )
    outcome = TaskOutcome.from_dict(result)
    repaired = next(
        message for message in process.session.messages
        if message.get("tool_call_id") == CALL_ID
    )
    assistant_index = next(
        index for index, message in enumerate(process.session.messages)
        if message.get("provider_state_id")
        and CALL_ID in json.dumps(
            process.session.provider_state.get(message["provider_state_id"], {}),
            ensure_ascii=False,
        )
    )
    tool_index = process.session.messages.index(repaired)
    user_index = next(
        index for index, message in enumerate(process.session.messages)
        if message.get("role") == "user" and MARKER in message.get("content", "")
    )
    summary = {
        "verified": (
            outcome.status == OutcomeStatus.COMPLETED
            and outcome.error is None
            and MARKER in outcome.response
            and repaired.get("data", {}).get("execution_state") == "unknown"
            and assistant_index < tool_index < user_index
        ),
        "status": outcome.status.value,
        "error_code": outcome.error.code if outcome.error else "",
        "error_message": str(outcome.error.message if outcome.error else "")[:1000],
        "marker_returned": MARKER in outcome.response,
        "unknown_result_inserted": (
            repaired.get("data", {}).get("execution_state") == "unknown"
        ),
        "provider_order_valid": assistant_index < tool_index < user_index,
        "sealed_event_count": sum(
            event.get("event") == "provider_tool_calls_sealed" for event in events
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
