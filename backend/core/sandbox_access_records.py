"""Bounded private journals for sandbox access configuration."""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import json, os, tempfile
from pathlib import Path
from .sandbox import trusted_runtime_roots
MAX_RECORD_BYTES = 64 * 1024 * 1024

def concrete_roots(paths):
    roots = sorted(set(Path(p).resolve(strict=True) for p in paths),
                   key=lambda p: (len(p.parts), str(p)))
    if any(not p.is_dir() or p == Path(p.anchor) for p in roots):
        raise ValueError('A concrete directory is required, never a drive root')
    return roots


def record_path(path, roots):
    target = Path(path).resolve(strict=False)
    if any(target.is_relative_to(root) for root in (*roots, *trusted_runtime_roots())):
        raise ValueError('Recovery record must be outside workspace and exposed runtime trees')
    if not target.parent.is_dir():
        raise ValueError('Recovery record requires an existing private Host directory')
    return target


def save_record(path, payload, *, create=False):
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    if len(raw) > MAX_RECORD_BYTES:
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
