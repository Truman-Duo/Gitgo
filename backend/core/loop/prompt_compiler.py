"""Provider-neutral, versioned system prompt compiler.

The compiler owns policy sections. Provider adapters own wire encoding only.
Dynamic values are escaped as data and never promoted to higher authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from backend.core.loop.provider_protocol import deterministic_hash


PROMPT_MESSAGE_TYPE = "compiled_system_prompt"
TASK_CONTRACT_MESSAGE_TYPE = "compiled_task_contract"
PROMPT_SCHEMA_VERSION = 7


@dataclass(frozen=True)
class PromptSection:
    name: str
    content: str
    provenance: str
    version: str = "1"


class PromptCompiler:
    """Compile stable policy plus task-scoped runtime data in a fixed order."""

    @classmethod
    def compile(
        cls,
        *,
        process,
        tools: dict,
        workspace_path: str,
        governance_brief: str = "",
    ) -> tuple[str, list[PromptSection]]:
        actor = getattr(process, "actor_kind", "worker")
        profile = getattr(process, "capability_profile_id", "text.only")
        task_kind = getattr(process, "task_kind", "answer")
        lease = getattr(process, "capability_lease", None)
        model_id = str(getattr(process, "model_id", "") or "unknown")

        identity = {
            "supervisor": (
                "You are the A-level project supervisor. You plan, delegate, coordinate "
                "and review, but delegation is not a goal by itself. For bounded, "
                "single-responsibility work, explicitly request a self-execution lease "
                "and finish it directly. Delegate when context isolation, parallel work, "
                "independent ownership, or a genuinely large task makes a B useful. You "
                "may not perform effectful work unless the host has "
                "issued a task-scoped self-execution lease after your explicit request. "
                f"You are running inside the Gitgo harness with model {model_id!r}. "
                "If asked about your model or runtime identity, report that exact "
                "Host-provided model identifier and state that it is operating inside "
                "Gitgo. Never hide an available identifier or guess an unavailable vendor."
            ),
            "reviewer": (
                "You are an independent reviewer. Inspect evidence and report defects; "
                "do not modify the work under review."
            ),
        }.get(actor, (
            "You are a B-level worker. Execute the assigned task within its contract, "
            "report evidence, and escalate conflicts. You cannot approve your own work."
        ))
        if task_kind == "answer":
            identity = (
                "You are Gitgo, a capable AI assistant in a project workspace. "
                "Respond to the user's actual message naturally and directly. Do not "
                "volunteer internal agent levels, governance procedures, completion "
                "state, workspace status, or project diagnostics unless the user asks "
                "for them or they are necessary to answer. A greeting should receive "
                "a brief greeting, not a task report. "
                f"The active model is {model_id!r} and it is operating inside the Gitgo "
                "harness. If asked about the model, answer with both facts: report that "
                "exact Host-provided identifier and state that it is operating inside the "
                "Gitgo harness. Never hide an available identifier or guess an inaccessible "
                "vendor or version detail."
            )

        lease_text = "none"
        if lease is not None:
            lease_text = (
                f"{lease.lease_id} for profile {lease.profile_id}; "
                f"actions={', '.join(lease.intended_actions)}"
            )

        completion = {
            "answer": (
                "Answer the user's actual message directly when no project workflow is "
                "needed. The Host exposes read-only tools for evidence. For any non-trivial "
                "workflow, the single initial workflow action is declare_task_contract: "
                "semantically interpret the request and call it once; do not use keyword "
                "matching and do not request execution authority or delegation first. Record "
                "concrete artifacts and whether delegation itself is a requirement. The Host "
                "then compiles one route and exposes only the appropriate explicit "
                "self-execution or delegation path. A "
                "workflow attempt makes the Host "
                "promote this turn to the governed supervisor/action completion contract. "
                "The final answer is a user deliverable, not an execution log. Never state "
                "the number of searches, fetches, retries, model turns, validations or gates, "
                "and never announce that data was re-fetched, unless the user explicitly asks "
                "for a process audit. Do not list inaccessible pages, HTTP/provider errors, "
                "search fallbacks, or sources you chose not to use when enough usable evidence "
                "exists. Cite useful sources and disclose only uncertainty that materially "
                "limits a requested conclusion. Match the response length to "
                "the request instead of proving diligence through internal detail. "
                "For stable general knowledge already within your confidence, answer in "
                "the current model turn; do not search merely to prove carefulness. Omit "
                "or qualify an uncertain optional detail instead of expanding the scope "
                "into research, and respect an explicit request for a direct answer. "
                "Treat quoted or user-provided text literally unless the user asks to "
                "decode, repair, or reconstruct hidden source text. If verification is "
                "needed, make a bounded evidence call promptly instead of spending the "
                "output budget reverse-engineering an unstated transformation. "
                "A non-empty assistant response without tool calls is terminal at the next "
                "safe Host boundary; before promotion, do not call complete_supervision and "
                "do not add TASK_COMPLETE."
            ),
            "supervisor": (
                "For questions, return a substantiated answer. For delegated delivery, "
                "wait for every required B outcome, inspect its evidence, record an "
                "explicit structured approval, then call complete_supervision once "
                "with the final synthesis; the Host closes the task if its facts pass. "
                "If a required B fails, record changes_required and call the same "
                "tool with a failure report; the Host derives the failed terminal state. "
                "A standalone TASK_COMPLETE line is only a compatibility fallback. Use Host "
                "shortcuts for calculable/aggregatable facts. If a material product or "
                "architecture preference remains genuinely ambiguous, call "
                "request_user_decision with 2-3 business options and state each option's principle, "
                "immediate effect, downstream effect, risks, and reversibility. "
                "Routing rule: default to self-execution for bounded one-owner work, but "
                "routing is stateful and may evolve: "
                "work that is clearly complex or parallel may create B immediately; if "
                "bounded work grows, revise the task contract and hand it to B with a "
                "concise evidence-backed handoff. Do not use rigid file/token thresholds. "
                "When the request continues or revises an artifact previously owned by "
                "a B, continue that B by process_id under owner routing; create a fresh B "
                "only for a distinct responsibility, explicit fresh-routing preference, "
                "or justified parallelism. Do not create a B merely because you are A."
            ),
            "action": (
                "When the work is ready, call complete_task for a rich semantic claim; "
                "if the Host facts pass, that call is terminal and no repeated final "
                "declaration is needed. Its result field is the actual final answer the "
                "user will receive, not an execution report: omit search attempts, "
                "provider failures, governance gates, receipts and validation narration "
                "unless they materially limit the result or the user requested an audit. "
                "Put internal evidence in verification. A standalone TASK_COMPLETE line remains a "
                "fallback for providers that cannot call tools. "
                "The Host attaches receipts and required-test facts independently; do "
                "not repeat successful actions merely to create evidence."
            ),
            "plan": (
                "Return a concrete, reviewable plan and finish with a standalone "
                "TASK_COMPLETE line."
            ),
            "review": (
                "Cite inspectable evidence and call complete_review with the verdict. "
                "If the Host accepts it, the call is terminal and no repeated final "
                "declaration is needed."
            ),
        }.get(task_kind, (
            "Provide a non-empty final answer and finish with a standalone "
            "TASK_COMPLETE line."
        ))

        tool_lines = []
        for name in sorted(tools):
            tool = tools[name]
            effect = getattr(tool, "effect", "read")
            cancellation = getattr(tool, "cancellation", "cooperative")
            effect = getattr(effect, "value", effect)
            cancellation = getattr(cancellation, "value", cancellation)
            tool_lines.append(f"- `{name}`: effect={effect}; cancellation={cancellation}")
        if not tool_lines:
            tool_lines = ["- No tools are available for this task."]

        safe_workspace = str(Path(workspace_path)) if workspace_path else "unspecified"
        context, _context_version = process.read_context_snapshot()
        task_contract = dict(context.get("task_contract") or {})
        adaptive_route_guidance = ""
        if task_kind == "answer" and task_contract.get("revision"):
            execution_mode = str(
                task_contract.get("execution_mode") or "answer"
            ).strip().lower()
            if execution_mode == "self_execute":
                if lease is None:
                    adaptive_route_guidance = (
                        "\nThe Host has accepted a self_execute contract. The current "
                        "surface intentionally withholds effectful tools until you make "
                        "the required explicit request_self_execute call. This staged "
                        "surface is not evidence that command, file or test capability is "
                        "unsupported. Call request_self_execute now; use profile "
                        "development.workspace for files, commands or tests and describe "
                        "the intended actions. After the lease is issued, use the rebuilt "
                        "tool surface to perform the work. Do not ask the user to run the "
                        "operation merely because the pre-lease surface lacks the tool. "
                    )
                else:
                    adaptive_route_guidance = (
                        "\nThe Host has accepted a self_execute contract and the explicit "
                        "lease is active. Perform the delivery with the leased tools; do "
                        "not request the lease again or claim that a pre-lease tool list "
                        "describes the current capability surface. "
                    )
            elif execution_mode in {"delegate", "either"}:
                adaptive_route_guidance = (
                    "\nThe Host has accepted a delegated workflow contract. Follow its "
                    "routing advice using the exposed delegation path; do not request a "
                    "self-execution lease merely because direct effectful tools are absent. "
                )
            else:
                adaptive_route_guidance = (
                    "\nThe Host has accepted a response-only contract. Continue with the "
                    "answer and do not request mutation authority. "
                )
            adaptive_route_guidance += (
                "A contract flag about unresolved user decisions applies only to a real "
                "semantic or product choice. Capability staging is resolved by the Host "
                "route above and is not a reason to ask the user or abandon the task."
            )
        upstream = []
        for raw in list(context.get("upstream_outcomes") or []):
            item = dict(raw or {})
            outcome = dict(item.get("outcome") or {})
            metadata_text = json.dumps(
                outcome.get("metadata", {}),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            upstream.append({
                "process_id": item.get("process_id", ""),
                "task_id": item.get("task_id", ""),
                "status": item.get("status", ""),
                "result_commit": item.get("result_commit", ""),
                "response": str(outcome.get("response") or "")[:4000],
                "error": outcome.get("error"),
                # Upstream metadata may contain provider/tool diagnostics that
                # grow without bound.  Preserve a deterministic evidence
                # summary while keeping the task-pinned prompt cacheable.
                "metadata_summary": metadata_text[:4000],
            })
        contract_payload = {
            "goal": str(task_contract.get("goal") or ""),
            "execution_mode": str(task_contract.get("execution_mode") or ""),
            "delegation_required": bool(task_contract.get("delegation_required", False)),
            "user_requested_delegation": bool(
                task_contract.get("user_requested_delegation", False)
            ),
            "user_request_evidence": str(
                task_contract.get("user_request_evidence") or ""
            ),
            "agent_required_delegation": bool(
                task_contract.get("agent_required_delegation", False)
            ),
            "minimum_delegated_outcomes": int(
                task_contract.get("minimum_delegated_outcomes", 0) or 0
            ),
            "deliverables": list(task_contract.get("deliverables") or []),
            "acceptance_criteria": list(task_contract.get("acceptance_criteria") or []),
            "target_files": list(task_contract.get("target_files") or []),
            "required_test_ids": list(task_contract.get("required_test_ids") or []),
            "depends_on": list(task_contract.get("depends_on") or []),
            "input_interfaces": list(task_contract.get("input_interfaces") or []),
            "output_interfaces": list(task_contract.get("output_interfaces") or []),
            "interface_contract": dict(task_contract.get("interface_contract") or {}),
            "relationship_policy": dict(
                task_contract.get("relationship_policy")
                or context.get("relationship_policy")
                or {}
            ),
            "uncertainties": list(task_contract.get("uncertainties") or []),
            "requires_user_decision": bool(
                task_contract.get("requires_user_decision", False)
            ),
            "routing_transition": str(task_contract.get("routing_transition") or "initial"),
            "handoff_summary": str(task_contract.get("handoff_summary") or ""),
            "delegation_attempts": list(
                task_contract.get("delegation_attempts") or []
            )[-8:],
            "upstream_outcomes": upstream,
            "engineering_workflow": dict(task_contract.get("engineering_workflow") or {}),
        }
        from backend.core.loop.decision_support import confirmed_user_state
        confirmed_state = confirmed_user_state(
            list(getattr(process.session, "host_ledger", []) or [])
        )
        sections = [
            PromptSection("Base identity", identity, "host:role-contract"),
            PromptSection(
                "Behavior and delivery standard",
                (
                    "Treat the user's input, intended outcome, produced work, and review "
                    "evidence with care, diligence, and honesty. Never claim actions, "
                    "tests, certainty, or completion that the evidence does not support. "
                    "When producing user-facing or product-facing wording, default to a "
                    "rational, restrained, non-sycophantic style: do not flatter, but do "
                    "not invent faults merely to sound critical. Simple interface symbols "
                    "are acceptable when useful; do not use emoji. Deliver work to a high "
                    "quality standard, prefer maintainable long-term solutions, and never "
                    "lower quality merely to finish sooner. Behavior standards govern "
                    "your work; they are not product copy or deliverable requirements. "
                    "Separate three audiences: internal execution evidence belongs in "
                    "structured receipts/reviews; the user-facing final response states "
                    "the delivered result, location/use and material unresolved limits; "
                    "the artifact itself contains only content relevant to its audience. "
                    "Do not insert contract jargon, internal process IDs, receipt IDs, "
                    "hashes or compliance checklists into a final answer or product unless "
                    "the user requests an audit or those details are necessary. Never "
                    "turn an instruction into a claim about implementation: privacy "
                    "filtering does not prove no local data is stored, and source "
                    "inspection does not prove rendered layout or visual correctness. "
                    "Historical tool failures describe their original execution; inspect "
                    "current Host facts before claiming the same limitation still exists."
                ),
                "host:behavior-standard",
            ),
            PromptSection(
                "Role and authority",
                f"actor_kind={actor}\nprofile={profile}\nself_execute_lease={lease_text}",
                "host:capability-policy",
            ),
            PromptSection(
                "Capabilities",
                "\n".join(tool_lines),
                "host:effective-tool-registry",
            ),
        ]
        sections.append(PromptSection(
            "User collaboration protocol",
            (
                "Classify questions as clarification, preference, direction, verification, "
                "choice, recovery, checkpoint, or permission. First inspect Host facts and "
                "the latest confirmed user state; never ask the user to calculate, copy, or "
                "look up information a Host shortcut can supply. Ask when product judgement, "
                "forgotten or stale intent, uncertainty, or a material tradeoff remains. "
                "Product work may use several incremental questions at natural milestones; "
                "do not force every question into one initial interview. Any A or B may call "
                "request_user_decision; the Host routes the card to the user's current A/B "
                "view and projects a compact question/answer summary to A. Do not manually "
                "copy or relay a B question through A. For a tool already on your surface, "
                "call it once: the Host automatically allows low-risk operations and pauses "
                "sensitive ones before execution. Do not preflight a normal tool call with "
                "request_permission. Use request_permission only after a structured Host error "
                "prescribes it for an out-of-scope resource or capability expansion. Lead with "
                "the intended outcome and user impact; the Host supplies API and scope details."
            ),
            "host:collaboration-policy",
        ))
        if task_kind != "answer" or actor in {"worker", "reviewer"}:
            sections.append(PromptSection(
                "Agent relationship and communication protocol",
                (
                    "A owns task boundaries, approvals and user-facing judgement. B owns its "
                    "assigned workstream and may publish structured dependency, interface, "
                    "blocking or stale-intent events to A through the Host. B Agents never "
                    "private-message one another. For a DAG edge, the Host distributes a "
                    "versioned upstream handoff and any later interface-revision proposal to "
                    "A and affected downstream B Agents; downstream execution waits until A "
                    "accepts, requests rework, or asks the user. Use publish_interface_update "
                    "after changing a declared output boundary and escalate_to_supervisor for "
                    "semantic exceptions. Do not copy messages manually or invent peer state."
                ),
                "host:agent-relationship-policy",
            ))
        if task_kind == "answer":
            # The initial adaptive turn stays outside the project-governance
            # envelope.  A Host-observed workflow tool event promotes the task
            # and recompiles this section at the next safe boundary.
            sections.extend([
                PromptSection(
                    "Runtime environment",
                    f"workspace={safe_workspace}; use only when relevant to the question",
                    "host:runtime",
                ),
                PromptSection(
                    "Interaction contract",
                    (
                        f"task_kind=answer\n{completion}\n"
                        f"{adaptive_route_guidance}\n"
                        "Do not imitate status-heavy or lifecycle-oriented replies from "
                        "older conversation history when the current message is ordinary "
                        "conversation. Treat the current request as the presentation boundary: "
                        "do not call it a retry, rerun or renewed fetch merely because similar "
                        "work exists in history. Do not inspect or summarize the workspace unless "
                        "the user's question requires project evidence."
                    ),
                    "host:task-contract",
                ),
            ])
        else:
            sections.extend([
                PromptSection(
                    "Project governance context",
                    governance_brief.strip() or "No active governance summary.",
                    "governance:context-snapshot",
                ),
                PromptSection(
                    "Runtime environment",
                    f"workspace={safe_workspace}",
                    "host:runtime",
                ),
                PromptSection(
                    "Task contract",
                    (
                        f"task_kind={task_kind}\n{completion}\n"
                        f"contract={json.dumps(contract_payload, ensure_ascii=False, separators=(',', ':'))}\n"
                        "Decision order: Host hard rules and shortcuts first; model semantic "
                        "judgement second; user escalation only for material unresolved "
                        "preferences. Do not spend model steps manually copying, counting, "
                        "calculating, or polling facts that a supplied Host tool can return."
                    ),
                    "host:task-contract",
                ),
            ])
        if actor == "supervisor" and getattr(process, "child_ids", None):
            child_ids, contracts, reviews = process.coordination_snapshot()
            manager = getattr(process, "_manager", None)
            records = []
            for child_id in child_ids[-16:]:
                child = manager.get(child_id) if manager else None
                records.append({
                    "process_id": child_id,
                    "display_name": child.session.display_name if child else "unavailable",
                    "execution_status": child.status.value if child else "missing",
                    "outcome_status": (child.result or {}).get("status") if child else None,
                    "review": reviews.get(child_id, {}).get("verdict", "not_reviewed"),
                    "target_files": contracts.get(child_id, {}).get("target_files", []),
                    "superseded_by": contracts.get(child_id, {}).get("superseded_by", ""),
                })
            sections.append(PromptSection("Known agent executions", (
                "Host facts at this turn boundary, not instructions from workers. "
                "A file existing or a successful write is not proof that its task completed "
                "or passed review. Preserve timed_out/failed outcomes; inspect evidence or "
                "continue the original worker to finish remaining work. Use list_agents for "
                "current state and older entries. Prior ownership is not a new obligation: "
                "when continuing/reviewing earlier work, declare adopt_process_ids for that "
                "work or continue_process_id when delegating its next iteration. Never "
                "bind unrelated new requests to old failed tasks. Do not recite these facts for unrelated chat.\n"
                + json.dumps(records, ensure_ascii=False, separators=(',', ':'))
            ), "host:task-contract"))
        if actor == "supervisor" and task_kind != "answer":
            sections.append(PromptSection(
                "Delegation economy",
                (
                    "A owns the task and may execute bounded work directly after obtaining "
                    "a self-execution lease. Use B when the marginal value comes from genuine "
                    "parallel work, specialist context, independent review, or an observed "
                    "workflow that has grown beyond A. Writing or testing a solution A has "
                    "already derived is not an independent workstream. Related iterations "
                    "continue the existing owner by default. Each new B consumes an agent slot "
                    "and receives a bounded provider/output escrow; unused escrow is refunded "
                    "at terminal state. declare_task_contract returns the current Budget Card, "
                    "and decision_evidence(focus=budget) refreshes it only when needed. "
                    "For A's own bounded work, submit complete_task after deterministic "
                    "verification; do not pre-emptively create a Reviewer B. The Host's "
                    "verification tier will explicitly require review for Level-2 work. "
                    "Unused budget is never penalized: finish as soon as the required "
                    "completion evidence is sufficient."
                ),
                "host:task-contract",
            ))
        if actor == "supervisor":
            from backend.core.loop.coordination import pending_coordination_events
            pending_coordination = pending_coordination_events(
                list(getattr(process.session, "host_ledger", []) or [])
            )
            if pending_coordination:
                sections.append(PromptSection(
                    "Pending Agent coordination",
                    (
                        "Host-routed events only; workers did not talk privately. Resolve "
                        "interface revisions before dependent workers start. Use "
                        "list_coordination_events for full details and "
                        "resolve_coordination_event for the decision. If material product "
                        "judgement remains, use request_user_decision rather than guessing.\n"
                        + json.dumps(
                            pending_coordination[-16:], ensure_ascii=False,
                            separators=(",", ":"), default=str,
                        )
                    ),
                    "host:coordination-events",
                ))
        if confirmed_state:
            sections.append(PromptSection(
                "Latest user-confirmed state",
                (
                    "Host projection of the newest confirmed value per topic. Older values "
                    "for the same topic are superseded; ask a verification question if new "
                    "evidence makes one stale. Do not recite this internal state unless useful.\n"
                    + json.dumps(confirmed_state, ensure_ascii=False, separators=(",", ":"))
                ),
                "host:user-confirmed-state",
            ))
        text = "\n\n".join(
            f"## {section.name}\n{section.content}" for section in sections
        )
        return text, sections

    @staticmethod
    def upsert(session, text: str, sections: list[PromptSection]) -> None:
        """Seed a stable ROM and a task-pinned contract as separate prefixes.

        The previous implementation put project governance and workspace data in
        the same system message as the runtime constitution.  That made the
        nominal "stable prefix" task-specific and prevented cross-task ROM cache
        reuse.  Sent prefixes are still never rewritten; later changes become
        append-only Host steering deltas.
        """
        session.messages[:] = [
            message for message in session.messages
            if message.get("message_type") != "governance_context_seed"
        ]
        stable_names = {
            # Only policy that is byte-stable across task admission, leases and
            # actor routes belongs in the provider cache prefix.  Identity,
            # role and effective capabilities all change during answer ->
            # supervisor -> action transitions and therefore belong to the
            # task-pinned prefix below.
            "Behavior and delivery standard", "User collaboration protocol",
        }
        stable_sections = [item for item in sections if item.name in stable_names]
        task_sections = [item for item in sections if item.name not in stable_names]

        def render(items: list[PromptSection]) -> str:
            return "\n\n".join(
                f"## {section.name}\n{section.content}" for section in items
            )

        stable_text = render(stable_sections)
        task_text = render(task_sections)
        stable_hash = deterministic_hash({
            "schema": PROMPT_SCHEMA_VERSION,
            "text": stable_text,
            "sections": [
                (item.name, item.provenance, item.version) for item in stable_sections
            ],
        })
        prompt_hash = deterministic_hash({
            "schema": PROMPT_SCHEMA_VERSION,
            "text": text,
            "sections": [
                (item.name, item.provenance, item.version) for item in sections
            ],
        })
        if session.compiled_prompt_hash == prompt_hash:
            return

        section_hashes = {
            item.name: deterministic_hash({
                "name": item.name,
                "provenance": item.provenance,
                "version": item.version,
                "content": item.content,
            })
            for item in sections
        }

        stable_payload = {
            "role": "system",
            "content": stable_text,
            "message_type": PROMPT_MESSAGE_TYPE,
            "prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "prompt_sections": [
                {
                    "name": s.name,
                    "provenance": s.provenance,
                    "version": s.version,
                    "estimated_tokens": max(1, len(s.content) // 4),
                }
                for s in stable_sections
            ],
            "prompt_hash": stable_hash,
        }
        task_payload = {
            "role": "system",
            "content": task_text,
            "message_type": TASK_CONTRACT_MESSAGE_TYPE,
            "prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "prompt_sections": [
                {"name": s.name, "provenance": s.provenance, "version": s.version,
                 "estimated_tokens": max(1, len(s.content) // 4)}
                for s in task_sections
            ],
            "prompt_hash": prompt_hash,
        }
        stable_message = next((
            message for message in session.messages
            if message.get("message_type") == PROMPT_MESSAGE_TYPE
        ), None)
        task_message = next((
            message for message in session.messages
            if message.get("message_type") == TASK_CONTRACT_MESSAGE_TYPE
        ), None)
        seeded_stable = stable_message is None
        seeded_task = task_message is None
        if seeded_stable:
            session.messages.insert(0, stable_payload)
            stable_message = stable_payload
        if seeded_task:
            stable_index = session.messages.index(stable_message)
            session.messages.insert(stable_index + 1, task_payload)
            task_message = task_payload

        updates = []
        previous_hashes = dict(
            getattr(session, "compiled_prompt_sections", {}) or {}
        )

        def changed(items: list[PromptSection]) -> list[PromptSection]:
            if not previous_hashes:
                return list(items)
            return [
                item for item in items
                if previous_hashes.get(item.name) != section_hashes.get(item.name)
            ]

        if not seeded_stable and stable_message.get("prompt_hash") != stable_hash:
            stable_delta = changed(stable_sections)
            if stable_delta:
                updates.append("[HOST ROM UPDATE]\n" + render(stable_delta))
        if not seeded_task and task_message.get("prompt_hash") != prompt_hash:
            task_delta = changed(task_sections)
            if task_delta:
                updates.append("[HOST TASK CONTRACT UPDATE]\n" + render(task_delta))
        removed = sorted(set(previous_hashes) - set(section_hashes))
        if removed:
            updates.append(
                "[HOST CONTEXT WITHDRAWAL]\nThe following sections are no longer "
                "active and must not be used as current facts: " + ", ".join(removed)
            )
        if updates:
            session.append_host_steering(
                "\n\n".join(updates),
                steering_type="prompt_contract_update",
                version=prompt_hash,
            )
        session.compiled_prompt_hash = prompt_hash
        session.compiled_prompt_sections = section_hashes
