"""Project a review outcome without turning unavailable review into rejection."""


def review_feedback(outcome, fallback_code):
    error = dict(outcome.get("error") or {})
    code = str(error.get("code") or outcome.get("error_code") or fallback_code)
    info = dict(error.get("details") or {})
    failure = dict(outcome.get('failure') or (info.get("details") or {}).get("failure") or info.get('failure') or {})
    state = "unavailable" if code == "ALIGNMENT_REVIEW_FAILED" else (
        "needs_revision" if "alignment_repair" in outcome else str(outcome.get("status") or "not_admitted"))
    message = (error.get("message") or outcome.get("response")
               or outcome.get("alignment_repair") or "Requirement review did not admit this call.")
    data = {"error_code": code, "executed": False, "execution_state": "not_started",
            "effect_scope": "reviewed_calls", "review_state": state}
    if failure:
        data["failure"] = failure
    return str(message)[:1200], data
