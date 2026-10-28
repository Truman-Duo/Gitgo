"""Bounded, explicit checks on a fixed Formal tree; configuration grants no authority."""
import re

from backend.core.sandbox_read import relative_parts
from .state import PublishError, digest

MAX_CHECKS = 8
MAX_SECONDS = 180


def normalize(values):
    if not isinstance(values, list) or len(values) > MAX_CHECKS:
        raise PublishError("PUBLISH_INVALID_CHECK_POLICY")
    checks, identifiers = [], set()
    for value in values:
        if not isinstance(value, dict) or set(value) - {"id", "target", "seeds", "timeout"}:
            raise PublishError("PUBLISH_INVALID_CHECK_POLICY")
        target = value.get("target")
        if (not isinstance(target, str) or not target or len(target) > 512 or target != target.strip()
                or target.startswith("-") or "\\" in target or any(ord(c) < 32 or ord(c) == 127 for c in target)):
            raise PublishError("PUBLISH_INVALID_TEST_TARGET")
        path = target.split("::", 1)[0]
        try:
            relative_parts(path)
            if any(part in {"", ".", ".."} for part in path.split("/")):
                raise ValueError("Nonportable test path")
        except (ValueError, RuntimeError, OSError) as exc:
            raise PublishError("PUBLISH_INVALID_TEST_TARGET") from exc
        seeds, timeout = value.get("seeds", [42]), value.get("timeout", 30)
        if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 4
                or any(type(seed) is not int or not 0 <= seed <= 2**32 - 1 for seed in seeds)
                or len(set(seeds)) != len(seeds) or type(timeout) is not int or not 1 <= timeout <= 120):
            raise PublishError("PUBLISH_INVALID_CHECK_POLICY")
        check = {"target": target, "seeds": list(seeds), "timeout": timeout}
        identifier = value.get("id", "pytest-" + digest(check)[:16])
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", identifier) or identifier in identifiers:
            raise PublishError("PUBLISH_INVALID_CHECK_POLICY")
        identifiers.add(identifier)
        checks.append({"id": identifier, **check})
    if sum(check["timeout"] * len(check["seeds"]) for check in checks) > MAX_SECONDS:
        raise PublishError("PUBLISH_CHECK_BUDGET_EXCEEDED")
    return checks


def requested(values, test_target):
    if values is not None and test_target:
        raise PublishError("PUBLISH_INVALID_CHECK_POLICY")
    return normalize(values if values is not None else [{"target": test_target, "timeout": 120}] if test_target else [])


def select(required, values, test_target):
    extra = requested(values, test_target)
    combined = {check["id"]: check for check in required}
    for check in extra:
        if check["id"] in combined and combined[check["id"]] != check:
            raise PublishError("PUBLISH_INVALID_CHECK_POLICY")
        combined[check["id"]] = check
    result = normalize(list(combined.values()))
    if not result:
        raise PublishError("PUBLISH_CHECKS_REQUIRED")
    return result


def planned(plan):
    # Preserve the declared semantics of older plans without a migration write.
    return normalize(plan["checks"] if "checks" in plan else [
        {"target": plan["test_target"], "seeds": plan["test_seeds"], "timeout": 120}])


def accepted(plan, result):
    rows = result.get("check_results", [])
    expected = planned(plan)
    return (not result.get("error") and not result.get("cleanup_pending")
            and isinstance(rows, list) and len(rows) == len(expected)
            and all(isinstance(row, dict) and row.get("check") == check
                    and row.get("passed") is True and row.get("verification_state") == "current"
                    for check, row in zip(expected, rows)))
