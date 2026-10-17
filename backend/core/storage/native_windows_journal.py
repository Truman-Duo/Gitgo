"""Native bounded journal I/O for the single Host registry.

The coordinator supplies an owned parent, exact recorded identities and the
already held Host gate. This primitive does not issue a reservation ticket or
prove a worker exited. Its native writer permits one 4096-byte cell per update
inside an 8192-byte file, compares the full old bytes, flushes and reads back.
"""
from __future__ import annotations

import ctypes
import sys
from typing import Any

from backend.core.sandbox_read import ObjectIdentity
from .journal_cells import CELL_BYTES, SLOT_BYTES, JournalFormat, JournalView
from .models import StorageBlocked
from .native_windows_scope import NativeHandle, _identity_words
from .runtime_records import FORMAT as RUNTIME_FORMAT
from .slot_records import FORMAT as WORKER_FORMAT

_U64 = ctypes.c_uint64
_U32 = ctypes.c_uint32
_MAX_OWNERS = 8
_LIVE_JOURNALS: dict[int, WindowsStorageJournal] = {}


class StorageJournalError(StorageBlocked):
    code = "STORAGE_NATIVE_JOURNAL"

    def __init__(self, operation: str, native_error: int):
        self.operation = operation
        self.native_error = native_error
        super().__init__(f"{self.code}: {operation} ({native_error})")


def _check(operation: str, result: int) -> None:
    if result:
        raise StorageJournalError(operation, result)


class WindowsJournalLibrary:
    """Bind an already verified and loaded runtime's ABI 1, without discovery."""

    def __init__(self, loaded_module: Any):
        if sys.platform != "win32":
            raise StorageJournalError("unsupported_platform", 50)
        self._module = loaded_module
        self._owners: dict[int, WindowsStorageJournal] = {}
        signatures = {
            "abi": [], "reserve": [ctypes.POINTER(_U64)],
            "open": [_U64, ctypes.c_void_p, _U32, ctypes.POINTER(_U64), _U32, ctypes.c_int],
            "initialize": [_U64], "read": [_U64, ctypes.c_void_p, _U32],
            "update": [_U64, ctypes.c_void_p, _U32, _U32, ctypes.c_void_p, _U32],
            "retire": [_U64],
        }
        self._calls = {}
        for name, arguments in signatures.items():
            call = getattr(loaded_module, "gitgo_journal_" + name)
            call.argtypes, call.restype = arguments, _U32
            self._calls[name] = call
        if self._calls["abi"]() != 1:
            raise StorageJournalError("unsupported_abi", 50)

    @property
    def retained_journals(self) -> int:
        return len(self._owners)

    def reserve(self, record_format: JournalFormat) -> WindowsStorageJournal:
        return WindowsStorageJournal(self, record_format)


class WindowsStorageJournal:
    def __init__(self, library: WindowsJournalLibrary, record_format: JournalFormat):
        if (type(library) is not WindowsJournalLibrary or library.retained_journals >= _MAX_OWNERS
                or len(_LIVE_JOURNALS) >= _MAX_OWNERS):
            raise StorageJournalError("journal_capacity", 1816)
        if record_format is not WORKER_FORMAT and record_format is not RUNTIME_FORMAT:
            raise ValueError("An explicitly supported Host journal contract is required")
        self._library = library
        self._format = record_format
        self._cookie = _U64()
        self._closed = False
        self._sealed = False
        library._owners[id(self)] = self
        _LIVE_JOURNALS[id(self)] = self
        try:
            _check("reserve", self._call("reserve", ctypes.byref(self._cookie)))
        except BaseException:
            if not self._cookie.value:
                library._owners.pop(id(self), None)
                _LIVE_JOURNALS.pop(id(self), None)
            raise

    def _call(self, name, *arguments):
        return self._library._calls[name](*arguments)

    def _require(self):
        if self._sealed or self._closed:
            raise StorageJournalError("journal_sealed", 5023)

    def open(self, parent: NativeHandle, index: int, parent_identity: ObjectIdentity,
             file_identity: ObjectIdentity, *, initialize: bool = False) -> None:
        self._require()
        if type(parent) is not NativeHandle:
            raise ValueError("An owned native parent is required")
        if type(index) is not int or not 0 <= index < 64 or type(initialize) is not bool:
            raise ValueError("A bounded journal index and explicit initialization mode are required")
        if (index == 63) != (self._format is RUNTIME_FORMAT):
            raise ValueError("Runtime and worker journals have distinct prepaid slots")
        words = (_U64 * 6)(*_identity_words(parent_identity), *_identity_words(file_identity))
        borrowed = parent._as_parameter_
        self._sealed = True
        _check("open", self._call("open", self._cookie, borrowed, index, words, 6, initialize))
        if initialize:
            _check("initialize", self._call("initialize", self._cookie))
        self._sealed = False

    def read(self) -> JournalView:
        self._require()
        output = ctypes.create_string_buffer(SLOT_BYTES)
        self._sealed = True
        _check("read", self._call("read", self._cookie, output, SLOT_BYTES))
        view = self._format.read(output.raw)
        self._sealed = False
        return view

    def update(self, view: JournalView, state) -> JournalView:
        self._require()
        update = self._format.next_update(view, state)
        prior = ctypes.create_string_buffer(view.raw, SLOT_BYTES)
        data = ctypes.create_string_buffer(update.encoded, CELL_BYTES)
        output = ctypes.create_string_buffer(SLOT_BYTES)
        self._sealed = True
        _check("update", self._call("update", self._cookie, prior, SLOT_BYTES,
                                   update.target, data, CELL_BYTES))
        _check("readback", self._call("read", self._cookie, output, SLOT_BYTES))
        result = self._format.read(output.raw)
        update.require_result(result)
        self._sealed = False
        return result

    def close(self) -> None:
        if not self._closed:
            self._sealed = True
            _check("retire", self._call("retire", self._cookie))
            self._closed = True
        self._library._owners.pop(id(self), None)
        _LIVE_JOURNALS.pop(id(self), None)
