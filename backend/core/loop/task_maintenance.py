"""Task-owned mechanism, plan and incremental-scope lifecycle.

Semantic proposals come from the Agent. This module validates identities and
versions and projects existing task/workflow/permission/evidence authorities.
All durable changes use SessionStore; the mailbox remains the notification path.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib
import json

from .mechanism_catalog import CATALOG_VERSION, catalog, compile_definition, digest, enabled

KEY = "task_maintenance"
CONTROL_TOOLS = frozenset({"task_mechanism", "harness_mechanism", "declare_task_contract", "engineering_workflow", "request_user_decision",
    "request_permission", "decision_evidence", "capability_status", "configure_capability", "request_self_execute"})


def task_id(process):
    return process.active_task_id or process.process_id


def state_for(process):
    context, _ = process.read_context_snapshot()
    state = context.get(KEY) or {}
    return deepcopy(state) if state.get("task_id") == task_id(process) else {}


def source_receipts(process):
    result = {}
    canonical = [(index, e) for index, e in enumerate(process.session.host_ledger) if e.get("event") == "task_user_instruction_received" and e.get("task_id") == task_id(process)]
    if canonical:
        for index, event in canonical:
            result[event["source_ref"]] = {"kind": "user_message", "content": event["content"], "authority_sequence": event.get("authority_sequence", 0)}
    else:
        # Standalone legacy Agent runtimes do not have the application envelope.
        for message in process.session.messages:
            if message.get("role") == "user" and message.get("message_type", "conversation") == "conversation":
                content = str(message.get("content") or "")
                result["user:" + digest(content)] = {"kind": "user_message", "content": content}
    from .alignment_evidence import CONTROL_DECISION_KINDS
    for index, event in enumerate(process.session.host_ledger):
        if event.get("event") == "user_decision_received" and event.get("task_id") == task_id(process) and event.get("kind") not in CONTROL_DECISION_KINDS:
            result["decision:" + str(event["decision_id"])] = {"kind": "user_decision", "event": deepcopy(event), "authority_sequence": event.get("authority_sequence", 0)}
    for identifier, change in state_for(process).get("changes", {}).items():
        if change.get("user_adopted"):
            result["change:" + identifier] = {"kind": "adopted_change", "content": change["content"],
                "source_ref": change.get("source_ref"), "target_task_id": change["target_task_id"],
                "authority_sequence": change.get("authority_sequence", 0)}
    return {**state_for(process).get("source_receipts", {}), **result}


def _initial(process):
    return {"schema_version": 1, "task_id": task_id(process), "process_id": process.process_id,
        "revision": 0, "epoch": 0, "phase": "current", "changes": {}, "maintenance_items": {},
        "related": {"candidates": {}}, "plan": {}, "source_receipts": {}, "suspended": False}


def _plan(raw):
    if not isinstance(raw, dict) or set(raw) - {"summary", "steps", "dependencies"}:
        raise ValueError("plan requires summary, steps and optional declared dependencies")
    summary, steps = str(raw.get("summary") or "").strip(), raw.get("steps") or []
    if not summary or len(summary) > 4000 or not isinstance(steps, list) or not 1 <= len(steps) <= 64:
        raise ValueError("a plan needs a bounded summary and 1..64 actionable steps")
    nodes = {}
    for item in steps:
        if not isinstance(item, dict) or set(item) - {"id", "goal", "depends_on", "tools", "resources", "verification"}:
            raise ValueError("unsupported plan step; do not assert execution/completion as plan text")
        node = str(item.get("id") or "")
        if (not node or len(node) > 96 or node in nodes
                or any(not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 4000
                       for key in ("goal", "verification"))):
            raise ValueError("each unique plan step needs a goal and verification")
        for key in ("depends_on", "tools", "resources"):
            if not isinstance(item.get(key, []), list) or any(not isinstance(v, str) or len(v) > 500 for v in item.get(key, [])):
                raise ValueError("invalid plan step references")
        nodes[node] = {**deepcopy(item), "depends_on": list(item.get("depends_on") or [])}
    visited, active = set(), set()
    def visit(node):
        if node not in nodes or node in active:
            raise ValueError("plan dependency missing or cyclic")
        if node not in visited:
            active.add(node)
            for dependency in nodes[node]["depends_on"]:
                visit(dependency)
            active.remove(node)
            visited.add(node)
    for node in nodes:
        visit(node)
    dependencies = raw.get("dependencies") or []
    if not isinstance(dependencies, list) or len(dependencies) > 64:
        raise ValueError("too many plan dependencies")
    for dep in dependencies:
        if not isinstance(dep, dict) or set(dep) - {"kind", "id", "affected_tools"} or dep.get("kind") not in {"file", "tool", "lesson"} or not dep.get("id"):
            raise ValueError("dependencies must identify actual file/tool/lesson sources")
        if (not isinstance(dep["id"], str) or len(dep["id"]) > 500
                or not isinstance(dep.get("affected_tools", []), list)
                or any(not isinstance(name, str) or not name or len(name) > 128 for name in dep.get("affected_tools", []))):
            raise ValueError("invalid dependency identity or affected tool set")
        if dep["kind"] == "file":
            _dependency_parts(dep["id"])
    return {"summary": summary, "steps": list(nodes.values()), "dependencies": deepcopy(dependencies)}


def _related(raw):
    if (not isinstance(raw, dict) or set(raw) - {"summary", "candidates"}
            or not isinstance(raw.get("summary"), str) or not raw["summary"].strip() or len(raw["summary"]) > 4000):
        raise ValueError("related review requires a reasoned summary, including when no extra work is needed")
    candidates = raw.get("candidates") or []
    if not isinstance(candidates, list) or len(candidates) > 32:
        raise ValueError("related work candidates must be bounded")
    result = {}
    required = ("source", "affected", "reason", "consequence", "work", "verification", "owner")
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            raise ValueError(f"related_review.candidates[{index}] must be an object")
        unknown = set(item) - {*required, "evidence_kind"}
        if unknown:
            raise ValueError(f"related_review.candidates[{index}] unsupported fields: " + ', '.join(sorted(unknown)))
        invalid = [k for k in required if not isinstance(item.get(k), str) or not item[k].strip() or len(item[k]) > 4000]
        if invalid:
            raise ValueError(f"related_review.candidates[{index}] requires bounded nonempty fields: " + ', '.join(invalid))
        if item.get("evidence_kind") not in {"observed", "declared", "inferred"}:
            raise ValueError("related candidate evidence_kind must separate observation from inference")
        identifier = "related-" + digest({k: item[k] for k in required})[:20]
        result[identifier] = {**deepcopy(item), "id": identifier, "status": "proposed",
                              "provenance": "agent_proposal", "semantic_claim_verified": False}
    return {"summary": str(raw["summary"]), "candidates": result}


def prepare_contract_state(process, contract, proposal, workflow):
    """Pure preparation, installed with the task contract's one checkpoint."""
    before = state_for(process)
    if not before and not contract.get("mechanism") and not process.runtime_preferences.get("task_maintenance_enabled"):
        return None
    state = before or _initial(process)
    definition = contract.get("mechanism") or state.get("definition")
    if not definition and not state.get("detached") and process.runtime_preferences.get("task_maintenance_enabled"):
        refs = source_receipts(process)
        if refs:
            definition = compile_definition({"intent": contract.get("goal") or process.task_description,
                "source_refs": list(refs)[-1:], "elements": [{"use": k} for k in
                    ("task.plan", "task.discussion", "task.related_work", "task.lesson")]})
            contract["mechanism"] = definition
    if definition:
        if definition.get("catalog_version") != CATALOG_VERSION:
            raise ValueError("MECHANISM_CATALOG_CHANGED")
        receipts = source_receipts(process)
        if any(ref not in receipts for ref in definition["source_refs"]):
            raise ValueError("mechanism source must refer to actual current-task user evidence; copy exact source_refs from: " + json.dumps(list(receipts)))
        state["source_receipts"].update({ref: receipts[ref] for ref in definition["source_refs"]})
        old_uses = {n["use"] for n in (state.get("definition") or {}).get("elements", [])}
        new_uses = {n["use"] for n in definition["elements"]}
        if old_uses - new_uses and (state.get("phase") == "needs_review" or any(c["status"] == "pending" for c in state["changes"].values())):
            raise ValueError("cannot detach a mechanism while its accepted obligations await reconciliation")
        state["definition"] = deepcopy(definition)
        if proposal.get("mechanism"):
            state["detached"] = False
    update = proposal.get("maintenance") or {}
    if not isinstance(update, dict):
        raise ValueError("maintenance update must be an object")
    if len(json.dumps(update, default=str).encode()) > 96000:
        raise ValueError("maintenance proposal exceeds task control-plane budget")
    if update and (type(update.get("expected_revision")) is not int or update["expected_revision"] != state["revision"]):
        raise ValueError("TASK_MAINTENANCE_REVISION_CONFLICT: maintenance.expected_revision must be " + str(state["revision"]))
    if set(update) - {"expected_revision", "plan", "plan_review", "related_review", "resolve_changes", "resolve_maintenance", "adopt_related", "related_dispositions"}:
        raise ValueError("unknown maintenance update")
    plan_review = update.get('plan_review')
    if plan_review is not None:
        if (not isinstance(plan_review, dict) or set(plan_review) != {'action', 'reason'}
                or plan_review['action'] != 'retain' or not isinstance(plan_review['reason'], str)
                or not plan_review['reason'].strip() or len(plan_review['reason']) > 4000):
            raise ValueError('PLAN_REVIEW_INVALID: retain requires your reasoned review of the existing plan')
        if update.get('plan') is not None:
            raise ValueError('PLAN_REVIEW_CONFLICT: supply a revised plan or retain it, not both')
        if not state.get('plan', {}).get('steps'):
            raise ValueError('PLAN_REVIEW_MISSING_PLAN: declare a plan first')
        state['plan'] = {**state['plan'], 'revision': state['plan']['revision'] + 1,
                         'contract_revision': contract['revision']}
        state['plan_review'] = {**deepcopy(plan_review), 'plan_revision': state['plan']['revision'],
            'contract_revision': contract['revision'], 'maintenance_ids': list(update.get('resolve_maintenance') or [])}
    if update.get("plan") is not None:
        state["plan"] = {**_plan(update["plan"]), "revision": state.get("plan", {}).get("revision", 0) + 1,
                         "contract_revision": contract["revision"]}
    if update.get("related_review") is not None:
        review = _related(update["related_review"])
        previous_candidates = state.get("related", {}).get("candidates", {})
        review["candidates"] = {**previous_candidates, **{
            key: {**value, **{field: previous_candidates[key][field] for field in ("status", "contract_revision", "adopted_after_authority_sequence", "disposition")
                            if field in previous_candidates.get(key, {})}}
            for key, value in review["candidates"].items()}}
        if len(review["candidates"]) > 128:
            raise ValueError("related work history exceeds bounded task capacity")
        state["related"] = review
    adopted = update.get("adopt_related") or []
    for identifier in adopted:
        item = state.get("related", {}).get("candidates", {}).get(identifier)
        if item is None:
            raise ValueError("unknown related candidate")
        if item["status"] not in {"proposed", "adopted"}:
            raise ValueError("a rejected/withdrawn candidate cannot silently become accepted again")
        if item['status'] == 'adopted':
            # Restating an already accepted identity is not another adoption.
            # Retain its original scope/effect provenance rather than demanding
            # a fresh plan and moving the execution baseline on every revision.
            continue
        if not update.get("plan") or not contract.get("acceptance_criteria"):
            raise ValueError("adopting related work must revise the plan and acceptance criteria")
        item.update(status="adopted", contract_revision=contract["revision"],
                    adopted_after_authority_sequence=process.session.authority_sequence)
    dispositions = update.get("related_dispositions") or []
    if (not isinstance(dispositions, list) or len(dispositions) > 32
            or any(not isinstance(r, dict) or set(r) - {"id", "action", "reason", "source_ref", "quote"} for r in dispositions)):
        raise ValueError("related dispositions must be bounded, typed decisions")
    for resolution in dispositions:
        item = state["related"].get("candidates", {}).get(resolution.get("id"))
        if not item or not str(resolution.get("reason") or "").strip():
            raise ValueError("related disposition needs an actual candidate and reason")
        if resolution.get("action") == "reject" and item["status"] == "proposed":
            item.update(status="rejected", disposition=deepcopy(resolution), contract_revision=contract["revision"])
        elif resolution.get("action") == "withdraw" and item["status"] == "adopted" and update.get("plan"):
            _withdrawal_source(process, resolution, after=item.get("adopted_after_authority_sequence", 0))
            item.update(status="withdrawn", disposition=deepcopy(resolution), contract_revision=contract["revision"])
        else:
            raise ValueError("only proposals may be rejected; accepted work needs a user-sourced withdrawal and revised plan")
    resolutions = update.get("resolve_changes") or []
    if not isinstance(resolutions, list) or len(resolutions) > 64:
        raise ValueError("change resolutions must be bounded")
    for item in resolutions:
        change = state["changes"].get(item.get("id")) if isinstance(item, dict) else None
        withdraw_adopted = isinstance(item, dict) and item.get("disposition") == "withdraw" and change and change["status"] == "adopted"
        if not change or (change["status"] != "pending" and not withdraw_adopted) or change["target_task_id"] != task_id(process):
            raise ValueError("change resolution must identify this task's pending adopted request")
        if not str(item.get("reason") or "").strip() or not update.get("plan") or not contract.get("acceptance_criteria"):
            raise ValueError("adoption requires a revised plan, acceptance criteria and reconciliation reason")
        fields = item.get("contract_fields") or []
        if not fields or any(f not in {"goal", "deliverables", "acceptance_criteria", "uncertainties", "engineering_workflow"} for f in fields):
            raise ValueError("adoption must identify the actual contract fields reconciled by the Agent")
        questions = item.get("question_ids") or []
        known = {n["id"] for n in (workflow or {}).get("plan", {}).get("nodes", []) if n["kind"] == "decision"}
        if any(q not in known for q in questions):
            raise ValueError("discussion links must reuse actual workflow decision ids")
        disposition = item.get("disposition", "adopt")
        if disposition not in {"adopt", "withdraw"}:
            raise ValueError("invalid change disposition")
        if disposition == "withdraw":
            _withdrawal_source(process, item, after=change.get("authority_sequence", 0))
        change.update(status="withdrawn" if disposition == "withdraw" else "adopted", resolution=deepcopy(item), contract_revision=contract["revision"],
                      plan_revision=state["plan"]["revision"])
    for identifier in update.get("resolve_maintenance") or []:
        item = state["maintenance_items"].get(identifier)
        if item is None or item.get("status") != "pending":
            raise ValueError("maintenance resolution requires an actual pending trigger id")
        if not update.get("plan") and not plan_review:
            raise ValueError("maintenance requires a revised plan or plan_review={action:retain,reason:...} when the current plan still covers the trigger")
        source = item.get("source", "")
        retained = {dep["kind"] + ":" + dep["id"] for dep in state["plan"].get("dependencies", [])}
        if source in retained and str(state.get("dependency_versions", {}).get(source, "")).startswith("unavailable:"):
            raise ValueError("cannot mark an unavailable retained dependency as reconciled")
        item.update(status="resolved", contract_revision=contract["revision"], attempts=item.get("attempts", 0) + 1)
    state["revision"] += 1
    state["epoch"] += 1
    pending = any(c["status"] == "pending" for c in state["changes"].values()) or any(c["status"] == "pending" for c in state["maintenance_items"].values())
    needs_plan = enabled(definition, "task.plan") is not None and (not state["plan"] or state.get("review_requirements", {}).get("plan", False)) and not (update.get("plan") or plan_review)
    needs_related = enabled(definition, "task.related_work") is not None and (not state.get("related", {}).get("summary") or state.get("review_requirements", {}).get("related", False)) and not update.get("related_review")
    # A changed goal/acceptance basis requires another semantic review, not a text append.
    from .task_contract import semantic_contract, get_task_contract
    def scope_basis(value):
        # Reconciliation commands are audit input, not new requirements. Their
        # changing plan/status must still invalidate Grill's delivery review,
        # but must not manufacture another scope-change review of themselves.
        # Definition/control revisions have their own lifecycle validation.
        # They are not new business scope; newly enabled plan/related-work
        # elements are checked by needs_plan/needs_related above.
        return digest({k: v for k, v in semantic_contract(value).items()
                       if k not in {"maintenance_intent", "mechanism"}})
    previous_contract = get_task_contract(process)
    previous_basis = scope_basis(previous_contract) if before and previous_contract.get("revision") else None
    basis = scope_basis(contract)
    changed = bool(previous_basis and previous_basis != basis)
    if changed and plan_review:
        raise ValueError('PLAN_REVIEW_SCOPE_CHANGED: changed business scope requires an explicit revised plan')
    if changed and not update.get("plan"):
        needs_plan = True
    if changed and not update.get("related_review"):
        needs_related = True
    state["contract_basis"] = basis
    state["phase"] = "needs_review" if pending or needs_plan or needs_related else "ready"
    state["review_requirements"] = {"plan": needs_plan, "related": needs_related}
    state["manifest"] = {"epoch": state["epoch"], "task_id": task_id(process), "contract_revision": contract["revision"],
        "contract_digest": digest(semantic_contract(contract)), "catalog_version": CATALOG_VERSION,
        "definition_digest": (definition or {}).get("digest"), "plan_revision": state["plan"].get("revision", 0),
        "workflow_digest": (workflow or {}).get("plan", {}).get("digest"), "phase": state["phase"],
        "task_kind": process.task_kind, "capability_profile_id": process.capability_profile_id}
    state["manifest"]["digest"] = digest(state["manifest"])
    return state


