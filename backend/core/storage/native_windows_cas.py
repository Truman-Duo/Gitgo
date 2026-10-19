"""Exact native CAS objects under an enclosing admitted publication lifetime.

These primitives do not issue file tickets, prove an unpublished batch, or
refund capacity. The coordinator must persist intent and creation identities,
hold the protected namespace and selected runtime, and settle the native worker
before committing independent cleanup evidence. Unknown owners stay retained.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import IntEnum
import hashlib
import os
import re
import sys
import threading
import uuid

from backend.core.sandbox_read import ObjectIdentity
from .models import StorageBlocked
from .native_windows_scope import NativeHandle, _identity_words

_U64, _U32 = ctypes.c_uint64, ctypes.c_uint32
_WRITERS: dict[int, WindowsCasWriter] = {}
_GENERATIONS: dict[int, WindowsCasGeneration] = {}


class StorageCasError(StorageBlocked):
    code = "STORAGE_NATIVE_CAS"

    def __init__(self, operation: str, native_error: int):
        self.operation, self.native_error = operation, native_error
        super().__init__(f"{self.code}: {operation} ({native_error})")


def _check(operation, result):
    if result:
        raise StorageCasError(operation, result)


@dataclass(frozen=True)
class CasFileIntent:
    """Closed filename and bytes bound; an immutable value is not authority."""
    digest: str
    generation: str
    length: int

    def __post_init__(self):
        if type(self.digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ValueError("An exact SHA-256 digest is required")
        if type(self.generation) is not str or str(uuid.UUID(self.generation)) != self.generation:
            raise ValueError("A canonical CAS generation is required")
        if type(self.length) is not int or not 0 <= self.length <= 65536:
            raise ValueError("A bounded immutable file length is required")

    @property
    def name(self):
        return self.digest + "." + self.generation + ".blob"

    def require_bytes(self, data: bytes):
        if (type(data) is not bytes or len(data) != self.length
                or hashlib.sha256(data).hexdigest() != self.digest):
            raise ValueError("Exact immutable planned CAS bytes are required")


@dataclass(frozen=True)
class CasFileRecord:
    intent: CasFileIntent
    identity: ObjectIdentity

    def __post_init__(self):
        if type(self.intent) is not CasFileIntent:
            raise ValueError("An exact CAS intent is required")
        _identity_words(self.identity)

    def _words(self):
        return (_U64 * 4)(*_identity_words(self.identity), self.intent.length)


class CasPurpose(IntEnum):
    READ = 0
    RETIRE_COMMITTED = 1
    RETIRE_UNPUBLISHED = 2


class _Library:
    def _bind(self, module, prefix, signatures):
        if sys.platform != "win32":
            raise StorageCasError("unsupported_platform", 50)
        self._module, self._owners, self._calls = module, {}, {}
        for name, arguments in signatures.items():
            call = getattr(module, prefix + name)
            call.argtypes, call.restype = arguments, _U32
            self._calls[name] = call
        if self._calls["abi"]() != 1:
            raise StorageCasError("unsupported_abi", 50)

    @property
    def retained_owners(self):
        return len(self._owners)


class WindowsCasWriterLibrary(_Library):
    def __init__(self, loaded_module):
        self._bind(loaded_module, "gitgo_cas_writer_", {
            "abi": [], "reserve": [ctypes.POINTER(_U64)],
            "create": [_U64, ctypes.c_void_p, ctypes.POINTER(_U64), _U32, ctypes.c_wchar_p, _U64],
            "write": [_U64, ctypes.c_void_p, _U32], "seal": [_U64],
            "created": [_U64, ctypes.POINTER(_U64), _U32],
            "identity": [_U64, ctypes.POINTER(_U64), _U32],
            "read": [_U64, _U64, ctypes.c_void_p, _U32], "retire": [_U64],
        })

    def reserve(self):
        return WindowsCasWriter(self)


class WindowsCasGenerationLibrary(_Library):
    def __init__(self, loaded_module):
        self._bind(loaded_module, "gitgo_cas_generation_", {
            "abi": [], "reserve": [ctypes.POINTER(_U64), ctypes.c_int],
            "open": [_U64, ctypes.c_void_p, ctypes.POINTER(_U64), _U32, ctypes.c_wchar_p,
                     ctypes.POINTER(_U64), _U32],
            "identity": [_U64, ctypes.POINTER(_U64), _U32],
            "read": [_U64, _U64, ctypes.c_void_p, _U32],
            "delete": [_U64, ctypes.POINTER(_U64), _U32],
            "absent": [_U64, ctypes.POINTER(ctypes.c_int)], "retire": [_U64],
        })

    def reserve(self, purpose: CasPurpose):
        return WindowsCasGeneration(self, purpose)


class _Owner:
    def _start_owner(self, library, registry, limit, *reservation_args):
        if library.retained_owners >= limit or len(registry) >= limit:
            raise StorageCasError("owner_capacity", 1816)
        self._library, self._registry = library, registry
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._cookie = _U64()
        self._sealed = self._closed = self._attempted = False
        library._owners[id(self)] = registry[id(self)] = self
        try:
            _check("reserve", self._call("reserve", ctypes.byref(self._cookie), *reservation_args))
        except BaseException:
            self._sealed = True
            if not self._cookie.value:
                library._owners.pop(id(self), None)
                registry.pop(id(self), None)
            raise

    def _call(self, name, *args):
        return self._library._calls[name](*args)

    def _context(self):
        if (os.getpid(), threading.get_ident()) != (self._pid, self._thread):
            raise StorageCasError("foreign_context", 6)

    def _require(self):
        self._context()
        if self._closed or self._sealed:
            raise StorageCasError("owner_sealed", 5023)

    def close(self):
        self._context()
        if not self._closed:
            self._sealed = True
            _check("retire", self._call("retire", self._cookie))
            self._closed = True
        self._library._owners.pop(id(self), None)
        self._registry.pop(id(self), None)


class WindowsCasWriter(_Owner):
    def __init__(self, library: WindowsCasWriterLibrary):
        if type(library) is not WindowsCasWriterLibrary:
            raise ValueError("An explicit pinned native CAS writer module is required")
        self._record = None
        self._verified = False
        self._start_owner(library, _WRITERS, 8)

    def create(self, parent: NativeHandle, parent_identity: ObjectIdentity,
               intent: CasFileIntent) -> CasFileRecord:
        self._require()
        if self._attempted or type(parent) is not NativeHandle or type(intent) is not CasFileIntent:
            raise ValueError("One exact native-parent creation attempt is required")
        parent_words, borrowed = _identity_words(parent_identity), parent._as_parameter_
        self._attempted = self._sealed = True
        _check("create", self._call("create", self._cookie, borrowed, parent_words, 3, intent.name, intent.length))
        observed = (_U64 * 4)()
        _check("creation_identity", self._call("created", self._cookie, observed, 4))
        if observed[3] != 0:
            raise StorageCasError("new_file_not_empty", 13)
        identity = ObjectIdentity("win32", observed[0], observed[1] | (observed[2] << 64))
        self._record = CasFileRecord(intent, identity)
        self._sealed = False
        return self._record

    def materialize(self, data: bytes) -> CasFileRecord:
        """Caller must first durably record the returned creation identity."""
        self._require()
        if self._record is None or self._verified:
            raise StorageCasError("creation_required", 5023)
        self._record.intent.require_bytes(data)
        self._sealed = True
        for offset in range(0, len(data), 4096):
            part = data[offset:offset+4096]
            _check("write", self._call("write", self._cookie, ctypes.create_string_buffer(part, len(part)), len(part)))
        _check("flush", self._call("seal", self._cookie))
        observed = (_U64 * 4)()
        _check("sealed_identity", self._call("identity", self._cookie, observed, 4))
        if tuple(observed) != tuple(self._record._words()):
            raise StorageCasError("sealed_identity_changed", 13)
        digest = hashlib.sha256()
        for offset in range(0, len(data), 4096):
            count = min(4096, len(data)-offset)
            buffer = ctypes.create_string_buffer(count)
            _check("readback", self._call("read", self._cookie, offset, buffer, count))
            digest.update(buffer.raw)
        if digest.hexdigest() != self._record.intent.digest:
            raise StorageCasError("readback_digest_changed", 13)
        self._verified = True
        self._sealed = False
        return self._record


class WindowsCasGeneration(_Owner):
    def __init__(self, library: WindowsCasGenerationLibrary, purpose: CasPurpose):
        if type(library) is not WindowsCasGenerationLibrary or type(purpose) is not CasPurpose:
            raise ValueError("A pinned native module and immutable CAS purpose are required")
        self._purpose = purpose
        self._record = self._observed = None
        self._absent = self._verified = self._delete_entered = False
        self._start_owner(library, _GENERATIONS, 32, int(purpose))

    def open(self, parent: NativeHandle, parent_identity: ObjectIdentity, record: CasFileRecord) -> bool:
        self._require()
        if self._attempted or type(parent) is not NativeHandle or type(record) is not CasFileRecord:
            raise ValueError("One exact native-parent and recorded-object open is required")
        parent_words, expected, borrowed = _identity_words(parent_identity), record._words(), parent._as_parameter_
        self._attempted = self._sealed = True
        self._record = record
        _check("open", self._call("open", self._cookie, borrowed, parent_words, 3, record.intent.name, expected, 4))
        observed = (_U64 * 5)()
        _check("opened_identity", self._call("identity", self._cookie, observed, 5))
        if observed[4] not in (0, 1):
            raise StorageCasError("absence_result_invalid", 13)
        self._absent = bool(observed[4])
        if not self._absent:
            if (tuple(observed[:3]) != tuple(expected[:3]) or observed[3] > record.intent.length
                    or self._purpose is not CasPurpose.RETIRE_UNPUBLISHED and observed[3] != record.intent.length):
                raise StorageCasError("opened_identity_changed", 13)
            self._observed = (_U64 * 4)(*observed[:4])
        self._sealed = False
        return not self._absent

    def read(self, *, planned_bytes: bytes | None = None) -> bytes:
        self._require()
        if self._record is None or self._absent or self._delete_entered:
            raise StorageCasError("file_unavailable", 2)
        if self._purpose is CasPurpose.RETIRE_UNPUBLISHED:
            # A short file is accepted only against the complete retained input,
            # not a caller-supplied arbitrary prefix or a hash of partial bytes.
            self._record.intent.require_bytes(planned_bytes)
        elif planned_bytes is not None:
            raise ValueError("Only unpublished retirement consumes planned bytes")
        self._sealed = True
        data = bytearray()
        for offset in range(0, self._observed[3], 4096):
            count = min(4096, self._observed[3]-offset)
            buffer = ctypes.create_string_buffer(count)
            _check("read", self._call("read", self._cookie, offset, buffer, count))
            data.extend(buffer.raw)
        actual = bytes(data)
        matches = (actual == planned_bytes[:len(actual)] if self._purpose is CasPurpose.RETIRE_UNPUBLISHED
                   else hashlib.sha256(actual).hexdigest() == self._record.intent.digest)
        if not matches:
            raise StorageCasError("content_mismatch", 13)
        self._verified, self._sealed = True, False
        return actual

    def delete(self) -> None:
        """Caller owns durable exact-generation deletion intent and exclusivity."""
        self._require()
        if (self._purpose is CasPurpose.READ or not self._verified or self._absent
                or self._delete_entered or self._observed is None):
            raise StorageCasError("verified_deletion_required", 5023)
        self._delete_entered = self._sealed = True
        _check("delete", self._call("delete", self._cookie, self._observed, 4))
        self._sealed = False

    def observe_absence(self) -> bool:
        self._require()
        if not self._attempted:
            raise StorageCasError("opened_parent_required", 5023)
        self._sealed = True
        output = ctypes.c_int()
        _check("observe_absence", self._call("absent", self._cookie, ctypes.byref(output)))
        if output.value not in (0, 1):
            raise StorageCasError("absence_result_invalid", 13)
        self._sealed = False
        return bool(output.value)
