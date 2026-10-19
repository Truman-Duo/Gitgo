"""Bounded evidence for files explicitly published by an Agent completion claim.

This verifies an authorized committed write or explicitly reviewed snapshot, not a general
file-reading capability or a decision that a file satisfies the user's goal.
"""
from hashlib import sha256
from pathlib import Path
from backend.core.tools.workspace_tools import _confined_path
from .candidate_evidence import candidate_segments

MAX_ARTIFACT_BYTES = 64_000
MAX_ARTIFACTS = 8


def _root(process):
    value = process.worktree_path or process.workspace_root
    if not value:
        raise ValueError('artifact verification requires an explicit workspace')
    return Path(value).resolve()


def _witness(process, path):
    task_id = process.active_task_id or process.process_id
    receipts = {r.get('receipt_id'): r for r in process.tool_receipts
                if r.get('task_id') == task_id and r.get('succeeded')
                and r.get('committed') and r.get('effect_state') == 'committed'
                and r.get('effect') == 'workspace_write'}
    # Use the current process's real tool result and receipt together. A model's
    # claimed hash, a failed write or a previous task's file is not a witness.
    for message in reversed(process.session.messages):
        if message.get('message_type') != 'tool_result' or message.get('is_error'):
            continue
        receipt = message.get('receipt') or {}
        if not receipt.get('receipt_id') or receipts.get(receipt['receipt_id']) != receipt:
            continue
        data = message.get('data') or {}
        # Native operations may commit several files atomically (promotion).
        # All witnesses still come from the real, task-bound tool result and
        # must be within the canonical receipt's file scope.
        entries = data.get('files')
        candidates = [data, *(item for item in (entries if isinstance(entries, list) else [])
                               if isinstance(item, dict))]
        for item in candidates:
            raw = item.get('path')
            if not raw or raw not in receipt.get('files', []):
                continue
            try:
                observed = _confined_path(_root(process), raw, must_exist=False)
            except (ValueError, OSError):
                continue
            if observed == path:
                return (receipt['receipt_id'], str(item.get('after_sha256') or ''),
                        str(item.get('evidence_kind') or 'committed_write'))
    return '', '', ''


def _read(process, raw, remaining):
    root = _root(process)
    path = _confined_path(root, raw, must_exist=True)
    receipt_id, expected, evidence_kind = _witness(process, path)
    if not receipt_id or len(expected) != 64:
        raise ValueError('no current committed write evidence for artifact')
    # Bound before allocation and after reading, including concurrent growth.
    if not path.is_file() or path.stat().st_size > remaining:
        raise ValueError('artifact exceeds the bounded text evidence budget')
    with path.open('rb') as stream:
        content = stream.read(remaining + 1)
    if len(content) > remaining or sha256(content).hexdigest() != expected:
        raise ValueError('artifact changed since its committed write')
    text = content.decode('utf-8-sig')
    if '\x00' in text:
        raise ValueError('artifact is not supported text evidence')
    relative = path.relative_to(root).as_posix()
    return {'path': relative, 'sha256': expected, 'receipt_id': receipt_id,
            'bytes': len(content), 'evidence_kind': evidence_kind, 'segments': [
                {**part, 'id': f'artifact:{receipt_id}:{part["id"]}'}
                for part in candidate_segments(text)]}


def collect_artifacts(process, files):
    artifacts, notices, used, seen = [], [], 0, set()
    if not isinstance(files, (list, tuple)) or len(files) > MAX_ARTIFACTS:
        return [], ['Published file list exceeds the bounded evidence limit.']
    for raw in files:
        try:
            if not isinstance(raw, str):
                raise ValueError('artifact path must be text')
            artifact = _read(process, raw, MAX_ARTIFACT_BYTES - used)
            if artifact['path'] in seen:
                continue
            seen.add(artifact['path'])
            used += artifact['bytes']
            artifacts.append(artifact)
        except (ValueError, OSError, UnicodeError) as error:
            notices.append(f'{raw}: {error}')
    return artifacts, notices


def artifact_manifest(artifacts):
    return [{k: item[k] for k in ('path', 'sha256', 'receipt_id', 'bytes')} for item in artifacts]


def verify_manifest(process, manifest, files):
    artifacts, notices = collect_artifacts(process, files)
    current = artifact_manifest(artifacts)
    return not notices and all(item in current for item in manifest)