def _withdrawal_source(process, resolution, *, after=0):
    receipt = source_receipts(process).get(resolution.get("source_ref"), {})
    content = receipt.get("content") or (receipt.get("event") or {}).get("answer") or ""
    quote = resolution.get("quote")
    if (not isinstance(quote, str) or not quote.strip() or quote not in content
            or receipt.get("authority_sequence", 0) <= after):
        raise ValueError("withdrawal needs an exact quote from newer actual task-user evidence; Grill still assesses its meaning")


def _commit(process, state, event, store=None):
    from .mechanism_lifecycle import commit_activation, owner
    if process.session.mechanism_activation_blocked or owner(process).session.mechanism_activation_blocked:
        raise ValueError("MECHANISM_ACTIVATION_BLOCKED")
    before, version = process.context_snapshot, process.context_version
    prior_epoch = ((before or {}).get(KEY) or {}).get("epoch", -1)
    if state["epoch"] <= prior_epoch:
        state["epoch"] = prior_epoch + 1
    if state.get("manifest"):
        manifest = {key: value for key, value in state["manifest"].items() if key != "digest"}
        manifest.update(epoch=state["epoch"], phase=state["phase"], definition_digest=(state.get("definition") or {}).get("digest"))
        state["manifest"] = {**manifest, "digest": digest(manifest)}
    process.context_snapshot = {**(before or {}), KEY: deepcopy(state)}
    process.context_version += 1
    added_control = bool(state.get("allow_contract_reconciliation") and process.tool_registry is not None
                         and not process.tool_registry.has("declare_task_contract"))
    if added_control:
        process.tool_registry.add_scoped("declare_task_contract")
    def rollback():
        process.context_snapshot, process.context_version = before, version
        if added_control:
            process.tool_registry.remove_scoped("declare_task_contract")
    store = store or getattr(getattr(process, "_manager", None), "session_store", None)
    if store is not None:
        commit_activation(process, {"event": event, "task_id": task_id(process), "process_id": process.process_id,
                                   "maintenance_revision": state["revision"]}, store, rollback)
    else:
        process.session.host_ledger.append({"event": event, "task_id": task_id(process), "persistence": "memory_only"})


