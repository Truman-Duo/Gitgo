"""Bounded private journals for sandbox access configuration."""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import json, os, tempfile
from pathlib import Path
from .sandbox import SandboxDenied, trusted_runtime_roots
MAX_RECORD_BYTES = 64 * 1024 * 1024
SCOPED_ACCESS_KIND = 'scoped_workspace_access'
SCOPED_ACCESS_VERSION = 2
_ACL_SECTIONS = ('records', 'planned', 'boundaries')
SCOPED_ACCESS_EVIDENCE = (*_ACL_SECTIONS, 'descriptors')
_SCOPED_ACCESS_PHASES = {'prepared', 'applying', 'applied', 'prepare_failed',
                        'revoking', 'revoke_failed', 'revoked'}

def concrete_roots(paths, *, allow_files=False):
    roots = sorted(set(Path(p).resolve(strict=True) for p in paths),
                   key=lambda p: (len(p.parts), str(p)))
    if any((not p.is_dir() and not (allow_files and p.is_file())) or p == Path(p.anchor) for p in roots):
        raise ValueError('A concrete runtime object is required, never a drive root' if allow_files
                         else 'A concrete directory is required, never a drive root')
    return roots


def record_path(path, roots):
    target = Path(path).resolve(strict=False)
    if any(target.is_relative_to(root) for root in (*roots, *trusted_runtime_roots())):
        raise ValueError('Recovery record must be outside workspace and exposed runtime trees')
    if not target.parent.is_dir():
        raise ValueError('Recovery record requires an existing private Host directory')
    return target


def pack_access_evidence(records, planned, boundaries):
    """Preserve each object's identity and both ACL states, sharing repeated descriptors."""
    descriptors, indices = [], {}
    evidence = {}
    for key, section in zip(_ACL_SECTIONS, (records, planned, boundaries), strict=True):
        rows = []
        for record in section:
            descriptor = (record['descriptor'], record['sddl'])
            index = indices.get(descriptor)
            if index is None:
                index = indices[descriptor] = len(descriptors)
                descriptors.append(descriptor)
            rows.append((record['path'], record['device'], record['inode'], index))
        evidence[key] = rows
    return {**evidence, 'descriptors': descriptors}


def load_scoped_access_record(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError('Recovery record exceeds 64 MiB')
    payload = json.loads(raw)
    if (type(payload) is not dict or payload.get('kind') != SCOPED_ACCESS_KIND
            or type(payload.get('version')) is not int
            or payload.get('version') not in (1, SCOPED_ACCESS_VERSION)
            or type(payload.get('phase')) is not str
            or payload.get('phase') not in _SCOPED_ACCESS_PHASES):
        raise ValueError('Unsupported scoped access journal')
    if payload['version'] == SCOPED_ACCESS_VERSION and payload['phase'] != 'revoked':
        descriptors = payload.get('descriptors')
        if type(descriptors) is not list or any(type(pair) is not list or len(pair) != 2
                or any(type(value) is not str for value in pair) for pair in descriptors):
            raise ValueError('Invalid scoped access descriptor table')
        for key in _ACL_SECTIONS:
            rows = payload.get(key)
            if type(rows) is not list or any(type(row) is not list or len(row) != 4
                    or type(row[0]) is not str or type(row[1]) is not int or type(row[2]) is not str
                    or type(row[3]) is not int or not 0 <= row[3] < len(descriptors) for row in rows):
                raise ValueError('Invalid scoped access object record')
    return payload


def save_record(path, payload, *, create=False):
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_RECORD_BYTES:
        if payload.get('kind') == SCOPED_ACCESS_KIND:
            raise SandboxDenied('SANDBOX_ACCESS_JOURNAL_CAPACITY',
                'The complete scoped access journal exceeds its size limit.',
                details={'size_bytes': len(raw), 'limit_bytes': MAX_RECORD_BYTES})
        raise ValueError('Recovery record exceeds 64 MiB')
    if create:
        with open(path, 'xb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        return
    descriptor, temporary = tempfile.mkstemp(prefix='.gitgo-acl-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sid_string(api, sid):
    value = W.LPWSTR()
    convert = api.advapi.ConvertSidToStringSidW
    convert.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    convert.restype = W.BOOL
    api.check(convert(sid, C.byref(value)))
    try:
        return value.value
    finally:
        free = api.kernel.LocalFree
        free.argtypes = [C.c_void_p]
        free.restype = C.c_void_p
        free(C.cast(value, C.c_void_p))
