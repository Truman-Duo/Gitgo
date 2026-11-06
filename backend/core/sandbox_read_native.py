"""Stage 2 candidate adapter; existing readers are not switched over yet.

Native tokens are live resource ownership only. They are never serialized into
read approvals, child requests or persisted as a substitute for authorization.
The loader's minimal Windows bootstrap remains independent of this adapter.
"""
from __future__ import annotations

import ctypes as C
import os
from pathlib import Path
import sys
import threading

from backend.core.native_sandbox import NativeBoundaryError, get_native
from backend.core.sandbox_read import MAX_READ_BYTES, ObjectIdentity, ReadBoundaryError, relative_parts


def _path(path):
    return list(str(path).encode('utf-16-le', errors='surrogatepass')
                if sys.platform == 'win32' else os.fsencode(path))


def _call(library, **request):
    try:
        return library.call(**request)
    except NativeBoundaryError as error:
        # An unreadable/missing selected object is a known boundary refusal.
        # Transport/panic failures retain their original unknown-outcome flag.
        if error.outcome_unknown:
            raise
        # NativeLibrary retains the structured error below; no errno guessing
        # from arbitrary text and no retry through the old reader.
        result = getattr(error, 'response', {})
        kind = result.get('read_error')
        cls = {'NotFound': FileNotFoundError, 'IsADirectory': IsADirectoryError,
               'NotADirectory': NotADirectoryError}.get(kind, ReadBoundaryError)
        raise cls(result.get('os_error') or 0, result.get('error', 'Native read refused')) from error


def _identity(response):
    record = response['identity']
    return ObjectIdentity(sys.platform, record['volume'], int(record['file_id']))


class NativePinnedObject:
    def __init__(self, library, response):
        self._library = library
        self._token = response['token']
        self._lock = threading.RLock()
        self._closed = False
        try:
            self.identity = _identity(response)
        except BaseException:
            self.close()
            raise

    def close(self):
        with self._lock:
            if not self._closed:
                _call(self._library, op='read_close', token=self._token)
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class NativePinnedRead(NativePinnedObject):
    def read_bytes(self, *, max_bytes=MAX_READ_BYTES):
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_READ_BYTES:
            raise ValueError('Invalid bounded read size')
        with self._lock:
            if self._closed:
                raise ValueError('Read object is closed')
            if self._library._sealed:
                raise NativeBoundaryError('Native boundary is sealed')
            output, written = C.create_string_buffer(max_bytes), C.c_size_t()
            function = self._library._library.gitgo_native_read
            function.argtypes = [C.c_uint64, C.c_size_t, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
            function.restype = C.c_uint32
            status = function(self._token, max_bytes, output, len(output), C.byref(written))
            if status == 3:
                raise ReadBoundaryError('Native bounded read refused; no content exported')
            if status or written.value > max_bytes:
                self._library._sealed = True
                raise NativeBoundaryError('Native read transport failed; no replay')
            return C.string_at(output, written.value)

    def sha256(self, *, max_bytes=512 * 1024 * 1024):
        if type(max_bytes) is not int or not 0 < max_bytes <= 512 * 1024 * 1024:
            raise ValueError('Invalid bounded hash read')
        with self._lock:
            if self._closed:
                raise ValueError('Read object is closed')
            result = _call(self._library, op='read_hash', token=self._token, max_bytes=max_bytes)
            return result['sha256'], result['size']


class NativeReadRoot(NativePinnedObject):
    def __init__(self, path: Path):
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts or path == Path(path.anchor):
            raise ReadBoundaryError('A concrete absolute resource root is required')
        library = get_native()
        if library.call(op='capabilities').get('safe_read_version') != 1:
            raise ReadBoundaryError('Native safe read extension is unavailable')
        super().__init__(library, _call(library, op='read_root', path=_path(path)))

    def _open(self, relative, metadata_only):
        parts = relative_parts(relative)
        with self._lock:
            if self._closed:
                raise ValueError('Read root is closed')
            response = _call(self._library, op='read_open', token=self._token,
                             path=_path(str(Path(*parts))), metadata_only=metadata_only)
            cls = NativePinnedObject if metadata_only else NativePinnedRead
            return cls(self._library, response)

    def pin(self, relative):
        return self._open(relative, True)

    def open(self, relative):
        return self._open(relative, False)