def add_change(process, content, *, identifier, source_ref, expected_task_id, expected_contract_revision, store=None, source_contract_revision=None, user_adopted=True):
    from .mechanism_lifecycle import fence
    from .task_contract import get_task_contract
    with fence(process), process.session._mechanism_lock, process._context_lock:
        state = state_for(process) or _initial(process)
        if identifier in state["changes"]:
            previous = state["changes"][identifier]
            if previous["source_ref"] != source_ref or previous["content"] != content:
                raise ValueError("ADOPTION_IDENTITY_CONFLICT")
            return deepcopy(previous)
        if task_id(process) != expected_task_id or get_task_contract(process).get("revision", 0) != expected_contract_revision:
            raise ValueError("ADOPTION_TARGET_CHANGED")
        if process.status.value not in {"running", "waiting", "awaiting_user"}:
            raise ValueError("ADOPTION_TARGET_ENDED")
        if not content.strip() or len(content) > 64000 or len(state["changes"]) >= 128:
            raise ValueError("adopted changes exceed bounded task capacity")
        change = {"id": identifier, "status": "pending", "user_adopted": user_adopted, "source_ref": source_ref,
            "content": content, "target_process_id": process.process_id, "target_task_id": task_id(process),
            "base_contract_revision": expected_contract_revision, "source_contract_revision": source_contract_revision,
            "requires_rebase": source_contract_revision is not None and source_contract_revision != expected_contract_revision}
        from .mechanism_lifecycle import next_authority_sequence
        change["authority_sequence"] = next_authority_sequence(process.session)
        state["changes"][identifier] = change
        if process.actor_kind != "supervisor":
            # Explicit user adoption mounts only the existing scope-revision
            # control. It grants no worker capabilities, resources or permissions.
            state["allow_contract_reconciliation"] = True
        state["phase"] = "needs_review"
        state["revision"] += 1
        state["epoch"] += 1
        _commit(process, state, "task_change_admitted", store)
        return deepcopy(change)


