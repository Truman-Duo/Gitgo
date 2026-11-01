"""Independent read-only review using the existing owned-child lifecycle."""
from copy import deepcopy
import json
from time import monotonic


def review_description(ctx, payload):
    """Keep unchanged evidence before per-attempt identities, without omission.

    A new independent reviewer must see the current candidate and generation.
    Those fields used to precede every receipt, invalidating the entire shared
    evidence prefix even when the underlying work had not changed.
    """
    from backend.core.loop.review_evidence import inline_review_evidence
    evidence = {"instructions": payload["instructions"], **inline_review_evidence(payload['event']),
        "candidate": payload["event"].get("candidate", ""), "task_id": ctx.task_id,
        "instance_id": ctx.instance_id, "epoch": ctx.epoch, "event_id": ctx.event_id}
    return ("Independently review only this current task result and supplied evidence. "
        "You have read-only tools. Do not modify files, run commands/tests, install anything, change scope, "
        "request more authority or implement a fix. Inspect the declared result files when needed. "
        "Call complete_review with approved or changes_required and concrete evidence. "
        "The receipt inventory is bounded; receipt_source identifies the immutable full evidence snapshot. "
        "Use tool_result_open with that locator and a receipt's json_pointer (or query/paging) for verification details. "
        "A receipt's result_json_pointers identify retained actual output bodies, including command argv/stdout/stderr when present. "
        "Use those bodies to verify what ran; a command hash or exit code alone is not proof of a particular test. "
        "Absence from the inventory is not proof that no evidence exists. Never use a read operation's exit code as a test result. "
        "The inventory is an exact runtime projection, not the main model's self-report; use its facts when sufficient. "
        "observed_diffs are retained committed patches, including pre-compaction changes; they are not full before-files. "
        "Read a patch's json_pointer for a truncated preview, /artifacts for witnessed current files, and /harness_semantics for observed task control. "
        "Use these supplied sources for before/after comparisons. If the required baseline is genuinely absent, report that specific gap; "
        "do not repeatedly hunt unrelated backups or ask the owner to re-create already retained evidence. "
        "The task's original mandatory completion checks remain in force.\n\n" + json.dumps(evidence, ensure_ascii=False))


def validate_boundary(owner, origin):
    """Validate a trusted lifecycle adapter's actual running generation."""
    from .application import bound_application
    application = bound_application(owner)
    if application is None or origin.get("owner_process_id") != owner.process_id or origin.get("owner_task_id") != application.task:
        raise ValueError("RETAINED_REVIEW_BOUNDARY_INVALID")
    instance, _ = application.repo.instance(application.task, origin.get("instance_id"))
    run = (instance or {}).get("runs", {}).get((instance or {}).get("active_run"))
    if (not run or run["phase"] != "running" or run["boundary"] != "completion" or run["epoch"] != origin.get("epoch")
            or run["event_id"] != origin.get("event_id")):
        raise ValueError("RETAINED_REVIEW_BOUNDARY_INVALID")


def retained_report(process, owner):
    """An old read-only stage may report; the owner must recheck new scope.

    This only defers the owner's newer semantic maintenance to its next
    completion boundary. Own evidence, cancellation, permissions and live
    suspension are still enforced by their existing authorities.
    """
    from .application import bound_application
    from backend.core.loop.task_maintenance import state_for, CATALOG_VERSION
    origin = process.runtime_preferences.get("retained_review_boundary") or {}
    if (process.actor_kind != "reviewer" or process.capability_profile_id != "review.independent"
            or process.review_target_id != owner.process_id or origin.get("owner_process_id") != owner.process_id
            or origin.get("owner_task_id") != owner.active_task_id):
        return False
    state = state_for(owner)
    if state.get("suspended") or (state.get("definition") or {}).get("catalog_version", CATALOG_VERSION) != CATALOG_VERSION:
        return False
    app = bound_application(owner)
    instance, _ = app.repo.instance(app.task, origin.get("instance_id")) if app else (None, None)
    run = (instance or {}).get("runs", {}).get((instance or {}).get("active_run"))
    review = (instance or {}).get("reviews", {}).get(origin.get("event_id"), {})
    return bool(run and run["phase"] == "running" and run["boundary"] == "completion" and run["epoch"] == origin.get("epoch")
        and run["event_id"] == origin.get("event_id") and review.get("reviewer_process_id") == process.process_id
        and review.get("epoch") == origin.get("epoch"))


def review_service(bindings, application):
    def review(ctx, payload):
        ctx.check()
        if ctx.boundary != "completion" or bindings.spawn_child is None or bindings.manager is None:
            raise ValueError("MECHANISM_REVIEW_RUNTIME_UNAVAILABLE")
        process = bindings.process
        files = list(getattr(process.completion_claim, "files", []) or [])
        description = review_description(ctx, payload)
        allowed = ["read_file", "search_text", "artifact_read", "context_open", "context_search", "tool_result_open", "decision_evidence", "publish_diff"]
        child = bindings.spawn_child(task_description=description, profile_id="review.independent", task_kind="review",
            max_steps=payload["max_steps"], target_files=files, acceptance_criteria=["Provide an evidence-backed complete_review verdict"],
            allowed_tools=allowed, required_for_parent_completion=False, actor_kind="reviewer", review_target_id=process.process_id,
            review_evidence_source=payload['event'].get('receipt_source'),
            retained_boundary={"owner_process_id": process.process_id, "owner_task_id": ctx.task_id,
                "instance_id": ctx.instance_id, "epoch": ctx.epoch, "event_id": ctx.event_id})
        def record(value):
            from backend.core.loop.alignment_evidence import answer_digest
            value.setdefault("reviews", {})[ctx.event_id] = {"reviewer_process_id": child.process_id,
                "epoch": ctx.epoch, "phase": "reviewing", "read_only_tools": allowed, "created_at": child.created_at,
                "candidate_digest": answer_digest(payload['event'].get('candidate', '')),
                "definition_ref": value['effective_ref']}
        application.repo.mutate(ctx.task_id, ctx.instance_id, record)
        application.observe("mechanism_stage_changed", {"id": ctx.instance_id, "phase": "reviewing", "epoch": ctx.epoch, "revision": 0})
        bindings.manager.wait(child.process_id, timeout=max(0.0, ctx.deadline - monotonic()), cancel_event=process.cancellation_event)
        if process.cancel_requested or process.cancellation_event.is_set():
            bindings.manager.kill(child.process_id, reason="mechanism_review_cancelled")
            ctx.check()
        if child.status.value in {"running", "waiting", "awaiting_user", "cancelling"}:
            bindings.manager.kill(child.process_id, reason="mechanism_review_timeout")
            raise TimeoutError("MECHANISM_REVIEW_TIMEOUT")
        claim = deepcopy(child.review_claim or {})
        if child.status.value != "completed" or claim.get("verdict") not in {"approved", "changes_required"}:
            raise ValueError("MECHANISM_REVIEW_INCOMPLETE: " + child.status.value)
        result = {**claim, "reviewer_process_id": child.process_id, "review_epoch": ctx.epoch,
                  "authority_granted": False, "read_only_tools": allowed}
        def complete(value):
            value["reviews"][ctx.event_id].update(phase="complete", result=deepcopy(result))
        application.repo.mutate(ctx.task_id, ctx.instance_id, complete)
        return result
    return review
