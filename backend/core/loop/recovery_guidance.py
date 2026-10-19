"""On-demand explanations of existing protocols; no grants, state or execution."""
from backend.core.errors import diagnostic_entry, ERROR_CATALOG, NOTICE_CATALOG
from backend.core.prompt_templates import load_prompt


def lookup_guidance(identifier: str = "") -> dict:
    identifier = str(identifier).strip()
    procedures = sorted({d.procedure for d in (*ERROR_CATALOG.values(), *NOTICE_CATALOG.values())})
    result = {"kind": "procedure_help", "is_live_diagnostic": False,
              "authority_granted": False, "state_changed": False}
    if not identifier:
        return {**result, "procedures": procedures,
                "lookup": "Supply an exact catalog ID, symbolic name or procedure from this index."}
    entry = diagnostic_entry(identifier)
    procedure = entry["procedure"] if entry else identifier
    if procedure not in procedures:
        return {**result, "found": False, "identifier": identifier,
                "procedures": procedures, "message": "Unknown diagnostic or procedure; no recovery action was inferred."}
    return {**result, "found": True, "procedure": procedure,
            "diagnostic": entry, "instructions_format": "markdown",
            "instructions": load_prompt("guidance/" + procedure + ".md"),
            "scope": "Explanation only. Use current task evidence, available tool schemas and any producer next_actions. Historical diagnostics do not establish a current block."}


def diagnostic_hint(code: str) -> str:
    """Short reference appended by an existing typed recovery producer."""
    import json
    from backend.core.prompt_templates import render_prompt
    entry = diagnostic_entry(code)
    if entry is None:
        raise KeyError(code)
    return render_prompt("runtime/diagnostic_hint", diagnostic=json.dumps(
        {key: entry[key] for key in ("catalog_id", "name", "guidance_ref")}, ensure_ascii=False))