def is_suspended(process):
    """Read lifecycle state across the actual task ownership chain."""
    manager = getattr(process, "_manager", None)
    while process is not None:
        if state_for(process).get("suspended"):
            return True
        process = manager.get(process.parent_id) if manager and process.parent_id else None
    return False


def guard(process, tool_name=None, *, inherited=False):
    state = state_for(process)
    manager = getattr(process, "_manager", None)
    parent = manager.get(process.parent_id) if manager and process.parent_id else None
    if parent is not None and tool_name not in CONTROL_TOOLS:
        parent_block = guard(parent, tool_name, inherited=True)
        if parent_block:
            from backend.core.mechanism_runtime.reviews import retained_report
            retained = (tool_name is None and parent_block[0] == "TASK_MAINTENANCE_REQUIRED"
                and retained_report(process, parent))
            if not retained:
                return parent_block[0], "Shared supervisor requirement awaits reconciliation: " + parent_block[1]
    if not state or tool_name in CONTROL_TOOLS:
        return None
    if state.get("suspended"):
        return "TASK_MECHANISM_SUSPENDED", "Task mechanism is suspended; retain obligations and explicitly resume it."
    if (state.get("definition") or {}).get("catalog_version", CATALOG_VERSION) != CATALOG_VERSION:
        return "TASK_MAINTENANCE_REQUIRED", "A registered mechanism dependency changed; inspect and recompile the affected task."
    if state.get("phase") == "needs_review":
        pending = [v for v in state.get("maintenance_items", {}).values() if v.get("status") == "pending"]
        broad = any(v.get("status") == "pending" for v in state.get("changes", {}).values()) or any(state.get("review_requirements", {}).values())
        if tool_name is not None and pending and not broad and all(v.get("affected_tools") for v in pending):
            if tool_name not in {name for item in pending for name in item["affected_tools"]}:
                return None
        missing = [name + " review" for name, required in state.get("review_requirements", {}).items() if required]
        missing += [v["id"] for v in pending]
        missing += [v["id"] for v in state.get("changes", {}).values() if v.get("status") == "pending"]
        return "TASK_MAINTENANCE_REQUIRED", ("Reconcile adopted requirements, related work and the plan with declare_task_contract before this action/delivery. "
            "Outstanding: " + ", ".join(missing[:16]) + ". Inspect task_mechanism status.")
    projection = getattr(process, "_mechanism_projection", {})
    # Parent obligations are shared, but a parent's local tools/prompt
    # acknowledgement cannot be a prerequisite for its child to finish:
    # the parent may be waiting for that very child. Each executing consumer
    # acknowledges its own projection at its next admission boundary.
    if not inherited and state.get("manifest") and projection.get("epoch") != state["epoch"]:
        return "TASK_MANIFEST_NOT_READY", "The current tools and prompt have not acknowledged this task generation yet."
    return None


