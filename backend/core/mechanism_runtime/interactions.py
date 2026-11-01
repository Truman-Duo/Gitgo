"""Question checkpoints over the existing decision/card and exact-call runtime.

The stored records link an invocation to genuine question/answer identities.
They are advancement holds only: they neither issue nor consume permissions.
"""
from __future__ import annotations

from copy import deepcopy
import json

from .contracts import digest


class Interactions:
    def __init__(self, application):
        self.app = application

    @property
    def process(self):
        return self.app.process

    def checkpoint(self, ctx, payload):
        if ctx.boundary != "before_tool" or not ctx.event_id.startswith("unit:"):
            raise ValueError("MECHANISM_CHECKPOINT_BOUNDARY_INVALID")
        event = payload["event"]
        unit = event.get("unit_id")
        if ctx.event_id != "unit:" + str(unit) + ":g" + str(ctx.epoch):
            raise ValueError("MECHANISM_CHECKPOINT_UNIT_MISMATCH")
        if len({payload["hint_label"], payload["skip_label"], payload["exit_label"]}) != 3:
            raise ValueError("MECHANISM_CHECKPOINT_CONTROLS_DUPLICATE")
        instance, _ = self.app.repo.instance(ctx.task_id, ctx.instance_id)
        previous = instance.get("holds", {}).get(unit)
        if previous:
            return {"unit_id": unit, "decision_id": previous.get("decision_id"), "phase": previous["phase"]}
        topic = "mechanism." + digest([ctx.task_id, ctx.instance_id, unit])
        request = {"kind": "runtime_checkpoint", "state_topic": topic, "question": payload["question"] + "\n\n"
            + json.dumps({"tool": event["tool_name"], "arguments": event["arguments"]}, ensure_ascii=False),
            "why_user_must_decide": payload["reason"], "allow_free_form": True,
            "options": [{"label": payload["hint_label"]}, {"label": payload["skip_label"]}, {"label": payload["exit_label"]}]}
        hold = {"unit_id": unit, "epoch": ctx.epoch, "node_id": ctx.node_id, "phase": "preparing",
            "request": request, "hint": payload["hint"], "max_hints": payload["max_hints"], "hints": 0,
            "hint_label": payload["hint_label"], "skip_label": payload["skip_label"], "exit_label": payload["exit_label"],
            "decision_id": None, "answer_id": None, "prior_decision_ids": []}
        def stage(value):
            if len(value.setdefault("holds", {})) >= 128:
                raise ValueError("MECHANISM_CHECKPOINT_CAPACITY")
            value["holds"][unit] = deepcopy(hold)
        self.app.repo.mutate(ctx.task_id, ctx.instance_id, stage)
        decision = self._request(request)
        def commit(value):
            value["holds"][unit].update(phase="waiting", decision_id=decision["decision_id"], decision_sequence=decision.get("decision_sequence", 0))
        self.app.repo.mutate(ctx.task_id, ctx.instance_id, commit)
        return {"unit_id": unit, "decision_id": decision["decision_id"], "phase": "waiting"}

    def _request(self, request):
        from backend.core.loop.decision_support import create_user_decision
        # A request already committed to the canonical ledger is never recreated
        # merely because a component lost its reply or observation delivery.
        pending = self.process.pending_decision
        if pending and pending.get("task_id") == self.app.task and pending.get("state_topic") == request["state_topic"]:
            return pending
        decision = create_user_decision(self.process, request)
        store = getattr(getattr(self.process, "_manager", None), "session_store", None)
        if store is None:
            raise ValueError("MECHANISM_CHECKPOINT_STORE_REQUIRED")
        store.save_process_checkpoint(self.process)
        return decision

    def _answer(self, decision_id):
        return next((event for event in reversed(self.process.session.host_ledger)
            if event.get("event") == "user_decision_received" and event.get("decision_id") == decision_id
            and event.get("task_id") == self.app.task), None)

    def admit(self, unit):
        """Consume real interaction receipts without converting them to grants."""
        for instance in self.app.repo.list_instances(self.app.task, limit=100):
            hold = instance.get("holds", {}).get(unit)
            if not hold or hold["phase"] in {"answered", "skipped", "exited", "compared"}:
                continue
            if hold["phase"] != "waiting":
                raise ValueError("MECHANISM_CHECKPOINT_RECONCILIATION_REQUIRED")
            answer = self._answer(hold["decision_id"])
            if not answer:
                if not self.process.pending_decision or self.process.pending_decision["decision_id"] != hold["decision_id"]:
                    raise ValueError("MECHANISM_CHECKPOINT_QUESTION_UNAVAILABLE")
                return {"unit_id": unit, "instance_id": instance["id"], "epoch": hold["epoch"],
                        "decision_id": hold["decision_id"]}
            selected = answer.get("selected_label")
            if selected == hold["hint_label"]:
                hints = hold["hints"] + 1
                request = deepcopy(hold["request"])
                request["state_topic"] += ".hint." + str(hints)
                request["supersedes_decision_id"] = hold["decision_id"]
                request["question"] += "\n\n" + hold["hint"]
                if hints >= hold["max_hints"]:
                    request["options"] = [option for option in request["options"] if option["label"] != hold["hint_label"]]
                decision = self._request(request)
                def retain(value):
                    current = value["holds"][unit]
                    current["prior_decision_ids"].append(current["decision_id"])
                    current.update(decision_id=decision["decision_id"], hints=hints)
                self.app.repo.mutate(self.app.task, instance["id"], retain)
                return {"unit_id": unit, "instance_id": instance["id"], "epoch": hold["epoch"], "decision_id": decision["decision_id"]}
            phase = "skipped" if selected == hold["skip_label"] else "exited" if selected == hold["exit_label"] else "answered"
            def release(value):
                value["holds"][unit].update(phase=phase, answer_id=answer["decision_id"])
                if phase == 'exited':
                    for key, other in value.get('holds', {}).items():
                        if key != unit and key not in value.get('active_calls', {}) and other['phase'] in {'preparing', 'waiting', 'answered'}:
                            other.update(phase='cancelled', reason='instance_exit_before_operation')
            if phase == "exited":
                from backend.core.loop.mechanism_lifecycle import next_authority_sequence
                sequence = answer.get("authority_sequence")
                if type(sequence) is not int:
                    sequence = next_authority_sequence(self.process.session)
                    store = getattr(getattr(self.process, "_manager", None), "session_store", None)
                    if store is None:
                        raise ValueError("MECHANISM_CHECKPOINT_STORE_REQUIRED")
                    store.save_process_checkpoint(self.process)
                current, _ = self.app.repo.instance(self.app.task, instance["id"])
                self.app.coordinator.request(self.app.task, instance["id"], current["desired_ref"],
                    expected_revision=current["revision"], action="suspend", sources={"decision:" + answer["decision_id"]: deepcopy(answer)},
                    user_control={"origin": "canonical_checkpoint_choice", "source_ref": "decision:" + answer["decision_id"],
                        "authority_sequence": sequence}, update_state=release)
                self.app.coordinator.settle(self.app.task, instance["id"])
            else:
                self.app.repo.mutate(self.app.task, instance["id"], release)
        return None

    def retire_unstarted(self, unit, reason):
        """Close an obsolete prediction; this is never a tool-effect receipt."""
        for instance in self.app.repo.list_instances(self.app.task, limit=100):
            hold = instance.get('holds', {}).get(unit)
            if not hold or hold['phase'] not in {'preparing', 'waiting', 'answered'}:
                continue
            def retire(value):
                if unit in value.get('active_calls', {}):
                    raise ValueError('MECHANISM_STARTED_INVOCATION_CANNOT_RETIRE')
                current = value['holds'][unit]
                if current['phase'] in {'preparing', 'waiting', 'answered'}:
                    current.update(phase='superseded', reason=reason)
            self.app.repo.mutate(self.app.task, instance['id'], retire)

    def compare(self, ctx, payload):
        unit = payload.get("unit_id")
        instance, _ = self.app.repo.instance(ctx.task_id, ctx.instance_id)
        hold = instance.get("holds", {}).get(unit)
        if not hold or hold["phase"] != "answered":
            return {"available": False}
        answer = self._answer(hold["answer_id"])
        if not answer:
            raise ValueError("MECHANISM_CHECKPOINT_ANSWER_UNAVAILABLE")
        actual = {key: deepcopy(payload.get(key)) for key in ("tool_name", "is_error", "receipt", "result_preview")}
        projection = self.app.project_context(ctx, {"content": "Compare the user's prediction with this actual tool result and explain the difference. This answer is not a permission grant.",
            "include_event": True, "event": {"unit_id": unit, "question_epoch": hold["epoch"],
                "decision_id": answer["decision_id"], "prediction": answer["answer"], "actual_result": actual}})
        def record(value):
            value["holds"][unit].update(phase="compared", result_digest=digest(actual))
        self.app.repo.mutate(ctx.task_id, ctx.instance_id, record)
        return {"available": True, "projection": projection}
