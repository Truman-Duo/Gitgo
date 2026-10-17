"""Native Windows resource ownership, independent of storage admission.

The qualified runtime supplies an already loaded, trusted native module. This
module never searches for a DLL, creates a registry, grants access, or activates
a storage format. A handle or an acquired lock is not a reservation ticket.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import PureWindowsPath
import sys
from typing import Any

from backend.core.sandbox_read import ObjectIdentity, relative_parts
from backend.core.storage.models import StorageBlocked

_U64 = ctypes.c_uint64
_U32 = ctypes.c_uint32
_ABSENT = 0xE0010001
_MAX_OWNERS = 16
_LIVE_SCOPES: dict[int, WindowsStorageScope] = {}


class StorageOwnershipError(StorageBlocked):
    """The owner must stay retained until native retirement is confirmed."""

    code = "STORAGE_NATIVE_OWNERSHIP"

    def __init__(self, operation: str, native_error: int):
        self.operation = operation
        self.native_error = native_error
        super().__init__(f"{self.code}: {operation} ({native_error})")


def _check(operation: str, result: int) -> None:
    if result:
        raise StorageOwnershipError(operation, result)


def _identity_words(identity: ObjectIdentity):
    if (type(identity) is not ObjectIdentity or identity.platform != "win32"
            or type(identity.volume) is not int or not 0 <= identity.volume < 2**64
            or type(identity.file_id) is not int or not 0 <= identity.file_id < 2**128):
        raise ValueError("A bounded native Windows object identity is required")
    return (_U64 * 3)(identity.volume, identity.file_id & (2**64 - 1), identity.file_id >> 64)


def _leaf(name: str) -> str:
    if type(name) is not str or len(name) > 255 or relative_parts(name) != (name,):
        raise ValueError("An exact relative native member name is required")
    return name


@dataclass(frozen=True)
class ScopeSnapshot:
    sealed: bool
    handles: int
    unknown_closes: int
    lock_attempts: int


@dataclass(frozen=True)
class NativeHandle:
    """Process-local generation token; raw handle values are borrowed only."""

    owner: WindowsStorageScope
    token: int

    @property
    def _as_parameter_(self):
        return ctypes.c_void_p(self.owner.borrow(self))


class WindowsScopeLibrary:
    """Bind ABI 1 of the native module retained by the trusted runtime owner.

    The caller must keep that runtime's code and namespace pins through every
    scope lifetime. Passing a loaded module does not constitute installation
    approval. Default StorageRuntime does not construct this component.
    """

    def __init__(self, loaded_module: Any):
        if sys.platform != "win32":
            raise StorageOwnershipError("unsupported_platform", 50)
        self._module = loaded_module
        self._owners: dict[int, WindowsStorageScope] = {}
        signatures = {
            "volume": [_U64, _U64, ctypes.POINTER(_U64), _U32],
            "abi": [],
            "reserve": [ctypes.POINTER(_U64)],
            "open": [_U64, _U64, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(_U64)],
            "import_parent": [_U64, ctypes.c_void_p, ctypes.POINTER(_U64), ctypes.POINTER(_U64)],
            "borrow": [_U64, _U64, ctypes.POINTER(ctypes.c_size_t)],
            "identity": [_U64, _U64, ctypes.POINTER(_U64), _U32],
            "lock": [_U64, _U64, ctypes.POINTER(_U64), ctypes.c_int],
            "read": [_U64, _U64, ctypes.c_void_p, _U32, ctypes.POINTER(_U32)],
            "close": [_U64, _U64],
            "status": [_U64, ctypes.POINTER(_U64), _U32],
            "retire": [_U64],
            "account": [_U64, ctypes.c_wchar_p, _U32, ctypes.c_wchar_p, _U32],
            "flush_bound": [_U64, _U64, ctypes.c_wchar_p, ctypes.POINTER(_U64), _U64],
        }
        self._calls = {}
        for name, arguments in signatures.items():
            call = getattr(loaded_module, "gitgo_scope_" + name)
            call.argtypes, call.restype = arguments, _U32
            self._calls[name] = call
        if self._calls["abi"]() != 1:
            raise StorageOwnershipError("unsupported_abi", 50)

    @property
    def retained_scopes(self) -> int:
        return len(self._owners)

    def open_scope(self) -> WindowsStorageScope:
        return WindowsStorageScope(self)


class WindowsStorageScope:
    """Own every attempted native acquisition, including an unreturned handle.

    Native code binds each live scope to its creating process and thread. A
    failed operation seals further work. Retirement is the only operation that
    may release that debt; an unknown OS close is never repeated.
    """

    def __init__(self, library: WindowsScopeLibrary):
        if (type(library) is not WindowsScopeLibrary or library.retained_scopes >= _MAX_OWNERS
                or len(_LIVE_SCOPES) >= _MAX_OWNERS):
            raise StorageOwnershipError("scope_capacity", 1816)
        self._library = library
        self._cookie = _U64()
        self._sealed = False
        self._closed = False
        library._owners[id(self)] = self
        _LIVE_SCOPES[id(self)] = self
        try:
            _check("reserve", self._call("reserve", ctypes.byref(self._cookie)))
        except BaseException:
            if not self._cookie.value:
                library._owners.pop(id(self), None)
                _LIVE_SCOPES.pop(id(self), None)
            raise

    def _call(self, name: str, *arguments):
        return self._library._calls[name](*arguments)

    @property
    def closed(self) -> bool:
        return self._closed

    def _require(self, handle: NativeHandle | None = None) -> None:
        if self._sealed or self._closed:
            raise StorageOwnershipError("scope_sealed", 5023)
        if handle is not None and (type(handle) is not NativeHandle or handle.owner is not self):
            raise StorageOwnershipError("foreign_handle", 6)

    def _open(self, name: str, parent: NativeHandle | None, directory: bool, readable: bool) -> NativeHandle:
        self._require(parent)
        if type(directory) is not bool or type(readable) is not bool:
            raise ValueError("Native open flags must be booleans")
        token = _U64()
        self._sealed = True
        result = self._call("open", self._cookie, 0 if parent is None else parent.token,
                            name, directory, readable, ctypes.byref(token))
        if result == _ABSENT:
            self._sealed = False
            raise FileNotFoundError("Exact final native member is absent")
        _check("open", result)
        handle = NativeHandle(self, token.value)
        self._sealed = False
        return handle

    def root(self, path: str, *, exclude: ObjectIdentity | None = None) -> NativeHandle:
        if type(path) is not str or "\0" in path:
            raise ValueError("A bounded local Windows path is required")
        if exclude is not None:
            _identity_words(exclude)
        def check(handle):
            if exclude is not None and self.identity(handle)[0] == exclude:
                self._sealed = True
                raise StorageOwnershipError("excluded_ancestor",5)
            return handle
        root = PureWindowsPath(path)
        if (not root.is_absolute() or len(root.drive) != 2 or root.drive[1] != ":"
                or len(root.parts) > 65):
            raise ValueError("A bounded local Windows path is required")
        # Validate the complete input before the first open, including names
        # that Win32 could interpret as devices or truncate at a terminator.
        for part in root.parts[1:]:
            _leaf(part)
        parent = self._open("\\??\\" + root.anchor, None, True, True)
        check(parent)
        for part in root.parts[1:]:
            parent = check(self._open(part, parent, True, True))
        return parent

    def open(self, name: str, parent: NativeHandle, *, directory: bool = False,
             readable: bool = True) -> NativeHandle:
        return self._open(_leaf(name), parent, directory, readable)

    def import_parent(self, parent: NativeHandle, expected: ObjectIdentity) -> NativeHandle:
        self._require()
        if type(parent) is not NativeHandle:
            raise StorageOwnershipError("unowned_parent", 6)
        words, token = _identity_words(expected), _U64()
        borrowed = parent._as_parameter_
        self._sealed = True
        _check("import_parent", self._call("import_parent", self._cookie, borrowed, words, ctypes.byref(token)))
        handle = NativeHandle(self, token.value)
        self._sealed = False
        return handle

    def borrow(self, handle: NativeHandle) -> int:
        self._require(handle)
        raw = ctypes.c_size_t()
        self._sealed = True
        _check("borrow", self._call("borrow", self._cookie, handle.token, ctypes.byref(raw)))
        value = raw.value
        self._sealed = False
        return value

    def identity(self, handle: NativeHandle) -> tuple[ObjectIdentity, bool]:
        self._require(handle)
        words = (_U64 * 4)()
        self._sealed = True
        _check("identity", self._call("identity", self._cookie, handle.token, words, 4))
        identity = ObjectIdentity("win32", words[0], words[1] | (words[2] << 64))
        self._sealed = False
        return identity, bool(words[3])

    def lock(self, handle: NativeHandle, expected: ObjectIdentity, *, exclusive: bool = True) -> None:
        self._require(handle)
        if type(exclusive) is not bool:
            raise ValueError("Native lock mode must be a boolean")
        words = _identity_words(expected)
        self._sealed = True
        _check("lock", self._call("lock", self._cookie, handle.token, words, exclusive))
        self._sealed = False

    def read(self, handle: NativeHandle, maximum: int = 65536) -> bytes:
        self._require(handle)
        if type(maximum) is not int or not 0 < maximum <= 65536:
            raise ValueError("A bounded native metadata read is required")
        data, count = ctypes.create_string_buffer(maximum), _U32()
        self._sealed = True
        _check("read", self._call("read", self._cookie, handle.token, data, maximum, ctypes.byref(count)))
        result = data.raw[:count.value]
        self._sealed = False
        return result

    def flush_bound(self, parent: NativeHandle, name: str, expected: ObjectIdentity, maximum: int = 65536) -> None:
        self._require(parent)
        if type(maximum) is not int or not 0 <= maximum <= 65536:
            raise ValueError("A bounded native metadata flush is required")
        name, words = _leaf(name), _identity_words(expected)
        self._sealed = True
        _check("flush_bound", self._call("flush_bound", self._cookie, parent.token, name, words, maximum))
        self._sealed = False

    def volume(self, handle: NativeHandle) -> tuple[int, int]:
        """Qualify the actual local NTFS object and its 4096-byte allocation unit."""
        self._require(handle)
        words = (_U64 * 2)()
        self._sealed = True
        _check("volume", self._call("volume", self._cookie, handle.token, words, 2))
        result = words[0], words[1]
        self._sealed = False
        return result

    def account(self) -> tuple[str, str]:
        self._require()
        sid, profile = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(513)
        self._sealed = True
        _check("account", self._call("account", self._cookie, sid, len(sid), profile, len(profile)))
        result = sid.value, profile.value
        self._sealed = False
        return result

    def snapshot(self) -> ScopeSnapshot:
        words = (_U64 * 4)()
        _check("status", self._call("status", self._cookie, words, 4))
        return ScopeSnapshot(bool(words[0]), words[1], words[2], words[3])

    def close_handle(self, handle: NativeHandle) -> None:
        if type(handle) is not NativeHandle or handle.owner is not self or self._closed:
            raise StorageOwnershipError("foreign_or_expired_handle", 6)
        previous = self._sealed
        self._sealed = True
        _check("close", self._call("close", self._cookie, handle.token))
        self._sealed = previous

    def close(self) -> None:
        if not self._closed:
            self._sealed = True
            _check("retire", self._call("retire", self._cookie))
            self._closed = True
        self._library._owners.pop(id(self), None)
        _LIVE_SCOPES.pop(id(self), None)
