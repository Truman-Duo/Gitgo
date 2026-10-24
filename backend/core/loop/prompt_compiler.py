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
PROMPT_SCHEMA_VERSION = 8


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
            list(getattr(process.session, "host_ledger", []) or []), task_id=process.active_task_id or process.process_id
        )
        sections = [
            PromptSection("Base identity", identity, "host:role-contract"),
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
                "\n".join(tool_lines),
                "host:effective-tool-registry",
            ),
        ]
        sections.append(PromptSection(
            "Source authority", load_prompt('policy/source_authority'), "host:source-authority",
        ))
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
                            completion=format(completion, ''),
                            adaptive_route_guidance=format(adaptive_route_guidance, ''),
                        )
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
                        render_prompt(
                            'contracts/workflow',
                            task_kind=format(task_kind, ''),
                            completion=format(completion, ''),
                            contract_json=format(json.dumps(contract_payload, ensure_ascii=False, separators=(',', ':')), ''),
                        )
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
                load_prompt('context/agent_executions')
                + json.dumps(records, ensure_ascii=False, separators=(',', ':'))
            ), "host:task-contract"))
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
        if confirmed_state:
            sections.append(PromptSection(
                "Latest user-confirmed state",
                (
                    load_prompt('context/confirmed_user_state')
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
            "Behavior and delivery standard", "User collaboration protocol", "Source authority",
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
                load_prompt('context/withdrawal') + ", ".join(removed)
            )
        if updates:
            session.append_host_steering(
                "\n\n".join(updates),
                steering_type="prompt_contract_update",
                version=prompt_hash,
            )
        session.compiled_prompt_hash = prompt_hash
        session.compiled_prompt_sections = section_hashes
