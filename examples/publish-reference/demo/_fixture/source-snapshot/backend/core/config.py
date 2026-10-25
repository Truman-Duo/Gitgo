# Reference extract from Gitgo c8e7906e6ed5e23af29ebdbc51e4bfafe696926b
from __future__ import annotations
import json
import os
import sys
import threading
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional
from backend.models import FileAccessKind, RepoNode, SyncStatus
from backend.core.migrate import migrate_config_dict, needs_migration
DEFAULT_FORCE_EXCLUDE = ['CLAUDE.md', '.claude/', 'ANBM *', 'scripts/commit.sh', 'commit-config.json', '.git/', '__pycache__/', '*.pyc', '.venv/', '.env', '.env.*', 'llm_config.json', 'gitgo_config.json', 'sync_config.json', '.pytest_cache/']
DEFAULT_COMMIT_FORMAT = {'prefix': 'ANBM', 'number_start': 0, 'padding': False, 'plugins': [], 'template_name': 'default'}
DEFAULT_SECURITY_SCAN = {'enabled': True, 'severity_threshold': 'medium', 'ignored_rules': [], 'extra_patterns': []}

def default_outbound_policy() -> dict:
    """Canonical project policy for every outbound/publish path.

    The legacy force_exclude/security_scan/authorship fields remain readable for
    old configs, but new runtime code consumes this projection.
    """
    return {'enabled': True, 'exclude_paths': list(DEFAULT_FORCE_EXCLUDE), 'include_paths': [], 'severity_threshold': DEFAULT_SECURITY_SCAN['severity_threshold'], 'ignored_rules': [], 'extra_patterns': [], 'content_level': 2, 'deep_scan': False, 'approved_fingerprints': [], 'version': 1}

def normalize_outbound_policy(raw: dict | None, *, legacy: dict | None=None) -> dict:
    base = default_outbound_policy()
    legacy = dict(legacy or {})
    if legacy:
        base.update({'enabled': bool(legacy.get('enabled', True)), 'exclude_paths': list(legacy.get('force_exclude') or base['exclude_paths']), 'severity_threshold': str(legacy.get('severity_threshold') or base['severity_threshold']), 'ignored_rules': list(legacy.get('ignored_rules') or []), 'extra_patterns': list(legacy.get('extra_patterns') or []), 'content_level': int(legacy.get('content_level', 2) or 2), 'deep_scan': bool(legacy.get('deep_scan', False))})
    if isinstance(raw, dict):
        for key in base:
            if key in raw:
                base[key] = raw[key]
    base['enabled'] = bool(base['enabled'])
    for key in ('exclude_paths', 'include_paths', 'ignored_rules', 'extra_patterns', 'approved_fingerprints'):
        value = base.get(key)
        if not isinstance(value, list):
            raise ValueError(f'outbound_policy.{key} must be a list')
        base[key] = list(dict.fromkeys((str(item).strip() for item in value if str(item).strip())))
    base['content_level'] = max(1, min(3, int(base.get('content_level', 2) or 2)))
    base['deep_scan'] = bool(base.get('deep_scan', False))
    base['severity_threshold'] = str(base.get('severity_threshold') or 'medium').lower()
    if base['severity_threshold'] not in {'low', 'medium', 'high', 'critical'}:
        raise ValueError('outbound_policy.severity_threshold is invalid')
    base['version'] = max(1, int(base.get('version', 1) or 1))
    return base