def inspect(process):
    from .mechanism_lifecycle import fence
    with fence(process), process.session._mechanism_lock, process._context_lock:
        return _inspect_current(process)


def _inspect_current(process):
    from .task_contract import get_task_contract
    state = state_for(process)
    from .completion_protocol import _current_task_receipts
    from .operation_policy import is_effectful_mutation
    receipts = [r for r in _current_task_receipts(process) if r.get("committed") is True and r.get("succeeded") is True
                and r.get("tool_name") not in CONTROL_TOOLS and is_effectful_mutation(r.get("effect", "read"))]
    completed = bool(getattr(process, "lifecycle_finalized", False)
                     and ((getattr(process, "result", None) or {}).get("outcome") or {}).get("status") == "completed")
    for candidate in state.get("related", {}).get("candidates", {}).values():
        if candidate["status"] == "adopted":
            relevant = [r["receipt_id"] for r in receipts if r.get("receipt_id")
                        and r.get("task_contract_revision", 0) >= candidate.get("contract_revision", 0)]
            candidate["execution"] = {"scope": "containing_task", "task_id": task_id(process),
                "state": "task_completed" if completed else "task_in_progress" if relevant else "awaiting_execution",
                "receipt_ids": relevant}
    from .mechanisms import projection
    owned_updates = []
    manager = getattr(process, "_manager", None)
    if manager is not None:
        for child in manager.children_of(process.process_id):
            child_state = state_for(child)
            if child_state.get("changes"):
                owned_updates.append({"process_id": child.process_id, "task_id": task_id(child),
                    "contract": get_task_contract(child), "changes": child_state["changes"],
                    "plan_revision": child_state.get("plan", {}).get("revision", 0),
                    "phase": child_state.get("phase"), "provenance": "owned_task_authoritative_state"})
    return {"task_id": task_id(process), "process_id": process.process_id, "contract": get_task_contract(process),
        "advancement": projection(process),
        "user_control_precedence": "Persisted advancement.user_control is a real user selection. Its authority_sequence supersedes earlier source receipts about advancement only. A pinned definition is not permission to reset a newer selection.",
        "state": state, "owned_scope_updates": owned_updates,
        "source_refs": list(source_receipts(process)), "catalog_version": CATALOG_VERSION,
        "extensions": __import__("backend.core.mechanism_runtime.application", fromlist=["projection"]).projection(process),
        "consumer_projection": deepcopy(getattr(process, "_mechanism_projection", {})),
        "authority_granted": False}


