"""安全检查 — 敏感信息扫描 + push diff 获取"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from backend.adapters import GitRunner, LocalFileAdapter

DEFAULT_SECURITY_PATTERNS: list[dict] = [
    {"id": "aws_key",          "pattern": r"AKIA[0-9A-Z]{16}",                           "severity": "critical", "label": "AWS Access Key"},
    {"id": "private_key",      "pattern": r"-----BEGIN\s*(RSA\s*|EC\s*|OPENSSH\s*)?PRIVATE KEY-----", "severity": "critical", "label": "私钥内容块"},
    {"id": "github_token",     "pattern": r"ghp_[A-Za-z0-9]{36}",                        "severity": "critical", "label": "GitHub 经典 Token"},
    {"id": "github_fine_token", "pattern": r"github_pat_[A-Za-z0-9]{82}",                 "severity": "critical", "label": "GitHub 细粒度 Token"},
    {"id": "slack_token",      "pattern": r"xox[baprs]-[A-Za-z0-9\-]{24,}",               "severity": "high",     "label": "Slack Token"},
    {"id": "api_key",          "pattern": r"(?:api[_-]?key|apikey|api_secret)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}", "severity": "high", "label": "API 密钥"},
    {"id": "token",            "pattern": r"(?:access_token|auth_token|github_token)\s*[:=]\s*['\"][^'\"]{8,}", "severity": "high", "label": "访问令牌"},
    {"id": "password",         "pattern": r"password\s*[:=]\s*['\"][^'\"]{4,}",            "severity": "high",     "label": "密码"},
    {"id": "generic_secret",   "pattern": r"(?:secret|credential|passwd|pwd)\s*[:=]\s*['\"][^'\"]{8,}", "severity": "medium", "label": "通用密钥"},
]


def _get_push_diff(
    backup_path: str = "",
    *,
    git_runner: GitRunner | None = None,
) -> str:
    """获取待推送的 diff（HEAD~1..HEAD 的新增行）"""
    if git_runner is None:
        if not backup_path:
            return ""
        git_runner = LocalFileAdapter(Path(backup_path).resolve())
    if not git_runner.is_git_repo():
        return ""
    return git_runner.diff(["HEAD~1..HEAD", "--unified=0"])


def _security_scan(
    backup_path: str = "",
    config: Optional[dict] = None,
    *,
    git_runner: GitRunner | None = None,
) -> list[dict]:
    """扫描待 push 内容中的敏感信息，返回警告列表。"""
    if config and not config.get("enabled", True):
        return []

    diff = _get_push_diff(backup_path, git_runner=git_runner)
    if not diff:
        return []

    patterns = list(DEFAULT_SECURITY_PATTERNS)

    threshold = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    min_level = threshold.get((config or {}).get("severity_threshold", "medium"), 1)

    ignored = set((config or {}).get("ignored_rules", []))

    for extra in (config or {}).get("extra_patterns", []):
        if isinstance(extra, dict) and "pattern" in extra and "id" in extra:
            extra.setdefault("severity", "medium")
            extra.setdefault("label", extra["id"])
            patterns.append(extra)

    compiled = []
    for rule in patterns:
        if rule["id"] in ignored:
            continue
        if threshold.get(rule["severity"], 1) < min_level:
            continue
        try:
            compiled.append((rule, re.compile(rule["pattern"], re.IGNORECASE)))
        except re.error:
            continue

    if not compiled:
        return []

    warnings: list[dict] = []
    current_file = ""
    current_lnum = 0

    for line in diff.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,\d+)?", line)
            if m:
                current_lnum = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            if re.search(r"(?:#|//)\s*gitgo-ignore-sensitive\s*$", content):
                current_lnum += 1
                continue
            for rule, regex in compiled:
                m = regex.search(content)
                if m:
                    match_text = m.group()
                    if len(match_text) > 40:
                        match_text = match_text[:20] + "..." + match_text[-17:]
                    warnings.append({
                        "rule_id": rule["id"],
                        "severity": rule["severity"],
                        "label": rule["label"],
                        "file": current_file,
                        "line": current_lnum,
                        "match": match_text,
                    })
            current_lnum += 1

    seen: set[tuple[str, int, str]] = set()
    unique: list[dict] = []
    for w in warnings:
        key = (w["file"], w["line"], w["rule_id"])
        if key not in seen:
            seen.add(key)
            unique.append(w)

    return unique
