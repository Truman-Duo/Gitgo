"""On-demand explanations of existing protocols; no grants, state or execution."""
import json

from backend.core.errors import diagnostic_entry, guidance_reference, ERROR_CATALOG, NOTICE_CATALOG
from backend.core.prompt_templates import load_prompt, render_prompt


FILE_SECTIONS = {
    "FILE_EXISTS": "Recompute against current bytes",
    "STRING_NOT_FOUND": "Recompute against current bytes",
    "FILE_CHANGED": "Recompute against current bytes",
    "EXPECTED_HASH_REQUIRED": "Recompute against current bytes",
    "PATCH_CHECK_FAILED": "Patch validation and uncertain effects",
    "FILE_MUTATION_BUSY": "Cooperating writer lock",
    "FILE_MUTATION_FAILED": "Batch commits and recovery references",
    "FILE_CHANGED_AFTER_COMMIT": "Batch commits and recovery references",
}

NATIVE_RECOVERY_REASONS = frozenset({
    "NATIVE_LAUNCH_UNCONFIRMED", "NATIVE_CLEANUP_UNCONFIRMED",
    "INVOCATION_EVIDENCE_UNAVAILABLE",
})


def _section(instructions, heading):
    prefix = heading + "\n"
    return next((section[len(prefix):].rstrip() for section in instructions.split("\n## ")[1:]
                 if section.startswith(prefix)), "")


def _cleanup_state(cleanup):
    if not cleanup:
        return "unknown"
    return "confirmed" if cleanup.get("complete") is True and not cleanup.get("issues") else "unconfirmed"


def append_native_recovery(result, blockers):
    """Project the current invocation and its blockers from Host-owned receipts."""
    receipt = result.receipt
    code = result.diagnostics.get("code", "")
    definition = ERROR_CATALOG.get(code)
    render = receipt.get("candidate_render") or {}
    launch = receipt.get("launch_state") or receipt.get("producer_launch_state")
    cleanup = receipt.get("runtime_cleanup") or render.get("cleanup")
    if not (blockers or (definition and definition.procedure == "sandbox")
            or (launch and (code in {"TOOL_TIMEOUT", "TOOL_CRASH", "TOOL_CANCELLED"}
                            or (cleanup and cleanup.get("complete") is not True)))):
        return

    if launch is None and receipt.get("execution_state") == "not_started":
        launch = "not_started"
    # A retained candidate describes its earlier producer, not a new launch.
    if receipt.get("producer_observation") == "retained_candidate":
        launch, cleanup = "not_started", None
    effect = receipt.get("effect_state", "unknown")
    facts = {
        "launch_state": launch or "unknown",
        "effect_state": effect,
        "cleanup_state": _cleanup_state(cleanup),
    }
    if receipt.get("producer_observation"):
        # A private producer may have started even though the file service has
        # committed nothing to the formal workspace.
        facts.pop("effect_state")
        facts["formal_effect_state"] = effect
        facts["producer_completed"] = receipt.get("producer_completed")
        if "output_publication" in receipt:
            facts["output_publication"] = receipt["output_publication"]
    invocation_ids = receipt.get("native_invocations") or []
    if len(invocation_ids) == 1:
        facts["invocation_id"] = invocation_ids[0]
    if blockers:
        facts["earlier_invocations"] = [
            {**{key: item[key] for key in ("code", "invocation_id", "process_id", "execution_id",
               "call_index", "launch_state", "effect_state") if key in item},
             "cleanup_state": _cleanup_state(item.get("runtime_cleanup"))}
            for item in blockers[:4]
        ]
        if len(blockers) > 4:
            facts["earlier_invocation_count"] = len(blockers)
        heading = ("Earlier outcome unknown" if all(item.get("code") == "NATIVE_OUTCOME_UNCONFIRMED"
                   for item in blockers) else "Earlier invocation blocks admission")
    elif facts["cleanup_state"] == "unconfirmed":
        heading = "Current cleanup unconfirmed"
    elif code == "SANDBOX_LAUNCH_DENIED" and launch == "not_started" and effect == "not_committed":
        heading = "Confirmed launch refusal"
    elif launch != "not_started" and (effect in {"unknown", "ambiguous", "partial"}
            or code == "SANDBOX_LAUNCH_DENIED"):
        heading = "Current outcome requires reconciliation"
    else:
        heading = code
    instructions = load_prompt("guidance/sandbox.md")
    instruction = _section(instructions, heading) or _section(instructions, "Current outcome requires reconciliation")
    facts["guidance_ref"] = guidance_reference(code if definition else "sandbox")
    result.diagnostics["native_recovery"] = facts
    result.formatted += render_prompt("runtime/native_recovery",
        facts=json.dumps(facts, ensure_ascii=False, separators=(",", ":")), instruction=instruction.strip())


def lookup_guidance(identifier: str = "") -> dict:
    identifier = str(identifier).strip()
    procedures = sorted({d.procedure for d in (*ERROR_CATALOG.values(), *NOTICE_CATALOG.values())})
    result = {"kind": "procedure_help", "is_live_diagnostic": False,
              "authority_granted": False, "state_changed": False}
    if not identifier:
        return {**result, "procedures": procedures,
                "lookup": "Supply an exact catalog ID, symbolic name or procedure from this index."}
    entry = diagnostic_entry(identifier)
    if identifier in NATIVE_RECOVERY_REASONS:
        entry = {"kind": "recovery_reason", "name": identifier, "procedure": "sandbox",
                 "guidance_ref": guidance_reference(identifier)}
    procedure = entry["procedure"] if entry else identifier
    if procedure not in procedures:
        return {**result, "found": False, "identifier": identifier,
                "procedures": procedures, "message": "Unknown diagnostic or procedure; no recovery action was inferred."}
    instructions = load_prompt("guidance/" + procedure + ".md")
    detail = {}
    if entry:
        heading = None
        if procedure == "sandbox":
            heading = entry["name"]
        elif procedure == "file_mutation":
            heading = FILE_SECTIONS.get(entry["name"])
        if heading and (section := _section(instructions, heading)):
            instructions = f"## {heading}\n{section}"
            detail["full_guidance_ref"] = guidance_reference(procedure)
    return {**result, **detail, "found": True, "procedure": procedure,
            "diagnostic": entry, "instructions_format": "markdown",
            "instructions": instructions,
            "scope": "Explanation only. Use current task evidence, available tool schemas and any producer next_actions. Historical diagnostics do not establish a current block."}


def diagnostic_hint(code: str) -> str:
    """Short reference appended by an existing typed recovery producer."""
    entry = diagnostic_entry(code)
    if entry is None:
        raise KeyError(code)
    return render_prompt("runtime/diagnostic_hint", diagnostic=json.dumps(
        {key: entry[key] for key in ("catalog_id", "name", "guidance_ref")}, ensure_ascii=False))
