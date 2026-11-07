"""Pinned Rust ABI. This module owns no permissions and no recovery ledger.

No environment-selected library, PATH search, download or ctypes execution
fallback. A missing/incompatible native module makes Windows execution deny.
"""
from __future__ import annotations
from contextlib import ExitStack
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import threading

ABI = 1
MAX_REQUEST = 1024 * 1024
RESPONSE_CAPACITY = 65536
_lock = threading.RLock()
_loaded = None  # Retain loaded code AND file/ancestor pins for process lifetime.
_failed_loads = []


class NativeBoundaryError(OSError):
    outcome_unknown = True


def installed_path():
    name = {"win32": "gitgo_native_sandbox.dll", "darwin": "libgitgo_native_sandbox.dylib",
            "linux": "libgitgo_native_sandbox.so"}.get(sys.platform)
    if name is None:
        raise NativeBoundaryError("No supported native module ABI")
    return Path(__file__).absolute().parents[1] / "native_runtime" / name


def _pin_windows(path, owners):
    """Pin the complete loader name against rename and the image against writes.

    Bootstrap uses only stable read APIs. The file we hash stays open without
    write/delete sharing through LoadLibraryEx and for the loaded lifetime.
    """
    import msvcrt
    from backend.core.sandbox_read_windows import WindowsReadApi
    api = WindowsReadApi()
    if not path.is_absolute() or len(path.drive) != 2 or '..' in path.parts:
        raise NativeBoundaryError("Native module requires a local absolute path")
    parent = api.open('\\??\\' + path.anchor, None, directory=True)
    owners.callback(api.close, parent)
    api.identity(parent, directory=True)
    for part in path.parts[1:-1]:
        parent = api.open(part, parent, directory=True)
        owners.callback(api.close, parent)
        api.identity(parent, directory=True)
    handle = api.open(path.name, parent, directory=False, share_write=False)
    try:
        identity = api.identity(handle, directory=False)
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.close(handle)
        raise
    owners.callback(os.close, fd)
    return fd, identity


def _hash(fd):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= 512 * 1024 * 1024:
        raise NativeBoundaryError("Native module is not a bounded singly linked image")
    digest = hashlib.sha256()
    size = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 65536):
        size += len(chunk)
        if size > 512 * 1024 * 1024:
            raise NativeBoundaryError("Native module grew beyond its bound")
        digest.update(chunk)
    after = os.fstat(fd)
    fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
    if fields(before) != fields(after) or size != before.st_size:
        raise NativeBoundaryError("Native module changed while identifying it")
    return digest.hexdigest(), size


