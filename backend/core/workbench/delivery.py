"""Check explicit, visible delivery statements against pinned workbench results."""
from decimal import Decimal
import json

from backend.core.loop.alignment_evidence import answer_digest
from backend.core.loop.mechanism_lifecycle import fence
from backend.core.loop.models import ProcessStatus
from backend.core.storage.redaction import redact_for_persistence
from .data import _scalar_identity, parse_json


CONTEXT_KEY = "workbench_delivery"
MAX_CLAIMS = 12


def claim_scope(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 120
            or value != value.strip() or not value[0].isalnum() or value.endswith(" (historical snapshot)")
            or not all(c.isalnum() or c in " _-/()." for c in value)
            or redact_for_persistence(value) != value):
        raise ValueError("scope must be a short plain-text label")
    return value


def associate(process, sources, *, scope, result_ref, candidate, basis="current"):
    scope = claim_scope(scope)
    if basis not in {"current", "historical"}:
        raise ValueError("basis must be current or historical")
    if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 64_000:
        raise ValueError("candidate must be nonempty and at most 64000 characters")
    result = sources.read_result(result_ref)
    workspace = str(sources.contexts.workspace)
    task_id = process.active_task_id or process.process_id
    claim = {"scope": scope, "result_ref": dict(result_ref), "basis": basis}
    check = _check(claim, candidate, result)

    def update(context):
        state = context.get(CONTEXT_KEY) or {}
        same_task = state.get("task_id") == task_id and state.get("process_id") == process.process_id
        claims = {item["scope"]: item for item in state["claims"]} if same_task else {}
        if scope not in claims and len(claims) >= MAX_CLAIMS:
            raise ValueError("a task may associate at most 12 workbench statements")
        if claims and state["workspace"] != workspace:
            raise ValueError("workbench statements are bound to this task's original workspace")
        claims[scope] = claim
        return {**context, CONTEXT_KEY: {"task_id": task_id, "process_id": process.process_id,
                                       "workspace": workspace, "claims": list(claims.values())}}

    with fence(process):
        if process.status is not ProcessStatus.RUNNING:
            raise ValueError("delivery statements require a running task")
        process.update_context_snapshot(update)
    return check


def _fragments(candidate, label):
    prefix = label + ": "
    lines = candidate.replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != "```text" or "```" not in lines:
        return None
    end = lines.index("```")
    matches = [i for i, line in enumerate(lines) if line.startswith(label + ":")]
    if len(matches) == 1 and 0 < matches[0] < end:
        line = lines[matches[0]]
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _compare(value, result):
    if not value or len(value.encode("utf-8")) > 8_000:
        raise ValueError("a bounded JSON scalar or scalar array is required")
    if result["value_type"] == "scalar_set_json":
        claimed = parse_json(value)
        if not isinstance(claimed, list):
            raise ValueError("a JSON array is required for a set")
        claimed_members = {_scalar_identity(item) for item in claimed}
        actual_members = {_scalar_identity(item) for item in parse_json(result["value"])}
        return len(claimed) == len(claimed_members) and claimed_members == actual_members
    claimed = json.loads(value, parse_int=Decimal, parse_float=Decimal)
    if not isinstance(claimed, Decimal) or not claimed.is_finite():
        raise ValueError("a finite JSON number is required")
    return claimed == Decimal(result["value"])


def _check(claim, candidate, result):
    historical = claim["basis"] == "historical"
    label = claim["scope"] + (" (historical snapshot)" if historical else "")
    fragment = _fragments(candidate, label)
    check = {**claim, "candidate_digest": answer_digest(candidate)}
    if result is None:
        check.update(status="insufficient", reason="the pinned result or its dependency is unavailable")
    else:
        applicability = result["applicability"]
        check.update(applicability=applicability, dependencies=result["dependencies"],
                     source_ref=result["source"].get("ref"),
                     source_digest=result["source"].get("origin_digest") or result["source"]["snapshot_sha256"],
                     coverage=result["result"]["coverage"])
        if result["source"].get("redacted") or result["source"]["kind"] == "provided_data":
            check.update(status="insufficient", reason="the source is redacted or model-assembled")
        elif applicability["reason"] == "source_version_changed" and not historical:
            check.update(status="stale", reason="a source version changed")
        elif (applicability["state"] != "current"
                and not (historical and applicability["reason"] == "source_version_changed")):
            check.update(status="insufficient", reason=applicability["reason"])
        elif fragment == "unverified":
            check.update(status="uncertain", reason="the visible line explicitly leaves the claim unverified")
        else:
            try:
                matches = _compare(fragment, result["result"])
            except (ValueError, ArithmeticError, RecursionError):
                check.update(status="uncertain", reason=f'place one "{label}: <JSON value>" line in the opening fenced text block')
            else:
                display = str(result["result"]["value"])
                mismatch = (f'the pinned result is "{label}: {display}"' if len(display) <= 240
                            else "the visible value differs from the pinned result. Use read_result to inspect it")
                check.update(status="match" if matches else "contradiction",
                             reason="the visible value matches the pinned result" if matches else mismatch)
    # Optional scopes can acknowledge unavailable evidence explicitly. A scope
    # in the task contract still requires a match.
    check["qualified"] = fragment == "unverified"
    return check


def assess_delivery(process, context, candidate):
    """Use every Runtime-associated scope and every scope required by the contract."""
    required = (context.get("task_contract") or {}).get("required_workbench_claims") or []
    task_id = getattr(process, "active_task_id", "") or process.process_id
    state = context.get(CONTEXT_KEY) or {}
    same_task = state.get("task_id") == task_id and state.get("process_id") == process.process_id
    claims = {item["scope"]: item for item in state["claims"]} if same_task else {}
    if not claims and not required:
        return []
    from backend.core.loop.protocol_tools.workbench import bound_sources

    sources = bound_sources(process, state["workspace"]) if claims else None
    checks = []
    for scope in dict.fromkeys([*required, *claims]):
        claim = claims.get(scope)
        if claim is None:
            checks.append({"scope": scope, "status": "insufficient", "blocking": True,
                           "candidate_digest": answer_digest(candidate),
                           "reason": "the task contract requires a workbench result for this scope"})
            continue
        try:
            result = sources.read_result(claim["result_ref"], context=context) if sources else None
        except (KeyError, ValueError, OSError):
            result = None
        check = _check(claim, candidate, result)
        check["blocking"] = check["status"] != "match" and (scope in required or not check["qualified"])
        if check["blocking"] and check["status"] in {"stale", "insufficient"}:
            if scope not in required:
                label = scope + (" (historical snapshot)" if claim["basis"] == "historical" else "")
                check["reason"] += f'. Obtain stronger evidence or use "{label}: unverified"'
        checks.append(check)
    return checks
