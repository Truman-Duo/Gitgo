"""Selected immutable scratch material enters the existing harvest observation bus."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json

from .sheets import _text


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def submit_material(sheets, sources, *, sheet_id, revision, cell_ids, purpose,
                    project, task_id, context):
    if not project or not task_id:
        raise ValueError("Material submission requires the current Runtime-bound project and user task")
    sheets._revision_key(sheet_id, revision)
    document, selected, _ = sheets._load(sheet_id, revision)
    if (not isinstance(cell_ids, list) or not 1 <= len(cell_ids) <= 12 or
            not all(isinstance(i, str) for i in cell_ids) or len(set(cell_ids)) != len(cell_ids)):
        raise ValueError("Select 1..12 distinct cell ids")
    chosen = [deepcopy(c) for c in document["cells"] if c["id"] in cell_ids]
    if len(chosen) != len(cell_ids):
        raise ValueError("A selected material cell is missing")
    source_ids = set(cell_ids)
    source_ids.update(c["metadata"]["gitgo"].get("input_cell_id") for c in chosen
                      if c["metadata"]["gitgo"]["kind"] == "computed_value")
    bindings = [deepcopy(c["metadata"]["gitgo"]) for c in document["cells"]
                if c["id"] in source_ids and c["metadata"]["gitgo"]["kind"] in {"context_source", "tool_result_source"}]
    material = {"sheet_id": sheet_id, "revision": selected, "owner": sheets.owner,
                "cell_ids": sorted(cell_ids), "cells": chosen, "source_bindings": bindings,
                "authority": "unconfirmed working material; notes are assertions and computed values only describe their declared inputs"}
    if len(json.dumps(material, ensure_ascii=False).encode("utf-8")) > 16_000:
        raise ValueError("Selected material exceeds 16000 bytes; select fewer meaningful cells or a result with its provenance")
    material_id = _digest(material)
    from backend.core.knowledge.harvest import capture_signal, CaptureAuditIncomplete
    from backend.core.knowledge.evidence_context import task_context_snapshot
    warnings = []
    detail = {"task_id": task_id, "purpose": _text(purpose, "material purpose", 2000),
              "information_kind": "unclassified", "material": material,
              "material_digest": material_id, "task_context": task_context_snapshot(context),
              "applicability_at_submission": {k: v for k, v in sources.assessments(document).items() if k in source_ids} if sources else {},
              "confirmation": "not requested or granted by this tool"}
    try:
        signal_id = capture_signal("workbench_material", detail, project,
            source_event_id="workbench:" + material_id, storage=sheets.storage)
    except CaptureAuditIncomplete as exc:
        signal_id = exc.signal_id
        from backend.core.errors import notice_guidance
        warnings = [{"code": "WORKBENCH_MATERIAL_AUDIT_INCOMPLETE",
                     **notice_guidance("WORKBENCH_MATERIAL_AUDIT_INCOMPLETE"),
                     "message": "The material observation committed, but its audit append failed. Preserve its signal_id; do not repeat or reset the submitted material."}]
    record = next(r for r in sheets.storage.load_harvest_signal_records() if r["signal_id"] == signal_id)
    return {"sheet_id": sheet_id, "revision": selected, "material_digest": material_id,
            "signal_id": signal_id, "state": record["state"], "proposal_id": record.get("proposal_id"),
            "authority": "observation only; not an approved or injectable lesson",
            "next_step": "The existing task-end harvest summarizes eligible material and presents any candidate through normal confirmation/BTW. It may produce no knowledge.",
            **({"warnings": warnings} if warnings else {})}


def captured_bindings(workspace, signal):
    """Only the committed observation can supply material evidence, not prompt prose."""
    if signal.get("signal_type") != "workbench_material":
        return []
    from backend.core.storage import get_storage
    storage = get_storage(workspace)
    record = next((r for r in storage.load_harvest_signal_records()
                   if r["signal_id"] == signal.get("signal_id") and r["signal_type"] == "workbench_material"), None)
    if record is None:
        return [{"state": "unresolved", "signal_id": signal.get("signal_id")}]
    detail = record["detail"]
    material = detail.get("material") or {}
    ref = material.get("revision", "")
    key = f"{material.get('owner', '')}/revision/{material.get('sheet_id', '')}/{ref}"
    if _digest(material) != detail.get("material_digest") or storage.get_state_ref("workbench.v1", key) != ref:
        return [{"state": "unresolved", "signal_id": signal.get("signal_id")}]
    return [{"state": "bound", "signal_id": record["signal_id"],
             "material_digest": detail["material_digest"], "sheet_id": material["sheet_id"],
             "revision": ref, "cell_ids": material["cell_ids"],
             "source_bindings": deepcopy(material["source_bindings"]),
             "authority": "Selected working material only; source identity is not semantic proof"}]


def assess_bindings(workspace, bindings):
    from backend.core.loop.context_store import ContextObjectStore
    from .sources import SourceWorkbench
    service = SourceWorkbench(None, ContextObjectStore(workspace))
    for binding in bindings:
        if binding.get("state") != "bound":
            return {"state": "needs_recheck", "reason": "workbench_material_unresolved"}
        sources = binding.get("source_bindings") or []
        if not sources:
            return {"state": "needs_recheck", "reason": "workbench_material_has_no_independent_source"}
        for source in sources:
            assessment = service._assess_source(source)
            if assessment["state"] != "current":
                return {**assessment, "reason": "workbench_" + assessment["reason"]}
    return {"state": "current", "reason": "workbench_source_identity_matches"}