class NativeLibrary:
    def __init__(self, path):
        self._pins = ExitStack()
        self._sealed = False
        self._scratch = threading.local()
        path = Path(path)
        try:
            if sys.platform == 'win32':
                fd, expected = _pin_windows(path, self._pins)
                load_path = str(path)
            else:
                # Stage 1 POSIX builds expose ABI/capabilities only. Bind dyld/
                # dlopen to our retained descriptor, never reopen the input name.
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                self._pins.callback(os.close, fd)
                load_path = f'/dev/fd/{fd}' if sys.platform == 'darwin' else f'/proc/self/fd/{fd}'
            digest, size = _hash(fd)
            library = C.CDLL(load_path, **({'winmode': 0x100 | 0x800} if sys.platform == 'win32' else {}))
            self._library = library
            if sys.platform == 'win32':
                # Validate the actual loaded module, including loader reuse.
                from backend.core.sandbox_read_windows import WindowsReadApi
                kernel = C.WinDLL('kernel32', use_last_error=True)
                name = kernel.GetModuleFileNameW
                name.argtypes, name.restype = [C.c_void_p, C.c_wchar_p, C.c_uint32], C.c_uint32
                buffer = C.create_unicode_buffer(32768)
                length = name(library._handle, buffer, len(buffer))
                if not 0 < length < len(buffer):
                    raise NativeBoundaryError("Loaded native module path is unavailable")
                loaded_fd, observed = _pin_windows(Path(buffer.value), self._pins)
                if observed != expected or _hash(loaded_fd) != (digest, size):
                    raise NativeBoundaryError("Loaded module differs from the pinned artifact")
            version = library.gitgo_native_abi
            version.argtypes, version.restype = [], C.c_uint32
            if version() != ABI:
                raise NativeBoundaryError("Native module ABI mismatch")
            self._call = library.gitgo_native_call
            self._call.argtypes = [C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
            self._call.restype = C.c_uint32
            self._acl = library.gitgo_native_acl
            self._acl.argtypes = [C.c_uint32, C.c_uint64, C.c_void_p, C.c_size_t,
                                  C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
            self._acl.restype = C.c_uint32
            self.identity = {'name': 'gitgo-native-sandbox', 'abi_version': ABI,
                             'path': str(path), 'sha256': digest, 'size': size}
            capabilities = self.call(op='capabilities')
            if capabilities.get('abi') != ABI or capabilities.get('platform') != {
                    'win32': 'windows', 'darwin': 'macos', 'linux': 'linux'}[sys.platform]:
                raise NativeBoundaryError("Native module platform contract mismatch")
        except BaseException:
            # CDLL may already be loaded. Do not release its pins if any loaded
            # code escaped verification; keep the failed instance alive below.
            if not hasattr(self, '_library'):
                self._pins.close()
            else:
                _failed_loads.append(self)
            raise

    def acl(self, operation, token=0, raw=b''):
        if self._sealed:
            raise NativeBoundaryError('Native boundary is sealed; retain recovery debt')
        if len(raw) > MAX_REQUEST:
            raise ValueError('Native security descriptor exceeds its bound')
        if operation not in {1, 2, 3, 4, 5}:
            raise ValueError('Unknown native ACL operation')
        capacity = (min(8 * MAX_REQUEST, max(4096, len(raw) * 16)) if operation == 3
                    else MAX_REQUEST if operation in {1, 4} else 0)
        # A whole runtime tree needs many small descriptor reads. Reuse bounded
        # scratch space per OS caller thread, while reserving a separate slot
        # for reentrant calls (including Python signal handlers after FFI).
        slots = getattr(self._scratch, 'acl_buffers', None)
        if slots is None:
            slots = self._scratch.acl_buffers = []
        slot = next((item for item in slots if not item[1]), None) if capacity else None
        if capacity:
            if slot is None:
                slot = [C.create_string_buffer(capacity), False]
                if len(slots) < 2:
                    slots.append(slot)
            elif len(slot[0]) < capacity:
                slot[0] = C.create_string_buffer(capacity)
            slot[1] = True
        output, written = slot[0] if slot is not None else None, C.c_size_t()
        try:
            status = self._acl(operation, token, raw or None, len(raw), output, capacity, C.byref(written))
            if status:
                if status == 5023:
                    self._sealed = True
                raise NativeBoundaryError(status, 'Native ACL operation failed; preserve recovery records')
            if written.value > capacity:
                self._sealed = True
                raise NativeBoundaryError('Native ACL response exceeds bounds')
            return C.string_at(output, written.value) if written.value else b''
        finally:
            if slot is not None:
                slot[1] = False

    def call(self, **request):
        if self._sealed:
            raise NativeBoundaryError("Native boundary is sealed; preserve recovery debt")
        raw = json.dumps(request, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
        if not 0 < len(raw) <= MAX_REQUEST:
            raise ValueError("Native request exceeds its bound")
        # Tree preparation opens/releases many files. Avoid allocating and
        # zeroing 64 KiB for every small token response. Keep the same bounded,
        # thread-local/reentrant ownership as the descriptor transport above.
        slots = getattr(self._scratch, 'call_buffers', None)
        if slots is None:
            slots = self._scratch.call_buffers = []
        slot = next((item for item in slots if not item[1]), None)
        if slot is None:
            slot = [C.create_string_buffer(RESPONSE_CAPACITY), False]
            if len(slots) < 2:
                slots.append(slot)
        slot[1] = True
        output, written = slot[0], C.c_size_t()
        try:
            status = self._call(raw, len(raw), output, len(output), C.byref(written))
            if status != 0 or not 0 < written.value <= len(output):
                self._sealed = True
                raise NativeBoundaryError("Native call outcome is unknown; do not replay the invocation")
            try:
                result = json.loads(C.string_at(output, written.value))
                if type(result) is not dict or type(result.get('ok')) is not bool:
                    raise ValueError('Invalid response')
            except (ValueError, TypeError) as error:
                self._sealed = True
                raise NativeBoundaryError("Native call response is unknown; retain recovery debt") from error
        finally:
            slot[1] = False
        if not result['ok']:
            error = NativeBoundaryError(result.get('winerror', 0), result.get('error', 'native_failure'))
            error.winerror = result.get('winerror', 0)
            error.outcome_unknown = False
            raise error
        return result


def get_native():
    global _loaded
    with _lock:
        if _loaded is None:
            if _failed_loads:
                raise NativeBoundaryError("Native module failed verification after load; restart after repair")
            _loaded = NativeLibrary(installed_path())
        return _loaded


def implementation_identity():
    return dict(get_native().identity)
