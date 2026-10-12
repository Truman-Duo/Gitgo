"""Shared message identities; delivery classes retain different guarantees.

This contract does not turn observations into authoritative facts, grant
permissions, or make a synchronous policy decision asynchronous.
"""
from dataclasses import dataclass, field, asdict
from enum import Enum
import uuid


class DeliveryClass(str, Enum):
    COMMAND = "command"
    FACT = "durable_fact"
    OBSERVATION = "observation"
    STREAM = "ephemeral_stream"
    POLICY = "synchronous_policy"


@dataclass(frozen=True)
class MessageIdentity:
    kind: str
    delivery_class: DeliveryClass
    producer: str
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = ""
    causation_id: str = ""
    requires: tuple[str, ...] = ()

    def to_dict(self):
        return {**asdict(self), "delivery_class": self.delivery_class.value}


def observation_identity(event, trace_id, sequence):
    kind = str(event.get("event") or "unknown")
    delivery = DeliveryClass.STREAM if kind in {"text_delta", "reasoning_delta", "toolcall_delta"} else DeliveryClass.OBSERVATION
    return MessageIdentity(kind, delivery, "TaskRuntime",
        message_id=f"trace:{trace_id}:{sequence}",
        correlation_id=str(event.get("root_task_id") or event.get("task_id") or trace_id),
        causation_id=str(event.get("causation_id") or event.get("tool_call_id") or "")).to_dict()


def project_fact_to_trace(storage, fact):
    """Independent observer cursor; import's exact key deduplicates retries."""
    stream = "runtime-facts:" + fact["stream_id"]
    record = {"event": "runtime_fact_committed", "trace_id": stream,
              "seq": fact["stream_sequence"], "time": fact["occurred_at"],
              "process_id": fact["process_id"], "task_id": fact["task_id"],
              "kind": fact["kind"], "producer": fact["producer"],
              "event_id": fact["event_id"], "correlation_id": fact["correlation_id"],
              "causation_id": fact["causation_id"], "delivery_class": "durable_fact"}
    if storage.import_trace_records(stream, [(record, None)]) is None:
        raise RuntimeError("RUNTIME_FACT_OBSERVATION_THROTTLED")