def model_context(process):
    """Live operational state without recursively embedding observer digests.

    Full audit/source records remain available through task_mechanism.status.
    This projection is ephemeral; the durable state and gates are untouched.
    """
    from .model_feedback import task_view
    return task_view(inspect(process))


def alignment_basis(process):
    state = state_for(process)
    return {key: state.get(key) for key in ("definition", "changes", "plan", "related", "dependency_versions")}


def _dependency_parts(name):
    from backend.core.sandbox_read import relative_parts
    from backend.core.tools.file_operations import protected_metadata_part
    parts = relative_parts(name)
    if any(protected_metadata_part(part) for part in parts):
        raise ValueError("task dependencies must be business inputs, not repository/product metadata")
    return parts


def _file_dependency(root, name):
    # Use the existing descriptor-bound reader. Resolving then reopening a
    # model-selected path would lose the sandbox's anti-link-swap boundary.
    _dependency_parts(name)
    from backend.core.sandbox_read_plan import open_planned_read
    try:
        with open_planned_read(root, name) as opened:
            content = opened.read_bytes(max_bytes=1000000)
            return hashlib.sha256(content).hexdigest()
    except FileNotFoundError:
        return "missing"


def dependency_snapshot(process, state, tools):
    """Read only declared inputs; changes in unrelated sources do not replan."""
    from .permission_broker import tool_contract_digests
    from backend.core.knowledge.review import content_version
    root = Path(process.worktree_path).resolve()
    storage = getattr(process, "runtime_storage", None) or getattr(getattr(process, "_manager", None), "storage", None)
    result = {}
    tool_ids = {dep["id"] for dep in state.get("plan", {}).get("dependencies", []) if dep["kind"] == "tool"}
    tool_versions = tool_contract_digests({name: tools[name] for name in tool_ids if name in tools}) if tool_ids else {}
    for dep in state.get("plan", {}).get("dependencies", []):
        key = dep["kind"] + ":" + dep["id"]
        if dep["kind"] == "file":
            try:
                result[key] = _file_dependency(root, dep["id"])
            except (OSError, ValueError) as exc:
                result[key] = "unavailable:" + type(exc).__name__
        elif dep["kind"] == "tool":
            result[key] = tool_versions.get(dep["id"], "missing")
        else:
            found = storage.get_lesson_record(dep["id"]) if storage is not None else None
            result[key] = content_version(found[1]) if found else "missing"
    for identifier in state.get("observed_lessons", {}):
        found = storage.get_lesson_record(identifier) if storage is not None else None
        result["lesson:" + identifier] = content_version(found[1]) if found else "missing"
    manager = getattr(process, "_manager", None)
    if manager is not None:
        for child in manager.children_of(process.process_id):
            changes = state_for(child).get("changes", {})
            if changes:
                result["child_scope:" + child.process_id] = digest({key: {k: value.get(k) for k in
                    ("content", "source_ref", "target_task_id")} for key, value in changes.items()})
    return result


