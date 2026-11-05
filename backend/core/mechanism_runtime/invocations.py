"""Retain component generations across the existing ToolPipeline invocation."""
from contextlib import ExitStack
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass

_CURRENT = ContextVar("harness_invocation", default=None)


@dataclass(frozen=True)
class RetainedGeneration:
    task_id: str
    instance_id: str
    unit_id: str
    epoch: int
    program: dict
    elements: dict


class InvocationScope:
    def __init__(self, ctx):
        self.ctx, self.leases, self.frames = ctx, ExitStack(), []
        self.result_observed = False

    def __enter__(self):
        self.token = _CURRENT.set(self)
        return self

    def __exit__(self, kind, value, traceback):
        try:
            if kind is not None:
                # An unobserved tool outcome cannot be inferred or replayed.
                for app, frame in self.frames:
                    def unknown(instance):
                        instance["phase"] = "blocked"
                        call = instance.get("active_calls", {}).get(frame.unit_id)
                        if call:
                            call["phase"] = "unknown" if not self.result_observed else "result_observed_cleanup_failed"
                    try:
                        app.repo.mutate(frame.task_id, frame.instance_id, unknown)
                    except (ValueError, RuntimeError, OSError):
                        # Preserve the original failure. A durable active pin or
                        # the storage quarantine still prevents unsafe replay.
                        self.ctx.artifacts["mechanism_cleanup_pending"] = True
        finally:
            self.leases.close()
            _CURRENT.reset(self.token)

    def admit(self, app, unit, expected):
        for selected in expected:
            instance, _ = app.repo.instance(app.task, selected["id"])
            if instance["phase"] != "ready" or instance["epoch"] != selected["epoch"]:
                raise ValueError("MECHANISM_GENERATION_CHANGED_BEFORE_TOOL_ADMISSION")
            elements = {node["id"]: self.leases.enter_context(app.registry.lease(node["use"], node["version"], node["contract_digest"]))
                        for node in instance["compiled"]["elements"]}
            frame = RetainedGeneration(app.task, instance["id"], unit, instance["epoch"], deepcopy(instance["compiled"]), elements)
            def pin(value):
                if value["epoch"] != frame.epoch or value["phase"] != "ready":
                    raise ValueError("MECHANISM_GENERATION_CHANGED_BEFORE_TOOL_ADMISSION")
                if unit in value.setdefault("active_calls", {}):
                    raise ValueError("MECHANISM_TOOL_OUTCOME_RECONCILIATION_REQUIRED")
                value["active_calls"][unit] = {"epoch": frame.epoch, "definition_ref": value["effective_ref"], "phase": "admitted"}
            app.repo.mutate(frame.task_id, frame.instance_id, pin)
            self.frames.append((app, frame))

    def finish(self, result):
        self.result_observed = True
        for app, frame in self.frames:
            if not result.suspended and any(node["entry"] and "after_tool" in node["boundaries"] for node in frame.program["elements"]):
                try:
                    app.runner.dispatch(frame.task_id, frame.instance_id, "result:" + frame.unit_id, "after_tool",
                        {"unit_id": frame.unit_id, "tool_name": result.tool_name, "is_error": result.is_error,
                         "receipt": dict(result.receipt or {}), "result_preview": str(result.formatted)[:4000]},
                        retained=frame, cancelled=lambda: app.process.cancel_requested)
                except (ValueError, RuntimeError, OSError) as error:
                    if app.emit:
                        app.emit({"event": "mechanism_observer_failed", "task_id": frame.task_id,
                            "instance_id": frame.instance_id, "boundary": "after_tool", "detail": str(error)[:400]})
            uncertain = ((result.receipt or {}).get("effect_state") in {"ambiguous", "unknown", "partial"}
                         and (result.receipt or {}).get("effect") not in {"read", "external_read"})
            def release(value):
                if uncertain:
                    value["phase"] = "blocked"
                    value["active_calls"][frame.unit_id].update(phase="unknown", receipt_id=result.receipt.get("receipt_id"))
                else:
                    value.get("active_calls", {}).pop(frame.unit_id, None)
            app.repo.mutate(frame.task_id, frame.instance_id, release)
        # Remove this pin before settling a pending configuration. Actual tool
        # receipts retain the old epoch, regardless of the next ready version.
        for app, frame in self.frames:
            app.coordinator.settle(frame.task_id, frame.instance_id)


def current_scope(ctx):
    scope = _CURRENT.get()
    if scope is None or scope.ctx is not ctx:
        raise ValueError("MECHANISM_INVOCATION_SCOPE_REQUIRED")
    return scope
