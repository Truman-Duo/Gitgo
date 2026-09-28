"""Admission-time eager/lazy/never context policy."""

from __future__ import annotations


def context_admission_policy(actor_kind: str, task_kind: str) -> dict[str, list[str]]:
    privileged = ["governance_active"]
    if task_kind == "answer":
        return {
            "eager_privileged": [],
            "eager_context": [],
            "lazy": ["project_lessons", "dependency_impact"],
            "never": ["lease_ids", "raw_reasoning", "unrelated_agent_transcripts"],
        }
    if actor_kind == "reviewer":
        return {
            "eager_privileged": privileged,
            "eager_context": [],
            "lazy": ["project_lessons", "dependency_impact"],
            "never": ["process_ids", "lease_ids", "raw_reasoning"],
        }
    if actor_kind == "supervisor":
        return {
            "eager_privileged": privileged,
            "eager_context": [],
            "lazy": ["project_lessons", "dependency_impact", "child_transcripts"],
            "never": ["raw_reasoning"],
        }
    return {
        "eager_privileged": privileged,
        "eager_context": [],
        "lazy": ["project_lessons", "dependency_impact"],
        "never": ["lease_ids", "raw_reasoning", "unrelated_agent_transcripts"],
    }
