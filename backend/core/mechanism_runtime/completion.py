"""Additional bounded completion transitions; mandatory completion gates remain."""
from copy import deepcopy
import json


class Completion:
    def __init__(self, application):
        self.app = application

    def controls(self, instance):
        """Projection of real lifecycle answers, never a new approval state."""
        from .semantic_projection import completion_controls
        return completion_controls(self.app.process, instance)

    def return_to_execution(self, ctx, payload):
        from backend.core.loop.mechanism_lifecycle import fence
        with fence(self.app.process), self.app.process.session._mechanism_lock:
            return self._return_current(ctx, payload)

    def _return_current(self, ctx, payload):
        if ctx.boundary != "completion":
            raise ValueError("MECHANISM_COMPLETION_BOUNDARY_INVALID")
        instance, _ = self.app.repo.instance(ctx.task_id, ctx.instance_id)
        previous = instance.get("completion_directives", {}).get(ctx.event_id)
        if previous:
            return previous
        count = instance.get("return_counters", {}).get(ctx.node_id, 0)
        if self.app.process.mailbox is not None and self.app.process.mailbox.snapshot()['pending']:
            # The old read-only stage has finished. Accepted input owns the
            # next boundary; it must not consume another automatic return or
            # get stranded behind a newly generated question.
            result = {"kind": "refresh", "event_id": ctx.event_id, "epoch": ctx.epoch,
                "return_count": count, "evidence": payload.get("evidence", {})}
            self.app.repo.mutate(ctx.task_id, ctx.instance_id,
                lambda value: value.setdefault("completion_directives", {}).__setitem__(ctx.event_id, deepcopy(result)))
            return result
        if count < payload["max_returns"]:
            result = {"kind": "rework", "event_id": ctx.event_id, "epoch": ctx.epoch,
                "return_count": count + 1, "instruction": payload["instruction"], "evidence": payload.get("evidence", {})}
            def returning(value):
                value.setdefault("return_counters", {})[ctx.node_id] = count + 1
                value.setdefault("completion_directives", {})[ctx.event_id] = deepcopy(result)
            self.app.repo.mutate(ctx.task_id, ctx.instance_id, returning)
        else:
            request = {"kind": "runtime_checkpoint", "state_topic": "mechanism.completion." + ctx.instance_id + "." + ctx.node_id,
                "question": payload["question"], "why_user_must_decide": payload["reason"], "options": [], "allow_free_form": True}
            # Publish the actual card at the delivery fence, after ALL old
            # stages finish and queued input is checked again. No synthetic
            # answer or early decision is needed to unblock maintenance.
            result = {"kind": "awaiting_user", "request": request, "instance_id": ctx.instance_id, "event_id": ctx.event_id,
                      "epoch": ctx.epoch, "return_count": count, "evidence": payload.get("evidence", {})}
            self.app.repo.mutate(ctx.task_id, ctx.instance_id,
                lambda value: value.setdefault("completion_directives", {}).__setitem__(ctx.event_id, deepcopy(result)))
        self.app.observe("mechanism_stage_changed", {"id": ctx.instance_id, "phase": result["kind"], "epoch": ctx.epoch, "revision": instance["revision"]})
        return result

    def publish_wait(self, directive):
        """Caller holds the same admission fence as user feedback."""
        if directive.get('decision_id'):
            return directive  # Existing durable pre-upgrade cards retain identity.
        from backend.core.loop.decision_support import create_user_decision
        process = self.app.process
        store = getattr(getattr(process, '_manager', None), 'session_store', None)
        if store is None:
            raise ValueError('MECHANISM_COMPLETION_STORE_REQUIRED')
        request = directive['request']
        pending = process.pending_decision
        if pending and pending.get('state_topic') != request['state_topic']:
            raise ValueError('MECHANISM_COMPLETION_DECISION_CHANGED')
        decision = pending or create_user_decision(process, request)
        store.save_process_checkpoint(process)
        result = {**directive, 'decision_id': decision['decision_id']}
        self.app.repo.mutate(self.app.task, directive['instance_id'],
            lambda value: value['completion_directives'].__setitem__(directive['event_id'], deepcopy(result)))
        return result

    def outcome(self, event_id, *, runs=None):
        identities = {run["instance_id"]: run["event_id"] for run in runs or []}
        results = [value.get("completion_directives", {}).get(identities.get(value["id"], event_id + ":g" + str(value["epoch"])))
            or value.get("completion_directives", {}).get(event_id)
            for value in self.app.repo.list_instances(self.app.task, limit=100)
            if value["phase"] in {"ready", "pending_activation", "activating"}]
        results = [value for value in results if value is not None]
        if not results:
            return None
        if any(result['kind'] == 'refresh' for result in results):
            return {'kind': 'refresh', 'evidence': [result.get('evidence', {}) for result in results]}
        waiting = next((result for result in results if result["kind"] == "awaiting_user"), None)
        if waiting:
            return waiting
        return {"kind": "rework", "instructions": [{"instruction": value["instruction"], "evidence": value["evidence"],
                "return_count": value["return_count"], "epoch": value["epoch"]} for value in results]}
