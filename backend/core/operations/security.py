"""Non-bypassable release privacy scan and outbound Git diff inspection."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

from backend.adapters import GitRunner, LocalGitRunner


DEFAULT_SECURITY_PATTERNS: list[dict] = [
    {"id": "aws_key", "pattern": r"AKIA[0-9A-Z]{16}",
     "severity": "critical", "label": "AWS Access Key"},
    {"id": "private_key",
     "pattern": r"-----BEGIN\s*(RSA\s*|EC\s*|OPENSSH\s*)?PRIVATE KEY-----",
     "severity": "critical", "label": "Private key block"},
    {"id": "github_token", "pattern": r"ghp_[A-Za-z0-9]{36}",
     "severity": "critical", "label": "GitHub classic token"},
    {"id": "github_fine_token", "pattern": r"github_pat_[A-Za-z0-9_]{60,}",
     "severity": "critical", "label": "GitHub fine-grained token"},
    {"id": "provider_key", "pattern": r"\bsk-[A-Za-z0-9_\-]{16,}\b",
     "severity": "critical", "label": "Provider API key"},
    {"id": "slack_token", "pattern": r"xox[baprs]-[A-Za-z0-9\-]{24,}",
     "severity": "high", "label": "Slack token"},
    {"id": "api_key",
     "pattern": r"['\"]?(?:api[_-]?key|apikey|api_secret)['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_./+=\-]{16,}",
     "severity": "high", "label": "API key assignment"},
    {"id": "token",
     "pattern": r"['\"]?(?:access_token|auth_token|github_token)['\"]?\s*[:=]\s*['\"]?[^'\"\s]{8,}",
     "severity": "high", "label": "Access token"},
    {"id": "password",
     "pattern": r"['\"]?password['\"]?\s*[:=]\s*['\"]?[^'\"\s]{4,}",
     "severity": "high", "label": "Password"},
    {"id": "generic_secret",
     "pattern": r"['\"]?(?:secret|credential|passwd|pwd)['\"]?\s*[:=]\s*['\"]?[^'\"\s]{8,}",
     "severity": "medium", "label": "Generic secret"},
]

_SOURCE_CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
    ".jsx", ".kt", ".kts", ".php", ".py", ".rb", ".rs", ".swift",
    ".ts", ".tsx",
}


def _is_source_reference(file_path: str, matched: str) -> bool:
    """Return true only for an unquoted identifier used as a code value.

    Secret scanners must inspect source files too, but ``secret = value`` in
    Python/TypeScript names a variable, not embedded credential material.  The
    same text in ``.env`` or a config file remains suspicious.  Quoted literals
    are never exempted.
    """
    if Path(file_path).suffix.casefold() not in _SOURCE_CODE_SUFFIXES:
        return False
    pieces = re.split(r"[:=]", matched, maxsplit=1)
    if len(pieces) != 2:
        return False
    raw_value = pieces[1].strip()
    if not raw_value or raw_value[0] in {"'", '"'}:
        return False
    value = raw_value.rstrip(",;)")
    return bool(re.fullmatch(
        r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*",
        value,
    ))


def _get_push_diff(
    backup_path: str = "", *, git_runner: GitRunner | None = None,
) -> str | None:
    """Return the complete diff not present in the configured upstream."""
    if git_runner is None:
        if not backup_path:
            return None
        git_runner = LocalGitRunner(Path(backup_path).resolve())
    if not git_runner.is_git_repo():
        return None
    upstream = git_runner.run(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        timeout=15,
    )
    base = upstream.stdout.strip() if upstream.returncode == 0 else ""
    revision = (
        f"{base}..HEAD" if base
        else "4b825dc642cb6eb9a060e54bf8d69288fbee4904..HEAD"
    )
    result = git_runner.run(
        ["diff", "--unified=0", "--no-ext-diff", revision, "--"],
        timeout=60,
    )
    return result.stdout if result.returncode == 0 else None


def _unavailable(rule_id: str, label: str) -> dict:
    return {
        "rule_id": rule_id, "severity": "critical", "label": label,
        "file": "", "line": 0,
    }


def _scan_private_tree(git_runner: GitRunner) -> list[dict]:
    """Detect path- or content-hidden tool configs in release HEAD."""
    from backend.core.authorship import (
        is_ai_config_file, looks_like_private_tool_content,
    )

    listing = git_runner.run(
        ["ls-tree", "-r", "-z", "--name-only", "HEAD"], timeout=30,
    )
    if listing.returncode != 0:
        return [_unavailable(
            "private_tree_scan_unavailable", "Private tree scan unavailable",
        )]
    warnings = []
    paths = [path for path in listing.stdout.split("\0") if path]
    # Ask Git to identify text blobs containing the high-confidence markers.
    # This deliberately has no filename-extension allow-list: renaming
    # CLAUDE.md to an arbitrary extension must not bypass the release guard.
    candidates_result = git_runner.run(
        [
            "grep", "-I", "-l", "-i", "-E",
            r"CLAUDE\.md|instructions for claude|claude instructions|you are claude",
            "HEAD", "--",
        ],
        timeout=30,
    )
    if candidates_result.returncode not in (0, 1):
        return [_unavailable(
            "private_tree_scan_unavailable", "Private tree scan unavailable",
        )]
    candidates = {
        line.removeprefix("HEAD:")
        for line in candidates_result.stdout.splitlines() if line
    }
    for rel_path in paths:
        private = is_ai_config_file(rel_path)
        if not private and rel_path in candidates:
            try:
                blob = git_runner.run(["show", f"HEAD:{rel_path}"], timeout=15)
            except (OSError, UnicodeError):
                return [_unavailable(
                    "private_tree_scan_unavailable", "Private tree scan unavailable",
                )]
            if blob.returncode != 0:
                return [_unavailable(
                    "private_tree_scan_unavailable", "Private tree scan unavailable",
                )]
            private = looks_like_private_tool_content(blob.stdout)
        if private:
            warnings.append({
                "rule_id": "private_artifact", "severity": "critical",
                "label": "Private tool/config artifact", "file": "", "line": 0,
                "path_fingerprint": hashlib.sha256(
                    rel_path.encode("utf-8")
                ).hexdigest()[:16],
            })
    return warnings


def _security_scan(
    backup_path: str = "", config: Optional[dict] = None, *,
    git_runner: GitRunner | None = None,
) -> list[dict]:
    """Scan all outbound commits without returning matched secret material."""
    if git_runner is None:
        if not backup_path:
            return [_unavailable(
                "push_diff_scan_unavailable", "Push diff scan unavailable",
            )]
        git_runner = LocalGitRunner(Path(backup_path).resolve())

    warnings = _scan_private_tree(git_runner)
    if warnings:
        return warnings
    diff = _get_push_diff(backup_path, git_runner=git_runner)
    if diff is None:
        return [_unavailable(
            "push_diff_scan_unavailable", "Push diff scan unavailable",
        )]
    if not diff:
        return []

    warnings.extend(scan_diff_for_secrets(diff, config=config))
    approved = {
        str(item).lower() for item in (config or {}).get("approved_fingerprints", [])
    }
    if approved:
        warnings = [
            warning for warning in warnings
            if str(
                warning.get("match_fingerprint")
                or warning.get("path_fingerprint")
                or ""
            ).lower() not in approved
        ]
    return warnings


def scan_diff_for_secrets(diff: str, config: Optional[dict] = None) -> list[dict]:
    """Scan an arbitrary unified diff without returning matched material.

    Release push checks and Agent worktree sealing deliberately share this
    parser.  A private credential must not enter an internal Git object merely
    because that object is not itself intended for a remote.
    """
    patterns = [dict(item) for item in DEFAULT_SECURITY_PATTERNS]
    threshold = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    min_level = threshold.get((config or {}).get("severity_threshold", "medium"), 1)
    ignored = set((config or {}).get("ignored_rules", []))
    for extra in (config or {}).get("extra_patterns", []):
        if isinstance(extra, dict) and "pattern" in extra and "id" in extra:
            item = dict(extra)
            item.setdefault("severity", "medium")
            item.setdefault("label", item["id"])
            patterns.append(item)

    compiled = []
    for rule in patterns:
        severity = rule.get("severity", "medium")
        kernel_rule = severity in {"high", "critical"}
        if rule["id"] in ignored and not kernel_rule:
            continue
        if threshold.get(severity, 1) < min_level and not kernel_rule:
            continue
        try:
            compiled.append((rule, re.compile(rule["pattern"], re.IGNORECASE)))
        except re.error:
            continue

    warnings: list[dict] = []
    current_file = ""
    current_lnum = 0
    for line in diff.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,\d+)?", line)
            if match:
                current_lnum = int(match.group(1))
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        content = line[1:]
        exempted = bool(re.search(
            r"(?:#|//)\s*gitgo-ignore-sensitive\s*$", content,
        ))
        for rule, regex in compiled:
            match = regex.search(content)
            if not match:
                continue
            if exempted and rule.get("severity") == "medium":
                continue
            matched = match.group()
            if rule.get("id") in {
                "api_key", "token", "password", "generic_secret",
            } and _is_source_reference(current_file, matched):
                continue
            warnings.append({
                "rule_id": rule["id"], "severity": rule["severity"],
                "label": rule["label"], "file": current_file,
                "line": current_lnum,
                "match_fingerprint": hashlib.sha256(
                    matched.encode("utf-8")
                ).hexdigest()[:16],
                "match_length": len(matched),
            })
        current_lnum += 1

    seen: set[tuple[str, int, str]] = set()
    unique = []
    for warning in warnings:
        key = (warning["file"], warning["line"], warning["rule_id"])
        if key not in seen:
            seen.add(key)
            unique.append(warning)
    return unique
