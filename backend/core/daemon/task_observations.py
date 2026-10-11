"""Checkpoint barriers before observation publication.

State failures propagate; trace failures are visible but do not execute effects.
The observer transports callbacks without becoming an authorization authority.
"""
class TaskObservations:
    def __init__(self, process, manager, store, trace, queue):
        self.process, self.manager, self.store = process, manager, store
        self.trace, self.queue = trace, queue

    def persist(self, event):
        event = dict(event)
        event.setdefault("causation_id", str(event.get("tool_call_id") or
            self.process.runtime_preferences.get("runtime_command_id") or ""))
        detail = event.pop("_trace_detail", None)
        # Persist only semantic safe boundaries, never token deltas.
        # These points capture complete provider continuation state,
        # committed tool receipts, decisions, or context epochs.
        if event.get("event") in {
            "governance_snapshot",
            "mailbox_applied",
            "provider_response_completed",
            "provider_response_incomplete",
            "tool_result",
            "decision_required",
            "context_compaction_completed",
            "agent_started",
            "agent_dag_admitted",
            "worktree_leased",
            "worktree_sealed",
            "worktree_promoted",
            "agent_terminal",
            "coordination_event",
            "coordination_event_resolved",
        }:
            store = self.store
            if store is not None:
                event_process_id = str(event.get("process_id") or "")
                event_process = (
                    self.manager.get(event_process_id) if event_process_id else None
                ) or self.process
                previous_finality = event_process.lifecycle_finalized
                if event.get("event") == "agent_terminal":
                    event_process.lifecycle_finalized = True
                try:
                    store.save_process_checkpoint(event_process)
                except BaseException:
                    event_process.lifecycle_finalized = previous_finality
                    raise
                # Delegation mutates both the child row and A's durable
                # coordination contract.  Persist both at admission so
                # a daemon crash before the first Provider response
                # cannot erase an already-started B or its DAG edges.
                if event.get("event") == "agent_started" and event_process.parent_id:
                    owner = self.manager.get(event_process.parent_id)
                    if owner is not None:
                        store.save_process_checkpoint(owner)
                if event.get("event") == "agent_dag_admitted":
                    for child_id in dict(event.get("nodes") or {}).values():
                        child = self.manager.get(str(child_id))
                        if child is not None:
                            store.save_process_checkpoint(child)
                if event.get("event") in {
                    "coordination_event", "coordination_event_resolved",
                }:
                    related_ids = list(dict.fromkeys([
                        str(event.get("owner_process_id") or ""),
                        str(event.get("source_process_id") or ""),
                        *[
                            str(item) for item in
                            list(event.get("affected_process_ids") or [])
                        ],
                    ]))
                    for related_id in related_ids:
                        related = self.manager.get(related_id) if related_id else None
                        if related is not None:
                            store.save_process_checkpoint(related)
        try:
            event = self.trace.append(event, detail=detail)
        except Exception as trace_exc:
            event["trace_error"] = str(trace_exc)[:300]
        self.queue.put(event)