def synchronize(process, tools):
    from .mechanism_lifecycle import fence
    with fence(process), process.session._mechanism_lock, process._context_lock:
        state = state_for(process)
        if not state:
            return False
        current = dependency_snapshot(process, state, tools)
        previous = state.get("dependency_versions")
        state["dependency_versions"] = current
        unavailable = {key for key, value in current.items() if value.startswith("unavailable:")}
        declared = {dep["kind"] + ":" + dep["id"] for dep in state.get("plan", {}).get("dependencies", [])}
        # A newly mounted dependency establishes its baseline. Only revisions
        # of already observed sources, or newly unavailable sources, invalidate
        # the plan. Retiring a binding does not itself change the source.
        invalidated = {key for key, value in current.items()
                       if (key in (previous or {}) and previous[key] != value)
                       or (key not in (previous or {}) and (key in unavailable
                           or (previous is not None and key not in declared)))}
        from .mechanisms import snapshot
        advancement = snapshot(process)
        bindings = {"task_kind": process.task_kind, "capability_profile_id": process.capability_profile_id,
            "advancement_revision": advancement["revision"], "advancement_mode": advancement["mode"],
            "governance_digest": process.context_snapshot.get("governance_digest")}
        rebound = bool(state.get("manifest")) and any(state["manifest"].get(key) != value for key, value in bindings.items())
        if rebound:
            state["manifest"].update(bindings)
        if invalidated:
            for source, revision in current.items():
                if source in invalidated:
                    identifier = "maintenance-" + digest([source, (previous or {}).get(source), revision, state["plan"].get("revision", 0)])[:20]
                    state["maintenance_items"].setdefault(identifier, {"id": identifier, "source": source,
                        "source_revision": revision, "previous_revision": (previous or {}).get(source), "status": "pending", "attempts": 0,
                        "affected_tools": next((dep.get("affected_tools", []) for dep in state["plan"].get("dependencies", [])
                            if dep["kind"] + ":" + dep["id"] == source), [])})
            state["phase"] = "needs_review"
            state["revision"] += 1
            state["epoch"] += 1
        if previous != current or rebound:
            _commit(process, state, "task_dependencies_reconciled")
        return previous != current or rebound


