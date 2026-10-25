# Reference extract from Gitgo c8e7906e6ed5e23af29ebdbc51e4bfafe696926b
from __future__ import annotations
import hashlib
import re
from pathlib import Path
from typing import Optional
from backend.adapters import GitRunner, LocalGitRunner
DEFAULT_SECURITY_PATTERNS: list[dict] = [{'id': 'aws_key', 'pattern': 'AKIA[0-9A-Z]{16}', 'severity': 'critical', 'label': 'AWS Access Key'}, {'id': 'private_key', 'pattern': '-----BEGIN\\s*(RSA\\s*|EC\\s*|OPENSSH\\s*)?PRIVATE KEY-----', 'severity': 'critical', 'label': 'Private key block'}, {'id': 'github_token', 'pattern': 'ghp_[A-Za-z0-9]{36}', 'severity': 'critical', 'label': 'GitHub classic token'}, {'id': 'github_fine_token', 'pattern': 'github_pat_[A-Za-z0-9_]{60,}', 'severity': 'critical', 'label': 'GitHub fine-grained token'}, {'id': 'provider_key', 'pattern': '\\bsk-[A-Za-z0-9_\\-]{16,}\\b', 'severity': 'critical', 'label': 'Provider API key'}, {'id': 'slack_token', 'pattern': 'xox[baprs]-[A-Za-z0-9\\-]{24,}', 'severity': 'high', 'label': 'Slack token'}, {'id': 'api_key', 'pattern': '[\'\\"]?(?:api[_-]?key|apikey|api_secret)[\'\\"]?\\s*[:=]\\s*[\'\\"]?[A-Za-z0-9_./+=\\-]{16,}', 'severity': 'high', 'label': 'API key assignment'}, {'id': 'token', 'pattern': '[\'\\"]?(?:access_token|auth_token|github_token)[\'\\"]?\\s*[:=]\\s*[\'\\"]?[^\'\\"\\s]{8,}', 'severity': 'high', 'label': 'Access token'}, {'id': 'password', 'pattern': '[\'\\"]?password[\'\\"]?\\s*[:=]\\s*[\'\\"]?[^\'\\"\\s]{4,}', 'severity': 'high', 'label': 'Password'}, {'id': 'generic_secret', 'pattern': '[\'\\"]?(?:secret|credential|passwd|pwd)[\'\\"]?\\s*[:=]\\s*[\'\\"]?[^\'\\"\\s]{8,}', 'severity': 'medium', 'label': 'Generic secret'}]
_SOURCE_CODE_SUFFIXES = {'.c', '.cc', '.cpp', '.cs', '.go', '.h', '.hpp', '.java', '.js', '.jsx', '.kt', '.kts', '.php', '.py', '.rb', '.rs', '.swift', '.ts', '.tsx'}

def _is_source_reference(file_path: str, matched: str) -> bool:
    """Return true only for an unquoted identifier used as a code value.

    Secret scanners must inspect source files too, but ``secret = value`` in
    Python/TypeScript names a variable, not embedded credential material.  The
    same text in ``.env`` or a config file remains suspicious.  Quoted literals
    are never exempted.
    """
    if Path(file_path).suffix.casefold() not in _SOURCE_CODE_SUFFIXES:
        return False
    pieces = re.split('[:=]', matched, maxsplit=1)
    if len(pieces) != 2:
        return False
    raw_value = pieces[1].strip()
    if not raw_value or raw_value[0] in {"'", '"'}:
        return False
    value = raw_value.rstrip(',;)')
    return bool(re.fullmatch('[A-Za-z_$][A-Za-z0-9_$]*(?:\\.[A-Za-z_$][A-Za-z0-9_$]*)*', value))

def scan_diff_for_secrets(diff: str, config: Optional[dict]=None) -> list[dict]:
    """Scan an arbitrary unified diff without returning matched material.

    Release push checks and Agent worktree sealing deliberately share this
    parser.  A private credential must not enter an internal Git object merely
    because that object is not itself intended for a remote.
    """
    patterns = [dict(item) for item in DEFAULT_SECURITY_PATTERNS]
    threshold = {'low': 0, 'medium': 1, 'high': 2, 'critical': 3}
    min_level = threshold.get((config or {}).get('severity_threshold', 'medium'), 1)
    ignored = set((config or {}).get('ignored_rules', []))
    for extra in (config or {}).get('extra_patterns', []):
        if isinstance(extra, dict) and 'pattern' in extra and ('id' in extra):
            item = dict(extra)
            item.setdefault('severity', 'medium')
            item.setdefault('label', item['id'])
            patterns.append(item)
    compiled = []
    for rule in patterns:
        severity = rule.get('severity', 'medium')
        kernel_rule = severity in {'high', 'critical'}
        if rule['id'] in ignored and (not kernel_rule):
            continue
        if threshold.get(severity, 1) < min_level and (not kernel_rule):
            continue
        try:
            compiled.append((rule, re.compile(rule['pattern'], re.IGNORECASE)))
        except re.error:
            continue
    warnings: list[dict] = []
    current_file = ''
    current_lnum = 0
    for line in diff.split('\n'):
        if line.startswith('+++ b/'):
            current_file = line[6:]
            continue
        if line.startswith('@@'):
            match = re.search('\\+(\\d+)(?:,\\d+)?', line)
            if match:
                current_lnum = int(match.group(1))
            continue
        if not line.startswith('+') or line.startswith('+++'):
            continue
        content = line[1:]
        exempted = bool(re.search('(?:#|//)\\s*gitgo-ignore-sensitive\\s*$', content))
        for rule, regex in compiled:
            match = regex.search(content)
            if not match:
                continue
            if exempted and rule.get('severity') == 'medium':
                continue
            matched = match.group()
            if rule.get('id') in {'api_key', 'token', 'password', 'generic_secret'} and _is_source_reference(current_file, matched):
                continue
            warnings.append({'rule_id': rule['id'], 'severity': rule['severity'], 'label': rule['label'], 'file': current_file, 'line': current_lnum, 'match_fingerprint': hashlib.sha256(matched.encode('utf-8')).hexdigest()[:16], 'match_length': len(matched)})
        current_lnum += 1
    seen: set[tuple[str, int, str]] = set()
    unique = []
    for warning in warnings:
        key = (warning['file'], warning['line'], warning['rule_id'])
        if key not in seen:
            seen.add(key)
            unique.append(warning)
    return unique
