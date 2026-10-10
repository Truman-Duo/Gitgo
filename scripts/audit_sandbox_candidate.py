"""Read-only candidate alignment/format facts; never a sandbox test verdict.

Observed upstream must be obtained separately from the actual target. This
script does not fetch, merge, rewrite history, publish, or run product code.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess


TARGET_REPOSITORY = "Truman-Duo/Gitgo"
TARGET_REF = "refs/heads/master"


def git(repo: Path, *args: str) -> str:
    # Disable optional index refreshes. Audit output must not expose file paths
    # or invoke configured fsmonitor hooks while checking the working tree.
    command = ["git", "--no-optional-locks", "-C", str(repo),
               "-c", "core.fsmonitor=false", *args]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30)
    if result.returncode:
        raise ValueError("Git candidate inspection failed: " + args[0])
    return result.stdout.strip()


def commit_errors(title: str, config: dict) -> list[str]:
    match = re.fullmatch(r"(?:\[GITGO-(\d+)\] )?([a-z]+)\(([^)]+)\): (.+)", title)
    if not match:
        return ["format"]
    _, kind, scope, subject = match.groups()
    errors = []
    if kind not in config["validTypes"]:
        errors.append("type")
    if scope not in config["validScopes"]:
        errors.append("scope")
    if len(subject) > config["maxSubjectLength"]:
        errors.append("subject_length")
    if subject.endswith("."):
        errors.append("trailing_period")
    return errors


def audit(repo: Path, upstream: str, observed_at: str, *, candidate: str = "HEAD",
          integration_mode: str = "squash") -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", upstream):
        raise ValueError("Observed upstream must be a full lowercase commit SHA")
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Observation time must include a timezone")
    if integration_mode not in {"squash", "merge"}:
        raise ValueError("Unsupported integration mode")
    # Reject injected options even for a locally provided revision. Git's
    # end-of-options requires a sufficiently recent audited Git binary.
    head = git(repo, "rev-parse", "--verify", "--end-of-options", candidate + "^{commit}")
    git(repo, "rev-parse", "--verify", upstream + "^{commit}")
    config = json.loads(git(repo, "show", head + ":commit-config.json"))
    merge_base = git(repo, "merge-base", head, upstream)
    tree = git(repo, "rev-parse", head + "^{tree}")
    title = git(repo, "show", "-s", "--format=%s", head)
    history = git(repo, "log", upstream + ".." + head,
                  "--no-merges", "--format=%H%x09%s").splitlines()
    invalid, numbered = [], []
    for entry in history:
        sha, subject = entry.split("\t", 1)
        errors = commit_errors(subject, config)
        if errors:
            invalid.append({"sha": sha, "errors": errors})
        match = re.match(r"\[GITGO-(\d+)\] ", subject)
        if match:
            numbered.append({"sha": sha, "number": int(match.group(1))})
    numbered_debt = bool(numbered)
    # All private paths and commit prose stay local. Only structural violations
    # and public object IDs enter the report.
    dirty = bool(git(repo, "status", "--porcelain=v1", "--untracked-files=normal"))
    current_head = git(repo, "rev-parse", "HEAD")
    head_errors = commit_errors(title, config)
    blockers = []
    if head_errors:
        blockers.append("CANDIDATE_COMMIT_FORMAT_INVALID")
    if dirty:
        blockers.append("WORKTREE_NOT_CLEAN")
    if current_head != head:
        blockers.append("CHECKOUT_DOES_NOT_MATCH_CANDIDATE")
    if merge_base != upstream:
        blockers.append("UPSTREAM_NOT_INCORPORATED")
    if integration_mode == "merge" and (invalid or numbered_debt):
        blockers.append("HISTORY_REQUIRES_MAINTAINER_SQUASH")
    return {
        "report_schema": 1,
        "candidate_head": head,
        "target_repository": TARGET_REPOSITORY,
        "target_ref": TARGET_REF,
        "observed_upstream": upstream,
        "observed_at": observed_at,
        "upstream_observation_source": "caller_supplied_not_live_verified_by_script",
        "merge_base": merge_base,
        "candidate_tree": tree,
        "integration_candidate_tree": tree if merge_base == upstream else None,
        "tested_integration_tree": None,
        "policy_version": "effective_manifest_not_implemented",
        "execution_protocol_version": "host_envelope_not_implemented",
        "worktree_clean": not dirty,
        "checkout_matches_candidate": current_head == head,
        "integration_mode": integration_mode,
        "head_format_errors": head_errors,
        "historical_format_errors": invalid,
        "numbered_contributions_require_reallocation": numbered,
        "alignment_format_blockers": blockers,
        "security_review": "changes_required",
        "test_evidence": [],
        "sandbox_acceptance": "not_asserted",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--observed-upstream", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--integration-mode", choices=["squash", "merge"], default="squash")
    args = parser.parse_args(argv)
    try:
        report = audit(args.repo, args.observed_upstream, args.observed_at,
                       candidate=args.candidate, integration_mode=args.integration_mode)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError):
        print(json.dumps({"report_schema": 1, "error": "CANDIDATE_AUDIT_FAILED",
                          "sandbox_acceptance": "not_asserted"}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["alignment_format_blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
