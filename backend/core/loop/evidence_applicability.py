"""Deterministic applicability at the existing governance projection boundary.

This is not a second fact database or a semantic classifier. Provenance is
supplied by Host producers, and dependencies point to the existing context refs.
Changing a source invalidates an observation; it never proves a repair.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any


class EvidenceApplicability:
    """Resolve evidence against one Host-owned, versioned dependency graph.

    ``context_refs`` and ``evidence_sources`` are two views of the same graph:
    addressable context objects and deterministic producer snapshots.  Nodes
    record the exact versions of their upstream inputs at production time.
    Applicability walks those edges transitively, so a stable top-level digest
    cannot conceal a changed input.
    """

    @staticmethod
    def _dependency_spec(value: Any) -> tuple[str, dict]:
        if isinstance(value, dict):
            return str(value.get("digest") or value.get("version") or ""), dict(
                value.get("depends_on") or {}
            )
        return str(value or ""), {}

    @classmethod
    def graph(cls, context: dict) -> dict[str, dict]:
        graph: dict[str, dict] = {}
        for collection in (context.get("context_refs") or {},
                           context.get("evidence_sources") or {}):
            if not isinstance(collection, dict):
                continue
            for name, raw in collection.items():
                digest, nested = cls._dependency_spec(raw)
                if not digest:
                    continue
                raw_dict = raw if isinstance(raw, dict) else {}
                graph[str(name)] = {
                    "digest": digest,
                    "producer": str(raw_dict.get("producer") or name),
                    "depends_on": nested,
                }
        return graph

    @classmethod
    def _validate_node(
        cls, name: str, expected: str, graph: dict[str, dict],
        *, path: tuple[str, ...] = (), validated: set[tuple[str, str]] | None = None,
    ) -> dict | None:
        validated = validated if validated is not None else set()
        if name in path:
            return {
                "state": "needs_recheck", "reason": "dependency_cycle",
                "source": name, "dependency_path": [*path, name],
            }
        node = graph.get(name)
        if node is None:
            return {
                "state": "needs_recheck", "reason": "source_version_missing",
                "source": name, "expected": expected, "actual": None,
                "dependency_path": [*path, name],
            }
        actual = str(node.get("digest") or "")
        if not expected or actual != expected:
            return {
                "state": "needs_recheck", "reason": "source_version_changed",
                "source": name, "expected": expected, "actual": actual or None,
                "dependency_path": [*path, name],
            }
        identity = (name, expected)
        if identity in validated:
            return None
        next_path = (*path, name)
        for upstream, raw_spec in sorted((node.get("depends_on") or {}).items()):
            upstream_version, _nested = cls._dependency_spec(raw_spec)
            failure = cls._validate_node(
                str(upstream), upstream_version, graph,
                path=next_path, validated=validated,
            )
            if failure:
                return failure
        validated.add(identity)
        return None

    @staticmethod
    def assess(evidence: dict, context: dict) -> dict:
        scope = dict(evidence.get("scope") or {})
        for key in ("project_name", "task_id"):
            expected = str(scope.get(key) or "")
            actual = str(context.get(key) or "")
            if expected and actual and expected != actual:
                return {"state": "out_of_scope", "reason": f"{key}_changed"}
            if expected and not actual:
                return {"state": "needs_recheck", "reason": f"{key}_unknown"}
        expected_workspace = str(scope.get("workspace_path") or "")
        actual_workspace = str(context.get("workspace_path") or "")
        if expected_workspace:
            if not actual_workspace:
                return {"state": "needs_recheck", "reason": "workspace_unknown"}
            if Path(expected_workspace).resolve() != Path(actual_workspace).resolve():
                return {"state": "out_of_scope", "reason": "workspace_changed"}
        if evidence.get("kind") == "history":
            return {"state": "historical", "reason": "observation_not_revalidated"}
        graph = EvidenceApplicability.graph(context)
        validated: set[tuple[str, str]] = set()
        for name, raw_spec in sorted((evidence.get("depends_on") or {}).items()):
            expected, _nested = EvidenceApplicability._dependency_spec(raw_spec)
            failure = EvidenceApplicability._validate_node(
                str(name), expected, graph, validated=validated,
            )
            if failure:
                return failure
        return {"state": "current", "reason": "host_observation"}

    @classmethod
    def partition(cls, signals: list, context: dict) -> tuple[list, list[dict]]:
        active, candidates = [], []
        for signal in signals:
            evidence = signal.metadata.get("evidence")
            catalog_assessment = signal.metadata.get("applicability")
            # Existing explicitly constructed runtime signals keep their
            # enforcement semantics.  Catalog lessons are different: their
            # file/dependency evidence has already been assessed by
            # ``assess_lesson`` before normalization.  Do not erase that
            # verdict merely because the lightweight catalog signal has no
            # second EvidenceApplicability graph edge of its own.
            if (
                signal.source == "lesson_trigger"
                and signal.metadata.get("enforcement_authority") is False
                and isinstance(catalog_assessment, dict)
            ):
                assessment = dict(catalog_assessment)
            else:
                assessment = cls.assess(evidence, context) if isinstance(evidence, dict) else {
                    "state": "current", "reason": "explicit_runtime_signal",
                }
            projected = replace(signal, metadata={**signal.metadata, "applicability": assessment})
            if assessment["state"] == "needs_recheck" and signal.category.value == "block":
                # Uncertain versions must not silently remove a safety gate.
                # Retain the conservative block, but do not assert the old
                # condition as a freshly observed fact.
                active.append(replace(projected, rule="Revalidation required before releasing prior block: " + signal.rule))
                candidates.append(projected.to_dict())
            elif assessment["state"] == "current":
                active.append(projected)
            else:
                candidates.append(projected.to_dict())
        return active, candidates

    @staticmethod
    def annotate(
        signals: list, *, kind: str, project_name: str, workspace_path: str,
        depends_on: dict | None = None,
    ) -> list:
        return [replace(signal, metadata={**signal.metadata, "evidence": {
            "kind": kind,
            "scope": {"project_name": project_name, "workspace_path": str(Path(workspace_path).resolve())},
            "depends_on": dict(depends_on or {}),
        }}) for signal in signals]