def observe_result(process, result):
    if result.tool_name in CONTROL_TOOLS:
        return
    state = state_for(process)
    if not state or enabled(state.get("definition"), "task.plan") is None:
        return
    feedback = (result.data or {}).get("lesson_feedback") or {}
    diagnostic_lessons = [d for d in feedback.get("diagnostics", []) if d.get("lesson_id")]
    # Admission/runtime failures do not establish a failed project assumption.
    # Keep their original diagnostics and recovery path, without turning a
    # missing capability or internal bug into another business-scope revision.
    internal = (result.receipt.get("diagnostic_scope") == "gitgo_runtime"
                or result.diagnostics.get("nature") in {"system", "governance"}
                or result.diagnostics.get('code') in {
                    'INVALID_TOOL_ARGUMENTS', 'TASK_CONTRACT_REVISION_CONFLICT',
                    'TOOL_RESULT_LOCATOR_INVALID',
                })
    plan_failure = result.is_error and not internal
    if not diagnostic_lessons and not plan_failure:
        return
    from .mechanism_lifecycle import fence
    with fence(process), process.session._mechanism_lock, process._context_lock:
        state = state_for(process)
        if diagnostic_lessons:
            from backend.core.knowledge.review import content_version
            storage = getattr(process, "runtime_storage", None) or getattr(getattr(process, "_manager", None), "storage", None)
            for diagnostic in diagnostic_lessons:
                identifier = diagnostic["lesson_id"]
                found = storage.get_lesson_record(identifier) if storage is not None else None
                current = content_version(found[1]) if found else "missing"
                raw = deepcopy(found[1].to_dict() if hasattr(found[1], "to_dict") else found[1]) if found else {}
                for key in ("trigger_count", "applied_count", "violated_after_count", "recent_retrievals"):
                    raw.pop(key, None)
                same = found is not None and diagnostic.get("lesson_version") == hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                state.setdefault("observed_lessons", {})[identifier] = {"feedback_id": feedback.get("feedback_id"),
                    "reported_version": diagnostic.get("lesson_version"), "version": current if same else "changed_since_feedback"}
                state.setdefault("dependency_versions", {})["lesson:" + identifier] = current if same else "changed_since_feedback"
        identifier = "failure-" + str(result.receipt.get("receipt_id") or digest(result.diagnostics))
        if plan_failure and identifier not in state["maintenance_items"]:
            state["maintenance_items"][identifier] = {"id": identifier, "source": "tool_result",
                "receipt_id": result.receipt.get("receipt_id"), "tool_name": result.tool_name,
                "status": "pending", "attempts": 0}
            state["phase"] = "needs_review"
            state["revision"] += 1
            _commit(process, state, "task_result_requires_plan_review")
        elif diagnostic_lessons:
            state["revision"] += 1
            _commit(process, state, "task_lesson_dependencies_observed")


def invoke(process, args, *, user_control=False):
    """Existing Agent control-plane adapter; no tool or sandbox authority grants."""
    operation = args.get("operation", "status")
    if operation == "catalog":
        return catalog()
    if operation == "status":
        return inspect(process)
    if operation == "preview":
        from .task_contract import validate_contract_proposal, preview_contract, semantic_contract
        proposal = validate_contract_proposal(args.get("contract") or {}, process.worktree_path)
        before = inspect(process)
        candidate = preview_contract(process, proposal)
        return {"base_revision": before["contract"].get("revision", 0), "contract": candidate,
            "behavior_diff": {k: {"before": semantic_contract(before["contract"]).get(k), "after": v}
                for k, v in semantic_contract(candidate).items() if semantic_contract(before["contract"]).get(k) != v},
            "preview_digest": digest(candidate), "authority_granted": False}
    if operation == "interpret_update":
        from .mechanism_lifecycle import fence
        with fence(process), process.session._mechanism_lock, process._context_lock:
            state = state_for(process)
            item = state.get("changes", {}).get(args.get("change_id"))
            if (not item or item["status"] != "pending" or item["user_adopted"]
                    or type(args.get("expected_revision")) is not int or args["expected_revision"] != state["revision"]):
                raise ValueError("only the exact pending informational update can be interpreted here")
            if args.get("disposition") not in {"information", "question", "scope_change"} or not str(args.get("reason") or "").strip():
                raise ValueError("update interpretation needs a disposition and semantic reason")
            if args["disposition"] == "scope_change":
                return {"status": "pending", "next_action": "declare_task_contract", "change": item}
            item.update(status="acknowledged", interpretation={"disposition": args["disposition"], "reason": args["reason"]})
            unresolved = any(v["status"] == "pending" for v in state["changes"].values()) or any(v["status"] == "pending" for v in state["maintenance_items"].values())
            state["phase"] = "needs_review" if unresolved or any(state.get("review_requirements", {}).values()) else "ready"
            state["revision"] += 1
            _commit(process, state, "task_update_interpreted")
            return inspect(process)
    if operation in {"suspend", "resume", "detach"}:
        from .mechanism_lifecycle import fence
        with fence(process), process.session._mechanism_lock, process._context_lock:
            state = state_for(process)
            if not state or type(args.get("expected_revision")) is not int or args["expected_revision"] != state["revision"]:
                raise ValueError("TASK_MAINTENANCE_REVISION_CONFLICT")
            if operation == "detach":
                from .engineering_workflow import EngineeringWorkflow
                if state["phase"] == "needs_review" or EngineeringWorkflow(process).completion_reasons():
                    raise ValueError("cannot detach unresolved accepted obligations; use existing scope amendment")
                state["definition"] = None
                state["detached"] = True
                state["suspended"] = False
            else:
                control = state.get("lifecycle_control") or {}
                if operation == "resume" and control.get("operation") == "suspend" and not user_control:
                    _withdrawal_source(process, args, after=control["authority_sequence"])
                state["suspended"] = operation == "suspend"
            if user_control:
                from .mechanism_lifecycle import next_authority_sequence
                state["lifecycle_control"] = {"origin": "user_command", "operation": operation,
                    "authority_sequence": next_authority_sequence(process.session)}
            state["revision"] += 1
            state["epoch"] += 1
            _commit(process, state, "task_mechanism_" + operation)
            return inspect(process)
    raise ValueError("unknown task mechanism operation")
