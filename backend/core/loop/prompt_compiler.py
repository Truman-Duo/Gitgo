"""Provider-neutral, versioned system prompt compiler.

The compiler owns policy sections. Provider adapters own wire encoding only.
Dynamic values are escaped as data and never promoted to higher authority.
"""

from __future__ import annotations

from backend.core.prompt_templates import load_prompt, render_prompt
from backend.core.prompt_profiles import with_prompt_profile

import json
from dataclasses import dataclass
from pathlib import Path

from backend.core.loop.provider_protocol import deterministic_hash


PROMPT_MESSAGE_TYPE = "compiled_system_prompt"
TASK_CONTRACT_MESSAGE_TYPE = "compiled_task_contract"
PROMPT_SCHEMA_VERSION = 17
GOVERNANCE_MESSAGE_TYPE = 'compiled_governance_context'
MIN_SPLIT_PREFIX_SCHEMA_VERSION = 6
STABLE_SECTION_NAMES = frozenset({
    "Behavior and delivery standard", "User collaboration protocol", "Source authority",
})


@dataclass(frozen=True)
class PromptSection:
    name: str
    content: str
    provenance: str
    version: str = "1"


class PromptCompiler:
    """Compile stable policy plus task-scoped runtime data in a fixed order."""

    @classmethod
    @with_prompt_profile
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
                render_prompt('identity/supervisor', model_id=repr(model_id))
            ),
            "reviewer": (
                load_prompt('identity/reviewer')
            ),
        }.get(actor, (
            load_prompt('identity/worker')
        ))
        if task_kind == "answer":
            identity = (
                render_prompt('identity/answer', model_id=repr(model_id))
            )

        lease_text = "none"
        if lease is not None:
            lease_text = (
                f"{lease.lease_id} for profile {lease.profile_id}; "
                f"actions={', '.join(lease.intended_actions)}"
            )

        completion = {
            "answer": (
                load_prompt('completion/answer')
            ),
            "supervisor": (
                load_prompt('completion/supervisor')
            ),
            "action": (
                load_prompt('completion/action')
            ),
            "plan": (
                load_prompt('completion/plan')
            ),
            "review": (
                load_prompt('completion/review')
            ),
        }.get(task_kind, (
            load_prompt('completion/default')
        ))

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
                        load_prompt('routing/self_execute_request')
                    )
                else:
                    adaptive_route_guidance = (
                        load_prompt('routing/self_execute_active')
                    )
            elif execution_mode in {"delegate", "either"}:
                adaptive_route_guidance = (
                    load_prompt('routing/delegated')
                )
            else:
                adaptive_route_guidance = (
                    load_prompt('routing/answer')
                )
            adaptive_route_guidance += (
                load_prompt('routing/clarification_boundary')
            )
        upstream = []
        for raw in list(context.get("upstream_outcomes") or []):
            item = dict(raw or {})
            outcome = dict(item.get("outcome") or {})
            metadata = dict(outcome.get("metadata") or {})
            evidence = metadata.get("completion_evidence") or {}
            response = str(outcome.get("response") or "")
            head_end = min(len(response), 4000)
            tail_start = max(head_end, len(response) - 1200)
            upstream.append({
                **{key: item[key] for key in ("process_id", "task_id", "status", "result_commit",
                                               "source_process_id", "draft_id", "changed_files") if key in item},
                "completion_evidence": {key: evidence[key] for key in ("factual_ready", "required_test_ids",
                                                                        "test_failures", "reasons") if key in evidence},
                **({"response_tail": response[tail_start:],
                    "response_tail_range": [tail_start, len(response)]} if tail_start < len(response) else {}),
                "response_excerpt": response[:head_end],
                "response_truncated": tail_start > head_end,
                "error": outcome.get("error"),
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
        if task_contract.get("required_workbench_claims"):
            contract_payload["required_workbench_claims"] = list(task_contract["required_workbench_claims"])
        from backend.core.loop.decision_support import confirmed_user_state
        from backend.core.loop.main_dialogue import owns_dialogue
        # Main-owned current decisions travel in the bounded structural view,
        # together with open decisions. Do not also encode this mutable data
        # as character edits to a rule/instruction section.
        confirmed_state = [] if owns_dialogue(context.get('alignment_runtime') or {}) else confirmed_user_state(
            list(getattr(process.session, "host_ledger", []) or []), task_id=process.active_task_id or process.process_id
        )
        sections = [
            PromptSection("Base identity", identity, "host:role-contract"),
            PromptSection("Completion protocol", completion, "host:completion-protocol"),
            PromptSection(
                "Behavior and delivery standard",
                (
                    load_prompt('policy/behavior')
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
                load_prompt('policy/capability_use'),
                "host:effective-tool-registry",
            ),
        ]
        sections.append(PromptSection(
            "Source authority", load_prompt('policy/source_authority'), "host:source-authority",
        ))
        if "task_mechanism" in tools and (process.runtime_preferences.get("task_maintenance_enabled") or context.get("task_maintenance")):
            sections.append(PromptSection("Task mechanism and maintenance",
                load_prompt('runtime/task_maintenance'), "runtime:task-maintenance"))
        if tools:
            sections.append(PromptSection(
                "Tool use and feedback",
                load_prompt('policy/tool_use') + "\n" + load_prompt('policy/tool_feedback'),
                "host:tool-guidance",
            ))
        if any(getattr(tool, "runner_name", "") in {"edit_file", "write_file", "apply_patch", "exec_command", "shell_script"}
               for tool in tools.values()):
            sections.append(PromptSection(
                "Implementation safety", load_prompt('policy/implementation_safety'), "host:implementation-guidance",
            ))
        sections.append(PromptSection(
            "User collaboration protocol",
            (
                load_prompt('policy/user_collaboration')
            ),
            "host:collaboration-policy",
        ))
        if task_kind != "answer" or actor in {"worker", "reviewer"}:
            sections.append(PromptSection(
                "Agent relationship and communication protocol",
                (
                    load_prompt('policy/agent_relationships')
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
                        render_prompt(
                            'contracts/answer',
                            completion='[See Completion protocol]',
                            adaptive_route_guidance='[See Adaptive routing]',
                        )
                    ),
                    "host:task-contract",
                ),
            ])
            sections.append(PromptSection("Adaptive routing", adaptive_route_guidance, "host:routing-guidance"))
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
                        render_prompt(
                            'contracts/workflow',
                            task_kind=format(task_kind, ''),
                            completion='[See Completion protocol]',
                            contract_json='[See Task requirements]',
                        )
                    ),
                    "host:task-contract",
                ),
            ])
            sections.append(PromptSection("Task requirements",
                json.dumps(contract_payload, ensure_ascii=False, separators=(',', ':')), "host:task-requirements"))
            if upstream:
                sections.append(PromptSection("Upstream evidence",
                    load_prompt("context/upstream_evidence"), "host:task-requirements"))
        if actor == "supervisor" and getattr(process, "child_ids", None):
            from .assignment_handoff import handoff_status, withdrawal_context

            child_ids, contracts, reviews = process.coordination_snapshot()
            child_ids = [child_id for child_id in child_ids
                         if not (contracts.get(child_id) or {}).get("subagent_owner_contract")]
            manager = getattr(process, "_manager", None)
            maintenance = context.get("task_maintenance") or {}
            handoffs = {row["source_process_id"]: row for row in handoff_status(process, maintenance)
                        if row["phase"] != "reassigned"}
            pending_feedback, issued = withdrawal_context(process, maintenance, child_ids[-16:], contracts)
            records = []
            for child_id in child_ids[-16:]:
                child = manager.get(child_id) if manager else None
                contract = contracts.get(child_id, {})
                returned = contract.get("returned_assignment")
                records.append({
                    "process_id": child_id,
                    "display_name": child.session.display_name if child else "unavailable",
                    "execution_status": child.status.value if child else "missing",
                    "outcome_status": (child.result or {}).get("status") if child else None,
                    "review": reviews.get(child_id, {}).get("verdict", "not_reviewed"),
                    "target_files": contract.get("target_files", []),
                    "superseded_by": contract.get("superseded_by", ""),
                    **({"returned_to": returned["owner_process_id"]} if returned else {}),
                    **({"handoff": handoffs[child_id]} if child_id in handoffs else {}),
                    **({"issued_scope": issued[child_id]} if child_id in issued else {}),
                })
            if records:
                sections.append(PromptSection("Known agent executions", (
                    load_prompt('context/agent_executions')
                    + json.dumps(records, ensure_ascii=False, separators=(',', ':'))
                ), "host:task-contract"))
            if issued:
                sections.append(PromptSection("Pending user feedback for owned work", (
                    "Interpret the user's feedback. If it removes a required file from an issued_scope above, "
                    "revise the parent contract and include assignment_scope in the same maintenance.resolve_changes entry. "
                    "Use assignment_ref from issued_scope. Select withdrawn files and test IDs and use "
                    "retire_criterion_numbers for the one-based positions of withdrawn acceptance criteria. "
                    "The runtime removes the matching entries from the inherited parent fields when omitted on revise. "
                    "Use close_source=true if the user wants immediate replacement and the current files are ready to retain. "
                    "For one feedback, omit contract_fields. Use the issued scope directly when sufficient. "
                    "Keep every other obligation. Pending feedback IDs: "
                    + json.dumps(pending_feedback, ensure_ascii=False, separators=(',', ':'))
                ), "host:task-maintenance"))
        if actor == "supervisor" and task_kind != "answer":
            sections.append(PromptSection(
                "Delegation economy",
                (
                    load_prompt('policy/delegation_economy')
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
                        load_prompt('context/pending_coordination')
                        + json.dumps(
                            pending_coordination[-16:], ensure_ascii=False,
                            separators=(",", ":"), default=str,
                        )
                    ),
                    "host:coordination-events",
                ))
        if task_kind == "answer" and task_contract.get("required_workbench_claims"):
            sections.append(PromptSection("Workbench delivery requirements",
                json.dumps({"required_workbench_claims": task_contract["required_workbench_claims"]},
                           ensure_ascii=False, separators=(',', ':')), "host:task-requirements"))
        if confirmed_state:
            from .dialogue_context import grouped_decisions
            sections.append(PromptSection(
                "Current user decisions and delegation",
                (
                    load_prompt('context/confirmed_user_state')
                    + json.dumps(grouped_decisions(confirmed_state), ensure_ascii=False, separators=(",", ":"))
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
        from .prompt_rule_updates import identity, record, render_sections, visible_sections, section_update, update_text
        session.messages[:] = [
            message for message in session.messages
            if message.get("message_type") != "governance_context_seed"
        ]
        # Identity, role and effective capabilities belong in the task prefix;
        # the shared stable-name contract also drives boundary migration.
        stable_names = STABLE_SECTION_NAMES
        stable_sections = [item for item in sections if item.name in stable_names]
        volatile_names = {'Project governance context'}
        volatile_sections = [item for item in sections if item.name in volatile_names]
        task_sections = [item for item in sections if item.name not in stable_names | volatile_names]

        def render(items: list[PromptSection]) -> str:
            return render_sections([record(section) for section in items])[0]

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
        latest_governance = next((message for message in reversed(session.messages)
            if message.get('message_type') == GOVERNANCE_MESSAGE_TYPE), None)
        governance_present = not volatile_sections or (
            latest_governance is not None and latest_governance.get('content') == render(volatile_sections))
        previous = visible_sections(session.messages)
        ordinary = stable_sections + task_sections
        visible_current = all(item.name in previous and identity(previous[item.name]) == identity(record(item))
                              for item in ordinary)
        if session.compiled_prompt_hash == prompt_hash and governance_present and visible_current:
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
        # Exact source spans avoid duplicating section bodies in metadata.
        stable_payload['content'], stable_payload['prompt_sections'] = render_sections([record(s) for s in stable_sections])
        task_payload['content'], task_payload['prompt_sections'] = render_sections([record(s) for s in task_sections])
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

        # A newly inserted prefix can precede retained steering. Compare the
        # actual final visible state, not merely the presence of a new seed.
        previous = visible_sections(session.messages)

        updates = []
        previous_hashes = dict(
            getattr(session, "compiled_prompt_sections", {}) or {}
        )

        def changed(items: list[PromptSection]) -> list[PromptSection]:
            return [
                item for item in items
                if (item.name in volatile_names and previous_hashes.get(item.name) != section_hashes.get(item.name))
                or (item.name not in volatile_names and (item.name not in previous
                    or identity(previous[item.name]) != section_hashes.get(item.name)))
            ]

        stable_delta = changed(stable_sections)
        updates.extend(section_update(previous.get(s.name), record(s)) for s in stable_delta)
        task_delta = changed(task_sections)
        updates.extend(section_update(previous.get(s.name), record(s)) for s in task_delta)
        removed = sorted((set(previous_hashes) | set(previous)) - set(section_hashes))
        ordinary_removed = [name for name in removed if name not in volatile_names]
        if ordinary_removed:
            updates.extend({'operation': 'remove', 'name': name} for name in ordinary_removed)
        if updates:
            session.append_host_steering(
                update_text(updates),
                steering_type="prompt_contract_update",
                version=prompt_hash,
            )
        # A changing project snapshot must not precede shared task/evidence
        # inputs. Keep its original system authority, append at this boundary,
        # and never rewrite already-sent history. These are the same complete
        # compiler sections, not a cached authorization or an LLM summary.
        volatile_delta = changed(volatile_sections)
        if not governance_present:
            volatile_delta = volatile_sections
        volatile_removed = [name for name in removed if name in volatile_names]
        if volatile_delta or volatile_removed:
            content = render(volatile_delta)
            if volatile_removed:
                content += '\n\n' + load_prompt('context/withdrawal') + ', '.join(volatile_removed)
            session.append_system(content, message_type=GOVERNANCE_MESSAGE_TYPE,
                prompt_schema_version=PROMPT_SCHEMA_VERSION,
                prompt_sections=[{'name': s.name, 'provenance': s.provenance, 'version': s.version}
                    for s in volatile_delta], prompt_hash=prompt_hash)
        session.compiled_prompt_hash = prompt_hash
        session.compiled_prompt_sections = section_hashes
