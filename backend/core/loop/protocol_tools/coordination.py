"""coordination protocol tools."""
from __future__ import annotations
from backend.core.loop.agent_tool import AgentTool, ToolEffect

def install(ctx):
    process = ctx.process
    on_stream_event = ctx.on_stream_event
    tools = ctx.tools
    manager = ctx.manager
    def list_coordination_events(args: dict) -> dict:
        from backend.core.loop.coordination import current_coordination_events
        status_filter = str(args.get("status") or "pending")
        events = current_coordination_events(process.session.host_ledger)
        if status_filter == "pending":
            events = [
                item for item in events
                if str(item.get("status") or "pending") in {
                    "pending", "awaiting_supervisor", "awaiting_user",
                    "rework_requested",
                }
            ]
        elif status_filter != "all":
            events = [
                item for item in events
                if str(item.get("status") or "") == status_filter
            ]
        return {"events": events[-64:], "count": len(events)}


    tools["list_coordination_events"] = AgentTool(
        name="list_coordination_events",
        description=(
            "List Host-routed worker escalation and dependency-interface events owned "
            "by this A. Use pending for decisions still needed or all for audit history."
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "pending", "all", "awaiting_supervisor", "awaiting_user",
                        "rework_requested", "accepted", "resolved", "restored",
                        "dismissed",
                    ],
                },
            },
            "required": [],
        },
        execute=list_coordination_events,
        read_only=True,
        effect=ToolEffect.READ,
        idempotent=True,
    )


    def resolve_coordination(args: dict) -> dict:
        if manager is None:
            return {"accepted": False, "error": "AgentProcessManager is unavailable"}
        from backend.core.errors import error_payload
        from backend.core.loop.coordination import resolve_coordination_event
        try:
            event = resolve_coordination_event(
                manager,
                process,
                str(args.get("event_id") or ""),
                disposition=str(args.get("disposition") or ""),
                note=str(args.get("note") or ""),
            )
        except KeyError:
            return {"accepted": False, **error_payload(
                "COORDINATION_EVENT_NOT_FOUND",
                next_actions=[{"action": "list_coordination_events", "effect": "Refresh owned event IDs."}],
            )}
        except ValueError:
            return {"accepted": False, **error_payload(
                "COORDINATION_RESOLUTION_INVALID",
                next_actions=[{
                    "action": "choose_resolution",
                    "effect": "Use accept_revision/request_rework/ask_user for interface revisions; acknowledge or dismiss other events.",
                }],
            )}
        if on_stream_event is not None:
            on_stream_event({
                "event": "coordination_event_resolved",
                "process_id": process.process_id,
                **event,
                "_trace_detail": event,
            })
        response = {"accepted": True, **event}
        if event.get("status") == "awaiting_user":
            response["next_actions"] = [{
                "action": "request_user_decision",
                "effect": "Ask the user with concrete options, principles, immediate effects and downstream effects, then resolve this event from the answer.",
            }]
        elif event.get("status") == "rework_requested":
            response["next_actions"] = [{
                "action": "send_feedback_or_continue_original_worker",
                "effect": "Use send_feedback while the source B is running; otherwise continue_process_id preserves its session for rework.",
            }]
        return response


    tools["resolve_coordination_event"] = AgentTool(
        name="resolve_coordination_event",
        description=(
            "Resolve a Host-routed coordination event. Interface revisions require "
            "accept_revision, request_rework, or ask_user; accepting mechanically "
            "updates the versioned interface document and releases affected downstream "
            "B Agents. This is the only route for changing a cross-B task boundary."
        ),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "disposition": {"type": "string", "enum": [
                    "accept_revision", "request_rework", "ask_user",
                    "acknowledge", "dismiss",
                ]},
                "note": {"type": "string"},
            },
            "required": ["event_id", "disposition", "note"],
        },
        execute=resolve_coordination,
        read_only=True,
        effect=ToolEffect.READ,
        idempotent=False,
    )


