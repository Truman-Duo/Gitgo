"""One governance projection for admission and live turn boundaries.

Projects existing authoritative signals; does not add a parallel governor or
mailbox injection. Semantic applicability is distinct from snapshot freshness.
"""
from __future__ import annotations

import hashlib
import json
from .signals import GovernanceSignal
from .evidence_applicability import EvidenceApplicability


class GovernanceProjection:
    SCHEMA = 3

    @staticmethod
    def normalize(signals: list) -> list[GovernanceSignal]:
        return [GovernanceSignal.from_dict(item) if isinstance(item, dict) else item for item in signals]

    @classmethod
    def compose(cls, context: dict, signals: list, *, base_brief: str = "") -> dict:
        normalized, candidates = EvidenceApplicability.partition(cls.normalize(signals), context)
        wire = sorted((item.to_dict() for item in normalized), key=lambda item: item["signal_id"])
        evidence_graph = EvidenceApplicability.graph(context)
        # governance_active is the projection output. Including it in its own
        # digest would force one rewrite after every otherwise-idempotent publish.
        digest_graph = {
            name: value for name, value in evidence_graph.items()
            if name != "governance_active"
        }
        lessons = [item.to_dict() if hasattr(item, "to_dict") else dict(item)
                   for item in (context.get("lessons") or [])]
        payload = {"schema": cls.SCHEMA, "base_brief": base_brief, "signals": wire,
                   "lessons": lessons,
                   "evidence_candidates": candidates, "evidence_graph": digest_graph}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":")).encode()).hexdigest()
        old_ids = {item.signal_id for item in cls.normalize(context.get("signals") or [])}
        removed = sorted(old_ids - {item.signal_id for item in normalized})
        lines = [base_brief.strip()] if base_brief.strip() else []
        lines.append(f"[Current governance snapshot {digest[:16]}]")
        if not normalized:
            lines.append("No active signals in this snapshot. Earlier signal lists are historical.")
        for item in normalized[:12]:
            lines.append(f"- {item.signal_id} [{item.severity.value}] {item.rule}")
            if item.suggestion:
                lines.append(f"  Next: {item.suggestion}")
        if len(normalized) > 12:
            lines.append("Use decision_evidence(governance) for the complete active list.")
        if removed:
            lines.append("No longer active (not proof of a successful repair): " + ", ".join(removed))
        if candidates:
            lines.append(f"{len(candidates)} historical/out-of-scope/unverified observations are available "
                         "via decision_evidence(governance). They are not current limitations or proof of repair; "
                         "revalidate relevant observations with the source tool before relying on them.")
        return {**context, "signals": normalized, "base_brief": base_brief,
                "lessons": lessons,
                "evidence_candidates": candidates,
                "evidence_sources": dict(context.get("evidence_sources") or {}),
                "evidence_graph": evidence_graph,
                "governance_projection_schema": cls.SCHEMA,
                "governance_digest": digest, "removed_governance_signal_ids": removed,
                "brief": "\n".join(lines)}

    @classmethod
    def publish(
        cls, process, signals: list, *, base_brief: str = "",
        candidates: list | None = None, evidence_sources: dict | None = None,
        lessons: list | None = None,
    ) -> bool:
        context, _ = process.read_context_snapshot()
        projection_context = dict(context)
        if evidence_sources is not None:
            projection_context["evidence_sources"] = dict(evidence_sources)
        if lessons is not None:
            projection_context["lessons"] = list(lessons)
        updated = cls.compose(
            projection_context, [*signals, *(candidates or [])], base_brief=base_brief,
        )
        if (context.get("governance_digest") == updated["governance_digest"]
                and not context.get("governance_context_error")):
            return False
        refs = dict(context.get("context_refs") or {})
        workspace = str(context.get("workspace_path") or "")
        if workspace:
            from .context_store import ContextObjectStore
            try:
                store = ContextObjectStore(workspace)
                if lessons is not None:
                    refs["project_lessons"] = store.put(
                        "knowledge/project-lessons", list(lessons),
                        metadata={
                            "authority": "host", "producer": "LessonManager",
                            "depends_on": {},
                        },
                    )
                dependencies = {
                    name: str(value.get("digest") or "")
                    for name, value in refs.items()
                    if name != "governance_active" and isinstance(value, dict)
                    and value.get("digest")
                }
                policy_source = (updated.get("evidence_sources") or {}).get("policy_snapshot") or {}
                if policy_source.get("digest"):
                    dependencies["policy_snapshot"] = str(policy_source["digest"])
                refs["governance_active"] = store.put(
                    "governance/active", {"brief": updated["brief"], "signals": updated["signals"],
                                          "evidence_candidates": updated["evidence_candidates"]},
                    metadata={
                        "authority": "privileged", "producer": "GovernanceProjection",
                        "depends_on": dependencies,
                    },
                )
                updated.pop("governance_context_error", None)
            except (OSError, ValueError) as exc:
                refs.pop("governance_active", None)
                updated["governance_context_error"] = str(exc)[:500]
                updated["brief"] += "\nContext reference unavailable; use decision_evidence for Host facts."
        def merge(current):
            # Only governance-owned fields may be changed here. A concurrent
            # task-contract admission or context publication must survive.
            owned = ("signals", "base_brief", "governance_projection_schema",
                     "governance_digest", "removed_governance_signal_ids", "brief",
                     "evidence_candidates", "evidence_graph", "evidence_sources",
                     "lessons")
            merged = {**current, **{key: updated[key] for key in owned if key in updated}}
            if "governance_context_error" in updated:
                merged["governance_context_error"] = updated["governance_context_error"]
            else:
                merged.pop("governance_context_error", None)
            current_refs = dict(current.get("context_refs") or {})
            for ref_name in ("project_lessons", "governance_active"):
                if ref_name in refs:
                    current_refs[ref_name] = refs[ref_name]
                elif ref_name == "governance_active":
                    current_refs.pop(ref_name, None)
            merged["context_refs"] = current_refs
            return merged
        process.update_context_snapshot(merge)
        return True
