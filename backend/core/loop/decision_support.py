"""Deterministic decision shortcuts and structured user escalation.

The Host computes facts that do not require semantic judgement.  Agents only
interpret the remaining ambiguity, and a supervisor can pause for a material
user choice through a validated, resumable decision request.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from backend.core.loop.completion_protocol import HostCompletionEvidence
# Python import compatibility; arithmetic is owned by the workbench.
from backend.core.workbench.calculation import safe_calculate


DECISION_KINDS = {
    "clarification", "preference", "direction", "verification",
    "choice", "permission", "recovery", "checkpoint",
}
STATEFUL_DECISION_KINDS = {
    "preference", "direction", "verification", "checkpoint",
}


def _decision_position(process) -> dict:
    ledger = list(getattr(process.session, "host_ledger", []) or [])
    sequence = 1 + max((
        int(item.get("decision_sequence", 0) or 0)
        for item in ledger if item.get("event") == "user_decision_requested"
    ), default=0)
    return {
        "decision_sequence": sequence,
        "context_epoch": int(getattr(process.session, "context_epoch", 0) or 0),
        "message_sequence": len(process.session.messages),
    }


def collect_decision_evidence(process, focus: str = "all", *, identifier: str = "") -> dict:
    """Return compact Host-owned facts so the LLM does not manually aggregate them."""
    if focus == "guidance":
        from .recovery_guidance import lookup_guidance
        return lookup_guidance(identifier)
    if identifier:
        raise ValueError("identifier is only valid with focus=guidance")
    allowed = {
        "all", "completion", "gates", "children", "tests",
        "governance", "budget", "capabilities", "engineering",
    }
    if focus not in allowed:
        raise ValueError(f"focus must be one of: {', '.join(sorted(allowed))}")
    result: dict[str, Any] = {
        "focus": focus,
        "process_id": process.process_id,
        "task_kind": process.task_kind,
        "steps": {"used": process.steps_used, "maximum": process.max_steps},
    }
    if focus in {"all", "completion", "tests"}:
        evidence = HostCompletionEvidence.collect(process)
        result["completion"] = {
            **evidence.to_dict(),
            "scope": "current_task_action_evidence",
            "effectful_receipts_required": process.task_kind == "action",
            "task_id": process.active_task_id or process.process_id,
        }
    if focus in {"all", "completion", "gates"}:
        from backend.core.loop.completion_protocol import HostCompletionEvaluator
        context, _version = process.read_context_snapshot()
        result["outstanding_gates"] = HostCompletionEvaluator.outstanding_gates(
            process, list(context.get("signals") or []),
        )
    if focus in {"all", "engineering"}:
        from .engineering_workflow import EngineeringWorkflow
        result["engineering"] = EngineeringWorkflow(process).status()
    if focus in {"all", "children"}:
        manager = getattr(process, "_manager", None)
        child_ids, contracts, reviews = process.coordination_snapshot()
        result["children"] = [
            {
                "process_id": child_id,
                "status": (
                    manager.get(child_id).status.value
                    if manager is not None and manager.get(child_id) is not None
                    else "missing"
                ),
                "contract": contracts.get(child_id, {}),
                "review": reviews.get(child_id, {}),
            }
            for child_id in child_ids
        ]
    if focus in {"all", "governance"}:
        context, version = process.read_context_snapshot()
        from backend.core.loop.governance_projection import GovernanceProjection
        result["governance"] = {
            "context_version": version,
            "snapshot_digest": context.get("governance_digest", ""),
            "context_reference_error": context.get("governance_context_error"),
            "evidence_candidates": list(context.get("evidence_candidates") or []),
            "evidence_graph": dict(context.get("evidence_graph") or {}),
            "active_signals": [
                {**item.to_dict(), "resolution": getattr(process, "governance_resolutions", {}).get(item.signal_id)}
                for item in GovernanceProjection.normalize(context.get("signals") or [])
            ],
        }
    if focus in {"all", "capabilities"}:
        from backend.core.loop.capabilities import CapabilityProfiles
        result["capabilities"] = {
            "current_profile_id": process.capability_profile_id,
            "worker_profiles": CapabilityProfiles.describe(actor_kind="worker"),
            "delegated_task_kinds": ["answer", "plan", "action"],
            "authority": "Host registry; selecting a profile does not grant A self-execution",
        }
    if focus in {"all", "budget"}:
        result["budget"] = (
            process.task_budget.snapshot() if process.task_budget is not None else None
        )
        result["budget_card"] = (
            process.task_budget.decision_card(process.process_id)
            if process.task_budget is not None else None
        )
    return result


def create_user_decision(process, args: dict) -> dict:
    """Validate and publish one durable Agent-to-user decision request."""
    kind = str(args.get("kind") or "choice").strip()
    if kind not in DECISION_KINDS:
        raise ValueError("kind must be one of: " + ", ".join(sorted(DECISION_KINDS)))
    question = str(args.get("question", "")).strip()
    why = str(args.get("why_user_must_decide", "")).strip()
    if not question or not why:
        raise ValueError("question and why_user_must_decide are required")
    raw_options = args.get("options", []) or []
    compact = kind == "clarification"
    if not isinstance(raw_options, list) or not (0 <= len(raw_options) <= 3 if compact else 2 <= len(raw_options) <= 3):
        raise ValueError(
            "request_user_decision requires 2..3 business options; use the Host "
            "free-form Discuss/Amend affordance instead of adding a defer option"
        )
    state_topic = str(args.get("state_topic") or "").strip()
    if kind in STATEFUL_DECISION_KINDS and not state_topic:
        raise ValueError(
            f"state_topic is required for {kind}; provide a stable semantic topic "
            "so the newest confirmed answer supersedes stale state"
        )
    options = []
    labels: set[str] = set()
    recommended_count = 0
    required = (
        "label", "principle", "immediate_effect", "downstream_effect",
        "risks", "reversibility",
    )
    for raw in raw_options:
        if not isinstance(raw, dict):
            raise ValueError("every decision option must be an object")
        option = {name: str(raw.get(name, "")).strip() for name in required}
        if compact:
            option["description"] = str(raw.get("description") or raw.get("immediate_effect") or "").strip()
        missing = [name for name, value in option.items() if not value and (not compact or name == "label")]
        if missing:
            raise ValueError("decision option missing: " + ", ".join(missing))
        if option["label"] in labels:
            raise ValueError("decision option labels must be unique")
        labels.add(option["label"])
        option["recommended"] = bool(raw.get("recommended", False))
        if raw.get("action") and compact:
            raise ValueError("clarifications cannot carry authority actions")
        if raw.get("action"):
            option["action"] = str(raw.get("action"))
        recommended_count += int(option["recommended"])
        options.append(option)
    if recommended_count > 1:
        raise ValueError("at most one option may be recommended")
    request = {
        "decision_id": str(uuid.uuid4()),
        "kind": kind,
        "state_topic": state_topic,
        "supersedes_decision_id": str(args.get("supersedes_decision_id") or "").strip(),
        "question": question,
        "why_user_must_decide": why,
        "options": options,
        # Semantic options cannot force a user to endorse an unsuitable answer.
        # Receiving other text still grants no permission or resolved intent.
        "allow_free_form": (kind in {"clarification","preference","direction","choice"}
                            and not any(o.get("action") for o in options))
                           or bool(args.get("allow_free_form", True)),
        "created_at": datetime.now().isoformat(),
        **_decision_position(process),
        "task_id": process.active_task_id or process.process_id,
        "process_id": process.process_id,
    }
    questions = args.get("questions")
    if questions is not None:
        if kind != "clarification" or not isinstance(questions, list) or not 1 <= len(questions) <= 8:
            raise ValueError("a clarification round requires 1..8 questions")
        if len(json.dumps(questions, ensure_ascii=False).encode()) > 32_000:
            raise ValueError("clarification round exceeds 32KB")
        questions=[dict(item) if isinstance(item,dict) else item for item in questions]
        ids = set()
        for item in questions:
            if not isinstance(item, dict) or not item.get("node_id") or item["node_id"] in ids:
                raise ValueError("round questions require unique node_id values")
            ids.add(item["node_id"])
            if not item.get("question") or not item.get("state_topic") or item.get("kind") == "permission":
                raise ValueError("round questions require question, state_topic and non-permission kind")
            if item.get("kind", "clarification") not in {"clarification", "preference", "direction", "verification", "choice", "checkpoint"} or not item.get("why_user_must_decide"):
                raise ValueError("round questions require a supported kind and user-decision reason")
            if item.get("kind","clarification") in {"clarification","preference","direction","choice"}:
                item["allow_free_form"]=True
            choices = item.get("options") or []
            simple = item.get("kind", "clarification") == "clarification"
            if not isinstance(choices, list) or not (0 <= len(choices) <= 3 if simple else 2 <= len(choices) <= 3):
                raise ValueError("invalid round question options")
            if not choices and not item.get("allow_free_form", True):
                raise ValueError("open questions must allow free-form answers")
            if any(not isinstance(o, dict) or o.get("action") or not str(o.get("label") or "").strip()
                   or (not simple and any(not str(o.get(f) or "").strip() for f in required)) for o in choices):
                raise ValueError("invalid option or authority action in alignment round")
            if len({o["label"] for o in choices}) != len(choices) or sum(bool(o.get("recommended")) for o in choices) > 1:
                raise ValueError("round options must be unique with at most one recommendation")
        request["questions"] = questions
    from backend.core.loop.question_broker import route_question
    request = route_question(process, request)
    if isinstance(args.get("permission_request"), dict):
        request["permission_request"] = dict(args["permission_request"])
    with process._coordination_lock:
        if process.pending_decision is not None:
            raise RuntimeError("a user decision is already pending")
        process.pending_decision = request
    if process.task_budget is not None:
        process.task_budget.pause_for_user()
    process.session.host_ledger.append({"event": "user_decision_requested", **request})
    return {"status": "awaiting_user", **request}


def confirmed_user_state(ledger: list[dict], *, task_id: str | None = None) -> list[dict]:
    """Project the newest user-confirmed value for each semantic topic."""
    from .alignment_evidence import resolved_answer, source_answer
    if task_id is not None:
        ledger = [e for e in ledger if e.get('task_id') == task_id]
    requests = {
        str(item.get("decision_id")): item
        for item in ledger
        if item.get("event") == "user_decision_requested" and item.get("decision_id")
    }
    latest: dict[str, dict] = {}
    for event in ledger:
        if event.get('event') == 'user_decision_interpreted':
            topic = event.get('state_topic')
            source = source_answer(ledger, event.get('task_id'), event.get('decision_id'), event.get('source_node_id', ''))
            resolution = resolved_answer(ledger, source, topic) if source else None
            if resolution and topic:
                latest[topic] = {'topic': topic, 'kind': source.get('kind', 'clarification'),
                    'value': resolution['value'], 'disposition': resolution['disposition'],
                    'conditions': source['answer'], 'decision_id': source['decision_id'],
                    'confirmed_at': source.get('created_at', ''), 'supersedes_decision_id': ''}
            elif topic:
                latest.pop(topic, None)
            continue
        if event.get("event") != "user_decision_received":
            continue
        request = requests.get(str(event.get("decision_id") or ""), {})
        for item in request.get("questions") or []:
            response = (event.get("answers") or {}).get(item["node_id"])
            resolution = resolved_answer(ledger, {**event, **(response or {}), 'kind': item.get('kind', 'clarification')}, item['state_topic']) if response else None
            if resolution:
                latest[item["state_topic"]] = {
                    "topic": item["state_topic"], "kind": item.get("kind", "clarification"),
                    "value": resolution['value'], 'disposition': resolution['disposition'],
                    "conditions": response["answer"], "decision_id": event["decision_id"],
                    "confirmed_at": str(event.get("created_at") or ""),
                    "supersedes_decision_id": item.get("supersedes_decision_id", ""),
                }
            elif response:
                latest.pop(item['state_topic'], None)
        kind = str(request.get("kind") or "choice")
        from .alignment_evidence import CONTROL_DECISION_KINDS
        if kind in CONTROL_DECISION_KINDS:
            continue
        topic = str(request.get("state_topic") or "").strip()
        if not topic:
            continue
        resolution = resolved_answer(ledger, {**event, 'kind': kind}, topic)
        if not resolution:
            # A newer undecided answer supersedes old intent as well.
            latest.pop(topic, None)
            continue
        latest[topic] = {
            "topic": topic,
            "kind": kind,
            "value": resolution['value'], 'disposition': resolution['disposition'],
            "conditions": str(event.get("answer") or ""),
            "decision_id": str(event.get("decision_id") or ""),
            "confirmed_at": str(event.get("created_at") or ""),
            "supersedes_decision_id": str(request.get("supersedes_decision_id") or ""),
        }
    return sorted(latest.values(), key=lambda item: item["topic"])


def build_context_force_compaction_decision(process_id: str, task_id: str) -> dict:
    """One decision schema for live-loop and parked-session compaction."""
    request = {
        "decision_id": str(uuid.uuid4()),
        "kind": "context_force_compaction",
        "question": "Ordinary compaction failed three times. Force a lossy compaction?",
        "why_user_must_decide": (
            "The Host can preserve the full prior epoch in the local audit archive, "
            "but must delete part of the provider-visible history to continue."
        ),
        "options": [
            {
                "label": "Force compact",
                "principle": "Keep immutable policy/task prefixes and a bounded host checkpoint.",
                "immediate_effect": "Delete older provider-visible history and retry in a new epoch.",
                "downstream_effect": "The Agent continues with less conversational detail; the audit archive remains intact.",
                "risks": "Fine-grained details not captured by the checkpoint may need to be reopened from durable records.",
                "reversibility": "The provider context cannot be expanded in place, but the archived epoch remains inspectable.",
                "recommended": True,
                "action": "force_compact",
            },
            {
                "label": "Stop safely",
                "principle": "Do not discard model-visible history without approval.",
                "immediate_effect": "Stop the current task before another provider request.",
                "downstream_effect": "The task can be resumed after correcting the configured limit or reviewing the archive.",
                "risks": "Current work remains incomplete.",
                "reversibility": "A later task can continue from durable state.",
                "recommended": False,
                "action": "stop",
            },
        ],
        "allow_free_form": False,
        "created_at": datetime.now().isoformat(),
        "task_id": task_id or process_id,
        "process_id": process_id,
        "error_catalog_id": "GITGO-E5203",
    }
    return request


def create_context_force_compaction_decision(process) -> dict:
    """Publish the deterministic decision reached after three failed compactions."""
    request = build_context_force_compaction_decision(
        process.process_id, process.active_task_id or process.process_id,
    )
    request.update(_decision_position(process))
    with process._coordination_lock:
        if process.pending_decision is None:
            process.pending_decision = request
        else:
            request = process.pending_decision
    if process.task_budget is not None:
        process.task_budget.pause_for_user()
    process.session.host_ledger.append({"event": "user_decision_requested", **request})
    return {"status": "awaiting_user", **request}


def create_completion_exception_decision(process, gate_status: dict) -> dict:
    """Escalate a stable, repeatedly-unsatisfied completion checklist.

    Facts are never rewritten as successful. The user may accept only gates
    explicitly marked degradable, producing a distinct degraded outcome.
    """
    from .completion_gates import contract_digest
    gates = [dict(item) for item in list(gate_status.get("gates") or [])]
    all_degradable = bool(gates) and all(
        item.get("degradable_by_explicit_user_decision") is True for item in gates
    )
    middle = {
        "label": "Accept partial result" if all_degradable else "Change scope",
        "principle": (
            "Preserve the failed facts and accept an explicitly degraded delivery."
            if all_degradable else
            "Change the requested scope instead of pretending a required fact exists."
        ),
        "immediate_effect": (
            "Finish this task as degraded with every unresolved gate recorded."
            if all_degradable else
            "Stop this execution and return the preserved work for a new scope request."
        ),
        "downstream_effect": (
            "History and reports distinguish it from a verified completion."
            if all_degradable else
            "Submit a concrete new task scope; this decision does not waive a safety requirement."
        ),
        "risks": "Some requested verification or deliverables remain incomplete.",
        "reversibility": "A later task can satisfy the preserved gates.",
        "recommended": False,
        "action": "accept_partial" if all_degradable else "amend_scope",
    }
    request = {
        "decision_id": str(uuid.uuid4()),
        "kind": "completion_exception",
        "question": "The task still cannot pass the same completion checks. How should Gitgo proceed?",
        "why_user_must_decide": (
            "The Host supplied the exact checklist and one bounded recovery attempt "
            "did not change it. Continuing automatically would waste model work."
        ),
        "options": [
            {
                "label": "Continue and fix",
                "principle": "Keep the current scope and satisfy the Host facts.",
                "immediate_effect": "Resume the same process with the checklist attached.",
                "downstream_effect": "Completion remains fully verified if all gates pass.",
                "risks": "This may consume more time and provider budget.",
                "reversibility": "You can stop or accept a partial result later.",
                "recommended": True,
                "action": "continue",
            },
            middle,
            {
                "label": "Stop safely",
                "principle": "Do not spend more work on an unsatisfied contract.",
                "immediate_effect": "The Host stops this task without another model call and preserves its incomplete result.",
                "downstream_effect": "Durable evidence remains available for a later continuation.",
                "risks": "The requested task is not delivered in this run.",
                "reversibility": "A future task can continue from the saved state.",
                "recommended": False,
                "action": "stop",
            },
        ],
        "allow_free_form": True,
        "created_at": datetime.now().isoformat(),
        "task_id": process.active_task_id or process.process_id,
        "process_id": process.process_id,
        "error_catalog_id": "GITGO-E6105",
        "contract_digest": contract_digest(process),
        "completion_gate_ids": [str(item.get("gate_id") or "") for item in gates],
        "completion_gates": gates,
    }
    request.update(_decision_position(process))
    with process._coordination_lock:
        if process.pending_decision is None:
            process.pending_decision = request
        else:
            request = process.pending_decision
    if process.task_budget is not None:
        process.task_budget.pause_for_user()
    process.session.host_ledger.append({"event": "user_decision_requested", **request})
    return {"status": "awaiting_user", **request}


def format_decision_request(request: dict) -> str:
    if request.get("questions"):
        return "\n\n".join(f"{i + 1}. " + format_decision_request(q) for i, q in enumerate(request["questions"]))
    lines = [
        str(request.get("question", "")),
        "",
        "为什么需要你决定：" + str(request.get("why_user_must_decide", "")),
        "",
    ]
    for index, option in enumerate(request.get("options", []) or [], start=1):
        marker = "（建议）" if option.get("recommended") else ""
        if request.get("kind") == "clarification":
            lines.append(f"{index}. {option.get('label', '')}{marker}：{option.get('description') or option.get('immediate_effect') or ''}")
            continue
        lines.extend([
            f"{index}. {option.get('label', '')}{marker}",
            f"   原理：{option.get('principle', '')}",
            f"   立即效果：{option.get('immediate_effect', '')}",
            f"   后续影响：{option.get('downstream_effect', '')}",
            f"   风险：{option.get('risks', '')}",
            f"   可逆性：{option.get('reversibility', '')}",
        ])
    if request.get("allow_free_form", True):
        lines.extend(["", "你也可以直接说明自己的选择或补充条件。"])
    return "\n".join(lines).strip()


def validate_round_answer(pending: dict, instruction: str) -> dict:
    """Parse all answers before mutating pending state; identity comes from Host."""
    raw = json.loads(instruction)
    answers = raw.get("answers") if isinstance(raw, dict) else None
    questions = pending.get("questions") or []
    if not isinstance(answers, dict) or set(answers) != {q["node_id"] for q in questions}:
        raise ValueError("answer every question in the current round")
    result = {}
    for q in questions:
        value = answers[q["node_id"]]
        if not isinstance(value, str) or not value.strip() or len(value) > 8_000:
            raise ValueError("each round answer must be nonempty text of at most 8000 characters")
        value = value.strip()
        selected = None
        if value.startswith("Choose option "):
            index = value.removeprefix("Choose option ").split(":", 1)[0].strip()
            if not index.isdigit() or not 1 <= int(index) <= len(q.get("options") or []):
                raise ValueError("invalid round option")
            selected = q["options"][int(index) - 1]
        elif not q.get("allow_free_form", True):
            raise ValueError("this question requires a listed choice")
        result[q["node_id"]] = {"answer": value, "state_topic": q["state_topic"],
                                "selected_label": (selected or {}).get("label", "")}
    return result
