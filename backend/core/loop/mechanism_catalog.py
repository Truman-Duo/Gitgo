"""Bounded task mechanism IR over existing runtime consumers, never executable code."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


ELEMENTS = {
    "task.contract": {"consumer": "task_contract", "parameters": {}, "requires": []},
    "task.advancement": {"consumer": "mechanisms", "parameters": {"mode": ["auto", "manual"]}, "requires": ["task.contract"]},
    "task.plan": {"consumer": "engineering_workflow", "parameters": {"required": [True]}, "requires": ["task.contract"]},
    "task.discussion": {"consumer": "discussion_adoption", "parameters": {"adoption": ["explicit"]}, "requires": ["task.contract", "task.plan"]},
    "task.related_work": {"consumer": "task_maintenance", "parameters": {"review": ["on_relevant_change"]}, "requires": ["task.contract", "task.plan"]},
    "task.alignment": {"consumer": "alignment_runtime", "parameters": {"review": ["on_relevant_change"]}, "requires": ["task.contract"]},
    "task.lesson": {"consumer": "knowledge.feedback", "parameters": {"feedback": ["before_candidate"]}, "requires": ["task.contract"]},
    "task.completion": {"consumer": "completion_protocol", "parameters": {"policy": ["existing_requirements"]}, "requires": ["task.contract"]},
}
CATALOG_VERSION = digest(ELEMENTS)
MANDATORY = ("task.contract", "task.alignment", "task.completion")
CONSUMER_ORDER = {name: index for index, name in enumerate(("task.contract", "task.plan", "task.discussion",
    "task.related_work", "task.alignment", "task.advancement", "task.lesson", "task.completion"))}


def catalog():
    return {"version": CATALOG_VERSION, "scope": "current_task",
        "draft_template": {"id": "task.default", "intent": "The actual requested behavior", "source_refs": ["COPY_EXACT_REF_FROM_STATUS"],
            "elements": [{"use": "task.advancement", "version": 1, "parameters": {"mode": "auto"}},
                         {"use": "task.plan"}, {"use": "task.related_work"}, {"use": "task.lesson"}]},
        "input_note": "Each selected element MUST contain use, the exact catalog id. Optional id names this instance. source_refs are exact user:/decision:/change: refs, never file paths.",
        "elements": [
        {"id": name, "version": 1, "input": "task_state", "output": "task_state",
         "effect": "existing_control_plane", "authority": "existing_permissions_only",
         "activation_boundary": "before_next_admission", "retirement": "retain_obligations_and_receipts",
         "maintainer": "task_owner", **deepcopy(spec)} for name, spec in ELEMENTS.items()]}


def compile_definition(raw):
    if not isinstance(raw, dict) or len(json.dumps(raw, default=str).encode()) > 32000:
        raise ValueError("mechanism must be a bounded object")
    if set(raw) - {"id", "intent", "source_refs", "elements", "bindings", "catalog_version"}:
        raise ValueError("unknown mechanism fields; executable expressions and authority overrides are unsupported")
    identifier, intent = str(raw.get("id") or "task.default"), str(raw.get("intent") or "").strip()
    if not re.fullmatch(r"[a-zA-Z][\w.-]{0,95}", identifier) or not intent or len(intent) > 4000:
        raise ValueError("mechanism requires a bounded id and intent")
    if raw.get("catalog_version", CATALOG_VERSION) != CATALOG_VERSION:
        raise ValueError("MECHANISM_CATALOG_CHANGED: recompile against the current catalog")
    sources = raw.get("source_refs") or []
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32 or any(not isinstance(s, str) or len(s) > 180 for s in sources):
        raise ValueError("mechanism requires actual user/change source_refs from task_mechanism status")
    items = raw.get("elements") or []
    if not isinstance(items, list) or len(items) > len(ELEMENTS):
        raise ValueError("too many mechanism elements")
    nodes, uses = {}, set()
    for item in items:
        if not isinstance(item, dict) or set(item) - {"id", "use", "version", "parameters"}:
            raise ValueError("invalid mechanism element")
        use, node = item.get("use"), item.get("id", item.get("use"))
        if use not in ELEMENTS or item.get("version", 1) != 1 or use in uses or not isinstance(node, str) or node in nodes:
            raise ValueError("unknown, duplicate or unsupported mechanism element")
        parameters = item.get("parameters") or {}
        allowed = ELEMENTS[use]["parameters"]
        if not isinstance(parameters, dict) or set(parameters) - set(allowed):
            raise ValueError("unsupported mechanism parameters")
        for key, values in allowed.items():
            value = parameters.get(key, values[0])
            if not any(type(value) is type(v) and value == v for v in values):
                raise ValueError("unsupported mechanism parameter value: " + key)
        nodes[node] = {"id": node, "use": use, "version": 1,
                       "parameters": {key: parameters.get(key, values[0]) for key, values in allowed.items()}}
        uses.add(use)
    # Dependencies are registered capabilities, not model-invented services.
    needed = set(MANDATORY)
    for use in uses:
        needed.update(ELEMENTS[use]["requires"])
    while needed - uses:
        use = sorted(needed - uses)[0]
        if use in nodes:
            raise ValueError("element id conflicts with a mandatory dependency")
        nodes[use] = {"id": use, "use": use, "version": 1,
                      "parameters": {k: v[0] for k, v in ELEMENTS[use]["parameters"].items()}}
        uses.add(use)
        needed.update(ELEMENTS[use]["requires"])
    links = raw.get("bindings") or []
    if not isinstance(links, list) or len(links) > 64:
        raise ValueError("bindings must be bounded")
    edges = {key: set() for key in nodes}
    for edge in links:
        if not isinstance(edge, dict) or set(edge) != {"from", "to"} or edge["from"] not in nodes or edge["to"] not in nodes:
            raise ValueError("bindings must join registered task_state ports")
        if CONSUMER_ORDER[nodes[edge["from"]]["use"]] > CONSUMER_ORDER[nodes[edge["to"]]["use"]]:
            raise ValueError("binding conflicts with the existing consumer lifecycle; arbitrary execution ordering is unsupported")
        edges[edge["from"]].add(edge["to"])
    seen, visiting = set(), set()
    def visit(node):
        if node in visiting:
            raise ValueError("mechanism bindings cannot contain an unbounded cycle")
        if node not in seen:
            visiting.add(node)
            for target in edges[node]:
                visit(target)
            visiting.remove(node)
            seen.add(node)
    for node in nodes:
        visit(node)
    result = {"schema_version": 1, "id": identifier, "intent": intent, "source_refs": list(dict.fromkeys(sources)),
              "catalog_version": CATALOG_VERSION, "elements": sorted(nodes.values(), key=lambda n: n["id"]),
              "bindings": sorted(links, key=lambda e: (e["from"], e["to"])), "authority_granted": False}
    return {**result, "digest": digest(result)}


def enabled(definition, use):
    return next((n["parameters"] for n in (definition or {}).get("elements", []) if n["use"] == use), None)


def maintenance_schema():
    text = {"type": "string"}
    texts = {"type": "array", "items": text}
    step = {"type": "object", "properties": {"id": text, "goal": text, "depends_on": texts,
        "tools": texts, "resources": texts, "verification": text}, "required": ["id", "goal", "verification"], "additionalProperties": False}
    dependency = {"type": "object", "properties": {"kind": {"type": "string", "enum": ["file", "tool", "lesson"]},
        "id": text, "affected_tools": texts}, "required": ["kind", "id"], "additionalProperties": False}
    candidate = {"type": "object", "properties": {**{name: text for name in
        ("source", "affected", "reason", "consequence", "work", "verification", "owner")},
        "evidence_kind": {"type": "string", "enum": ["observed", "declared", "inferred"]}},
        "required": ["source", "affected", "reason", "consequence", "work", "verification", "owner", "evidence_kind"], "additionalProperties": False}
    resolution = {"type": "object", "properties": {"id": text, "reason": text, "contract_fields": texts, "question_ids": texts,
        "work_item_ids": {**texts, "maxItems": 64, "description": "Existing plan step ids whose evidence must be renewed for the adopted change."},
        "disposition": {"type": "string", "enum": ["adopt", "withdraw"]}, "source_ref": text, "quote": text},
        "required": ["id", "reason", "contract_fields"], "additionalProperties": False}
    related_disposition = {"type": "object", "properties": {"id": text, "reason": text, "source_ref": text, "quote": text,
        "action": {"type": "string", "enum": ["reject", "withdraw"]}},
        "required": ["id", "reason", "action"], "additionalProperties": False}
    return {"type": "object", "description": "Atomic scope/plan/related-work reconciliation. Copy exact expected_revision and pending ids from task_mechanism status.",
        "properties": {"expected_revision": {"type": "integer", "minimum": 0},
            "plan": {"type": "object", "properties": {"summary": text, "steps": {"type": "array", "items": step, "minItems": 1, "maxItems": 64},
                "dependencies": {"type": "array", "items": dependency, "maxItems": 64}}, "required": ["summary", "steps"], "additionalProperties": False},
            "plan_review": {"type": "object", "properties": {"action": {"const": "retain"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 4000}},
                "required": ["action", "reason"], "additionalProperties": False,
                "description": "Explicit Agent judgment that the existing plan still covers the actual maintenance trigger. Retains its steps and dependencies; cannot replace scope/adoption reconciliation."},
            "related_review": {"type": "object", "properties": {"summary": text, "candidates": {"type": "array", "items": candidate, "maxItems": 32}},
                "required": ["summary"], "additionalProperties": False},
            "resolve_changes": {"type": "array", "items": resolution, "maxItems": 64},
            "resolve_maintenance": texts, "adopt_related": texts,
            "related_dispositions": {"type": "array", "items": related_disposition, "maxItems": 32}},
        "required": ["expected_revision"], "additionalProperties": False}
