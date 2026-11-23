"""Collect one provider response; never authorize or execute tool calls.

The attempt controller owns routing/retry. This collector retains partial
evidence on exceptions and validates tool arguments before dispatch.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import json
import uuid
from .provider_protocol import ProviderEventType, deterministic_hash
from .tool_summary import tool_argument_preview

@dataclass
class ProviderResponse:
    accumulated_text: str = ""
    accumulated_reasoning: str = ""
    accumulated_reasoning_details: list = field(default_factory=list)
    provider_artifacts: list[dict] = field(default_factory=list)
    provider_incomplete_reason: str = ""
    provider_incomplete_detail: object = None
    pending_tool_calls: dict[int, dict] = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    invalid_tool_calls: list[dict] = field(default_factory=list)
    public_progress: dict[str, str] = field(default_factory=dict)

class ProviderResponseCollector:
    def __init__(self, process, session, provider, emit, on_stream_event, cache_intent, budget_stream_prefix, state):
        self.process = process
        self.session = session
        self.llm_provider = provider
        self.emit = emit
        self.on_stream_event = on_stream_event
        self.cache_intent = cache_intent
        self.budget_stream_prefix = budget_stream_prefix
        self.state = state
        self.attempt_id = uuid.uuid4().hex

    def _progress_id(self, event):
        return "native:" + deterministic_hash([
            self.process.steps_used, event.response_id or self.attempt_id,
            event.message_id, event.output_index,
        ])

    def finish_progress(self, identity, status="completed"):
        message = self.state.public_progress.get(identity, "")
        if not message:
            return
        from .public_progress import record_progress
        record, created = record_progress(
            self.process, message, identity, source="provider_commentary", status=status,
        )
        if created:
            self.emit({"event": "progress_summary", "message": record["message"],
                       "update_id": identity, "task_id": record["task_id"],
                       "phase": "commentary", "progress_status": status})

    def finish_partial_progress(self):
        # A transport failure must retain explicitly public partial text. It
        # must not turn an interrupted model message into a completed update.
        for identity in self.state.public_progress:
            self.finish_progress(identity, "interrupted")

    def consume(self, event):
        if event.type == ProviderEventType.MESSAGE_COMPLETED and event.phase == "commentary":
            self.finish_progress(self._progress_id(event))
            return
        if event.type == ProviderEventType.PROVIDER_CAPABILITY_FALLBACK:
            artifact = dict(event.artifact or {})
            self.emit({
                "event": "provider_capability_fallback",
                "capability": artifact.get("capability", "hosted_web_search"),
                "from": artifact.get("from", "provider"),
                "to": artifact.get("to", "searxng"),
                "status_code": artifact.get("status_code"),
                "reason": artifact.get("reason", "provider_rejected_capability"),
            }, detail=artifact)
            return
        if event.type in {
            ProviderEventType.SERVER_TOOL_STARTED,
            ProviderEventType.SERVER_TOOL_RESULT,
        }:
            self.emit({
                "event": (
                    "toolcall_start"
                    if event.type == ProviderEventType.SERVER_TOOL_STARTED
                    else "tool_result"
                ),
                "tool_name": event.tool_name or "web_search",
                "tool_call_id": event.tool_call_id,
                "is_error": False,
                "result_preview": "Provider-hosted web search completed",
                "compact_summary": "provider search completed",
                "server_tool": True,
            }, detail=event.raw or event.artifact)
            return
        text = event.text if event.type == ProviderEventType.TEXT_DELTA else ""
        if text:
            if self.process.task_budget is not None:
                self.process.task_budget.consume_output_fragment(
                    text, self.process.process_id,
                    channel=self.budget_stream_prefix + "text",
                )
            if event.phase == "commentary":
                identity = self._progress_id(event)
                self.state.public_progress[identity] = self.state.public_progress.get(identity, "") + text
                self.emit({"event":"progress_delta","delta":text,"update_id":identity,
                           "task_id":self.process.active_task_id or self.process.process_id,"phase":"commentary"})
                return
            self.state.accumulated_text += text
            if self.on_stream_event:
                self.on_stream_event({
                    "event": "text_delta",
                    "process_id": self.process.process_id,
                    "delta": text,
                    "step": self.process.steps_used,
                    "visibility": (
                        "public" if event.phase == "final_answer" or self.process.task_kind == "answer" else "verbose"
                    ),
                })

        reasoning_delta = (
            event.reasoning
            if event.type == ProviderEventType.REASONING_DELTA else ""
        )
        if reasoning_delta:
            if self.process.task_budget is not None:
                self.process.task_budget.consume_output_fragment(
                    reasoning_delta, self.process.process_id,
                    channel=self.budget_stream_prefix + "reasoning",
                )
            self.state.accumulated_reasoning += reasoning_delta
            self.emit({
                "event": "reasoning_delta",
                "delta": reasoning_delta,
            })
        reasoning_details_delta = (
            event.artifact
            if event.type == ProviderEventType.REASONING_ARTIFACT else None
        )
        if reasoning_details_delta is not None:
            if isinstance(reasoning_details_delta, dict):
                self.state.provider_artifacts.append(reasoning_details_delta)
                if "reasoning_details" in reasoning_details_delta:
                    detail = reasoning_details_delta["reasoning_details"]
                    if self.process.task_budget is not None:
                        self.process.task_budget.consume_output_fragment(
                            json.dumps(detail, ensure_ascii=False),
                            self.process.process_id,
                            channel=self.budget_stream_prefix + "reasoning-artifact",
                        )
                    if isinstance(detail, list):
                        self.state.accumulated_reasoning_details.extend(detail)
                    else:
                        self.state.accumulated_reasoning_details.append(detail)

        if event.type == ProviderEventType.USAGE and event.usage:
            if self.process.task_budget is not None:
                self.process.task_budget.record_provider_usage(
                    event.usage.to_dict()
                )
            self.session.record_provider_usage(
                event.usage.to_dict(),
                protocol=str(getattr(
                    getattr(self.llm_provider, "protocol", ""), "value",
                    getattr(self.llm_provider, "protocol", ""),
                )),
                cache_intent=self.cache_intent,
            )
            self.emit({
                "event": "provider_usage",
                "usage": event.usage.to_dict(),
                "cache": self.session.cache_summary(),
            })

        if event.type == ProviderEventType.RESPONSE_INCOMPLETE:
            artifact = dict(event.artifact or {})
            self.state.provider_incomplete_reason = str(
                artifact.get("reason", "unknown")
            )
            known_item_ids = {
                str(item.get("response_output_item", {}).get("id", ""))
                for item in self.state.provider_artifacts if isinstance(item, dict)
            }
            for item in artifact.get("response_output_items", []) or []:
                item_id = str(item.get("id", ""))
                if item_id and item_id in known_item_ids:
                    continue
                self.state.provider_artifacts.append({"response_output_item": item})
                known_item_ids.add(item_id)
            # Emit the semantic event once after the stream finishes so
            # one provider response maps to one timeline row.  Keep the
            # raw terminal event for that final observation.
            self.state.provider_incomplete_detail = event.raw or artifact

        if event.type in {
            ProviderEventType.TOOL_CALL_STARTED,
            ProviderEventType.TOOL_CALL_DELTA,
            ProviderEventType.TOOL_CALL_DONE,
        }:
            idx = max(0, event.output_index)
            if idx not in self.state.pending_tool_calls:
                self.state.pending_tool_calls[idx] = {
                    "id": event.tool_call_id,
                    "name": event.tool_name,
                    "partial_json": "",
                    "done": False,
                }
                from .provider_tool_transport import CALL
                if self.on_stream_event and event.tool_name != CALL:
                    self.on_stream_event({
                        "event": "toolcall_start",
                        "process_id": self.process.process_id,
                        "tool_call_id": self.state.pending_tool_calls[idx]["id"],
                        "tool_name": self.state.pending_tool_calls[idx]["name"],
                    })

            if event.tool_call_id:
                self.state.pending_tool_calls[idx]["id"] = event.tool_call_id
            if event.tool_name:
                self.state.pending_tool_calls[idx]["name"] = event.tool_name
            args_fragment = event.arguments_delta
            if event.type == ProviderEventType.TOOL_CALL_DONE and event.arguments:
                if not self.state.pending_tool_calls[idx]["partial_json"]:
                    args_fragment = event.arguments
            if event.type == ProviderEventType.TOOL_CALL_DONE:
                self.state.pending_tool_calls[idx]["done"] = True
            if args_fragment:
                if self.process.task_budget is not None:
                    self.process.task_budget.consume_output_fragment(
                        args_fragment, self.process.process_id,
                        channel=self.budget_stream_prefix + f"tool-{idx}",
                    )
                self.state.pending_tool_calls[idx]["partial_json"] += args_fragment
                from .provider_tool_transport import CALL
                if self.on_stream_event and self.state.pending_tool_calls[idx]['name'] != CALL:
                    self.on_stream_event({
                        "event": "toolcall_delta",
                        "process_id": self.process.process_id,
                        "tool_call_id": self.state.pending_tool_calls[idx]["id"],
                        "delta": args_fragment,
                    })

    def decode_tool_calls(self):
        for idx in sorted(self.state.pending_tool_calls.keys()):
            p = self.state.pending_tool_calls[idx]
            # A normal completed compatibility stream may not expose an
            # explicit TOOL_CALL_DONE event.  For an incomplete Responses
            # stream, however, replaying a merely-started call would make
            # the next request invalid because no output can exist for it.
            if self.state.provider_incomplete_reason and not p["done"]:
                continue
            try:
                args = json.loads(p["partial_json"]) if p["partial_json"].strip() else {}
            except json.JSONDecodeError as exc:
                # Never turn malformed provider output into an executable
                # empty argument object.  Doing so erases the distinction
                # between "the model omitted path" and "the provider cut a
                # valid call in half", and can send a write tool into the
                # rollback loop with entirely fabricated arguments.
                self.state.invalid_tool_calls.append({
                    "name": p["name"],
                    "id": p["id"],
                    "raw": p["partial_json"],
                    "error": str(exc),
                })
                self.emit({
                    "event": "toolcall_invalid",
                    "tool_call_id": p["id"],
                    "tool_name": p["name"],
                    "output_index": idx,
                    "error_code": "INVALID_TOOL_ARGUMENT_JSON",
                }, detail={
                    "arguments_raw": p["partial_json"],
                    "parse_error": str(exc),
                })
                continue
            if not isinstance(args, dict):
                self.state.invalid_tool_calls.append({
                    "name": p["name"],
                    "id": p["id"],
                    "raw": p["partial_json"],
                    "error": "tool arguments must decode to an object",
                })
                continue
            call = {
                "name": p["name"],
                "id": p["id"],
                "args": args,
            }
            from .provider_tool_transport import decode
            try:
                call = decode(call, raw_arguments=p['partial_json'])
            except ValueError as error:
                self.state.invalid_tool_calls.append({'name':p['name'],'id':p['id'],
                    'raw':p['partial_json'],'error':str(error), 'error_code':'INVALID_TOOL_TRANSPORT'})
                self.emit({'event':'toolcall_invalid','tool_call_id':p['id'],'tool_name':p['name'],
                    'output_index':idx,'error_code':'INVALID_TOOL_TRANSPORT'},
                    detail={'arguments_raw':p['partial_json'],'parse_error':str(error)})
                continue
            self.state.tool_calls.append(call)
            if call.get('_provider_wire_call') and self.on_stream_event:
                self.on_stream_event({'event':'toolcall_start','process_id':self.process.process_id,
                    'tool_call_id':p['id'],'tool_name':call['name']})
                self.on_stream_event({'event':'toolcall_delta','process_id':self.process.process_id,
                    'tool_call_id':p['id'],'delta':json.dumps(call['args'],ensure_ascii=False)})
            self.emit({
                "event": "toolcall_done",
                "tool_call_id": p["id"],
                "tool_name": call["name"],
                "output_index": idx,
                "argument_preview": tool_argument_preview(call['args']),
            }, detail={"arguments": call['args'], "arguments_raw": p["partial_json"],
                'provider_wire_call':call.get('_provider_wire_call')})
