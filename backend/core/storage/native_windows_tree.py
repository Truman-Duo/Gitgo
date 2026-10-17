"""One bounded native owner pool for storage metadata and installed runtimes.

An immutable native purpose separates pinning, installation, bootstrap,
absence observation and exact-member retirement. None issues Host admission,
installation approval, a process grant or a storage-capacity refund.
"""
from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
from enum import IntEnum
import hashlib
from pathlib import PureWindowsPath
import re
import struct
import sys
from typing import Any
import uuid

from backend.core.sandbox_read import ObjectIdentity
from .models import StorageBlocked
from .native_windows_scope import NativeHandle, _identity_words, _leaf

_U64, _U32 = C.c_uint64, C.c_uint32
_MAX_OWNERS = 4
_LIVE_TREES: dict[int, _TreeOwner] = {}


class TreePurpose(IntEnum):
    PIN = 0
    INSTALL = 1
    ABSENCE = 3
    BOOTSTRAP = 4
    RETIRE = 6


class StorageTreeError(StorageBlocked):
    code = "STORAGE_NATIVE_TREE"

    def __init__(self, operation: str, native_error: int):
        self.operation, self.native_error = operation, native_error
        super().__init__(f"{self.code}: {operation} ({native_error})")


def _check(operation, result):
    if result:
        raise StorageTreeError(operation, result)


def _number(value, maximum, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("A bounded exact native integer is required")


def _name(value):
    _leaf(value)
    if len(value) > 128 or re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None:
        raise ValueError("A canonical installed-tree member name is required")
    return value


def _generation(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError("A canonical generation UUID is required")
    return value


def _sid_bytes(value):
    if type(value) is not str or re.fullmatch(r"S-1-5(?:-(?:0|[1-9][0-9]{0,9})){1,15}", value) is None:
        raise ValueError("A canonical NT-authority Host SID is required")
    numbers = tuple(int(part) for part in value.split("-")[3:])
    for number in numbers:
        _number(number, 2**32-1)
    return bytes((1, len(numbers))) + (5).to_bytes(6, "big") + b"".join(struct.pack("<I", n) for n in numbers)


def _parent(handle, expected):
    if type(handle) is not NativeHandle:
        raise ValueError("An owned native parent is required")
    words = _identity_words(expected)
    return handle._as_parameter_, words


@dataclass(frozen=True)
class TreeNode:
    directory: bool
    ceiling: int
    length: int | None


class WindowsTreeLibrary:
    """Bind a verified loaded module; all tree purposes share four native slots."""

    def __init__(self, loaded_module: Any):
        if sys.platform != "win32":
            raise StorageTreeError("unsupported_platform", 50)
        self._module = loaded_module
        self._owners: dict[int, _TreeOwner] = {}
        words = C.POINTER(_U64)
        signatures = {
            "tree_abi": [], "tree_reserve": [words, C.c_void_p, _U32, _U32],
            "tree_root": [_U64, C.c_void_p, words, _U32],
            "tree_path": [_U64, C.c_wchar_p, words, _U32],
            "tree_add": [_U64, _U32, C.c_wchar_p, C.c_int, _U64, C.POINTER(_U32)],
            "tree_read": [_U64, _U32, _U64, C.c_void_p, _U32],
            "tree_identity": [_U64, _U32, words, _U32],
            "tree_status": [_U64, words, _U32], "tree_retire": [_U64],
            "install_begin": [_U64, C.c_void_p, words, _U32, C.c_wchar_p],
            "install_add": [_U64, _U32, C.c_wchar_p, C.c_int, _U64, C.POINTER(_U32)],
            "install_write": [_U64, _U32, C.c_void_p, _U32],
            "install_seal_file": [_U64, _U32], "install_finish": [_U64, _U32, _U64],
            "install_absent": [_U64, C.c_void_p, words, _U32, C.c_wchar_p, C.POINTER(C.c_int)],
            "bootstrap_begin": [_U64, C.c_void_p, words, _U32, C.c_wchar_p],
            "bootstrap_attach": [_U64, C.c_void_p, words, _U32],
            "bootstrap_add": [_U64, _U32, C.c_wchar_p, C.c_int, _U64, C.POINTER(_U32)],
            "bootstrap_write_file": [_U64, _U32, C.c_void_p, _U32],
            "bootstrap_identity": [_U64, _U32, words, _U32],
            "bootstrap_finish": [_U64, _U32, _U64], "bootstrap_lock_gate": [_U64, _U32],
            "retirement_begin": [_U64, C.c_void_p, words, C.c_wchar_p, words, _U32],
            "retirement_add": [_U64, _U32, C.c_wchar_p, C.c_int, _U64, C.POINTER(_U32)],
            "retirement_info": [_U64, _U32, words, _U32],
            "retirement_prepare": [_U64, _U32], "retirement_delete": [_U64, _U32],
            "retirement_finish": [_U64],
        }
        self._calls = {}
        for name, arguments in signatures.items():
            call = getattr(loaded_module, "gitgo_" + name)
            call.argtypes, call.restype = arguments, _U32
            self._calls[name] = call
        if self._calls["tree_abi"]() != 1:
            raise StorageTreeError("unsupported_abi", 50)

    @property
    def retained_trees(self):
        return len(self._owners)

    def reserve(self, purpose: TreePurpose, host_sid: str):
        if type(purpose) is not TreePurpose:
            raise ValueError("An explicit native tree purpose is required")
        owner_type = {TreePurpose.PIN: RuntimePins, TreePurpose.INSTALL: RuntimeInstaller,
            TreePurpose.ABSENCE: RuntimeAbsence, TreePurpose.BOOTSTRAP: MetadataBootstrap,
            TreePurpose.RETIRE: RuntimeRetirement}[purpose]
        return owner_type(self, purpose, host_sid)


class _TreeOwner:
    def __init__(self, library, purpose, host_sid):
        if type(purpose) is not TreePurpose:
            raise ValueError("An explicit native tree purpose is required")
        if (type(library) is not WindowsTreeLibrary or library.retained_trees >= _MAX_OWNERS
                or len(_LIVE_TREES) >= _MAX_OWNERS):
            raise StorageTreeError("tree_capacity", 1816)
        raw = _sid_bytes(host_sid)
        buffer = C.create_string_buffer(raw, len(raw))
        self._library, self._purpose = library, purpose
        self._cookie = _U64()
        self._sealed = self._closed = self._finished = False
        self._nodes: dict[int, TreeNode] = {}
        self._charge = 0
        library._owners[id(self)] = self
        _LIVE_TREES[id(self)] = self
        try:
            _check("reserve", self._call("tree_reserve", C.byref(self._cookie), buffer, len(raw), purpose))
        except BaseException:
            if not self._cookie.value:
                library._owners.pop(id(self), None)
                _LIVE_TREES.pop(id(self), None)
            raise

    def _call(self, name, *arguments):
        return self._library._calls[name](*arguments)

    def _require(self, *, mutable=False):
        if self._closed or self._sealed or mutable and self._finished:
            raise StorageTreeError("tree_sealed", 5023)

    def _index(self, index):
        _number(index, 1023)
        if index not in self._nodes:
            raise ValueError("A returned node in this native tree is required")

    def _add(self, call, parent, name, directory, ceiling, maximum):
        self._require(mutable=True)
        self._index(parent)
        name = _name(name)
        _number(ceiling, maximum)
        if type(directory) is not bool or directory and ceiling:
            raise ValueError("Explicit file length or directory required")
        index = _U32(2**32-1)
        self._sealed = True
        _check(call, self._call(call, self._cookie, parent, name, directory, ceiling, C.byref(index)))
        self._nodes[index.value] = TreeNode(directory, ceiling, ceiling if call == "tree_add" else None)
        if not directory:
            self._charge += 1 << max(12, (max(1, ceiling)-1).bit_length())
        self._sealed = False
        return index.value

    def identity(self, index):
        self._require()
        self._index(index)
        values = (_U64*4)()
        call = "bootstrap_identity" if self._purpose is TreePurpose.BOOTSTRAP else "tree_identity"
        self._sealed = True
        _check("identity", self._call(call, self._cookie, index, values, 4))
        result = ObjectIdentity("win32", values[0], values[1] | values[2] << 64)
        self._sealed = False
        return result

    def read(self, index, offset, size):
        self._require()
        self._index(index)
        _number(offset, 64*1024*1024)
        _number(size, 65536, 1)
        node = self._nodes[index]
        if node.directory or node.length is None or offset+size > node.length:
            raise ValueError("A bounded completed tree file read is required")
        buffer = C.create_string_buffer(size)
        self._sealed = True
        _check("read", self._call("tree_read", self._cookie, index, offset, buffer, size))
        result = buffer.raw
        self._sealed = False
        return result

    def digest(self, index):
        self._require()
        self._index(index)
        node = self._nodes[index]
        if node.directory or node.length is None:
            raise ValueError("A completed tree file is required")
        digest = hashlib.sha256()
        for offset in range(0, node.length, 65536):
            digest.update(self.read(index, offset, min(65536, node.length-offset)))
        return digest.hexdigest()

    def close(self):
        if not self._closed:
            self._sealed = True
            _check("retire", self._call("tree_retire", self._cookie))
            self._closed = True
        self._library._owners.pop(id(self), None)
        _LIVE_TREES.pop(id(self), None)


class RuntimePins(_TreeOwner):
    def root(self, parent: NativeHandle, expected: ObjectIdentity):
        self._require(mutable=True)
        borrowed, words = _parent(parent, expected)
        self._sealed = True
        _check("pin_root", self._call("tree_root", self._cookie, borrowed, words, 3))
        self._nodes[0] = TreeNode(True, 0, 0)
        self._sealed = False

    def root_path(self, path: str, expected: ObjectIdentity):
        self._require(mutable=True)
        if type(path) is not str or "\0" in path or len(path) > 512:
            raise ValueError("A bounded installed runtime path is required")
        parsed = PureWindowsPath(path)
        if not parsed.is_absolute() or len(parsed.drive) != 2 or parsed.drive[1] != ":" or str(parsed) != path:
            raise ValueError("An exact local installed runtime path is required")
        # Ancestors can contain spaces; reject device/stream/redirection syntax.
        for part in parsed.parts[1:]:
            _leaf(part)
        words = _identity_words(expected)
        self._sealed = True
        _check("pin_path", self._call("tree_path", self._cookie, path, words, 3))
        self._nodes[0] = TreeNode(True, 0, 0)
        self._sealed = False

    def add(self, parent, name, *, directory=False, length=0):
        return self._add("tree_add", parent, name, directory, length, 64*1024*1024)


class MetadataBootstrap(_TreeOwner):
    def begin(self, parent, expected, name):
        self._require(mutable=True)
        name = _name(name)
        borrowed, words = _parent(parent, expected)
        self._sealed = True
        _check("bootstrap_begin", self._call("bootstrap_begin", self._cookie, borrowed, words, 3, name))
        self._nodes[0] = TreeNode(True, 0, 0)
        self._sealed = False

    def attach(self, parent, expected):
        """Attach exact protected parent; every added member still uses CREATE_NEW."""
        self._require(mutable=True)
        borrowed, words = _parent(parent, expected)
        self._sealed = True
        _check("bootstrap_attach", self._call("bootstrap_attach", self._cookie, borrowed, words, 3))
        self._nodes[0] = TreeNode(True, 0, 0)
        self._sealed = False

    def add(self, parent, name, *, directory=False, ceiling=0):
        return self._add("bootstrap_add", parent, name, directory, ceiling, 65536)

    def write(self, index, data):
        self._require(mutable=True)
        self._index(index)
        if type(data) is not bytes or len(data) > 65536:
            raise ValueError("A bounded metadata payload is required")
        node = self._nodes[index]
        buffer = C.create_string_buffer(data, max(1, len(data)))
        self._sealed = True
        _check("metadata_write", self._call("bootstrap_write_file", self._cookie, index, buffer, len(data)))
        self._nodes[index] = TreeNode(node.directory, node.ceiling, len(data))
        # Verify before removing Python's sealed state after the native flush.
        if data:
            output = C.create_string_buffer(len(data))
            _check("metadata_readback", self._call("tree_read", self._cookie, index, 0, output, len(data)))
            if output.raw != data:
                raise StorageTreeError("metadata_readback", 13)
        self._sealed = False

    def lock_gate(self, index):
        self._require(mutable=True)
        self._index(index)
        self._sealed = True
        _check("bootstrap_gate", self._call("bootstrap_lock_gate", self._cookie, index))
        self._sealed = False

    def finish(self):
        self._require(mutable=True)
        self._sealed = True
        _check("bootstrap_finish", self._call("bootstrap_finish", self._cookie, len(self._nodes), self._charge))
        self._finished = True
        self._sealed = False


class RuntimeInstaller(_TreeOwner):
    def begin(self, parent, expected, generation):
        self._require(mutable=True)
        generation = _generation(generation)
        borrowed, words = _parent(parent, expected)
        self._sealed = True
        _check("install_begin", self._call("install_begin", self._cookie, borrowed, words, 3, generation))
        self._nodes[0] = TreeNode(True, 0, 0)
        self._sealed = False

    def add(self, parent, name, *, directory=False, length=0):
        return self._add("install_add", parent, name, directory, length, 64*1024*1024)

    def write(self, index, data):
        self._require(mutable=True)
        self._index(index)
        if type(data) is not bytes or not 0 < len(data) <= 65536:
            raise ValueError("A bounded installer chunk is required")
        buffer = C.create_string_buffer(data, len(data))
        self._sealed = True
        _check("install_write", self._call("install_write", self._cookie, index, buffer, len(data)))
        self._sealed = False

    def seal(self, index):
        self._require(mutable=True)
        self._index(index)
        node = self._nodes[index]
        self._sealed = True
        _check("install_seal", self._call("install_seal_file", self._cookie, index))
        self._nodes[index] = TreeNode(node.directory, node.ceiling, node.ceiling)
        self._sealed = False

    def finish(self):
        self._require(mutable=True)
        self._sealed = True
        _check("install_finish", self._call("install_finish", self._cookie, len(self._nodes), self._charge))
        self._finished = True
        self._sealed = False


class RuntimeAbsence(_TreeOwner):
    def observe(self, parent, expected, generation):
        self._require(mutable=True)
        generation = _generation(generation)
        borrowed, words = _parent(parent, expected)
        result = C.c_int()
        self._sealed = True
        _check("observe_absence", self._call("install_absent", self._cookie, borrowed, words, 3,
                                            generation, C.byref(result)))
        self._finished = True
        # Observation and closure remain distinct; no second observation.
        return bool(result.value)


class RuntimeRetirement(_TreeOwner):
    def begin(self, parent, expected, generation, root: ObjectIdentity | None):
        self._require(mutable=True)
        generation = _generation(generation)
        borrowed, words = _parent(parent, expected)
        root_words = None if root is None else _identity_words(root)
        self._sealed = True
        _check("retirement_begin", self._call("retirement_begin", self._cookie, borrowed, words,
                                             generation, root_words, 0 if root is None else 3))
        self._nodes[0] = TreeNode(True, 0, 0)
        self._sealed = False

    def add(self, parent, name, *, directory=False, ceiling=0):
        return self._add("retirement_add", parent, name, directory, ceiling, 64*1024*1024)

    def info(self, index):
        self._require()
        self._index(index)
        values = (_U64*2)()
        self._sealed = True
        _check("retirement_info", self._call("retirement_info", self._cookie, index, values, 2))
        node = self._nodes[index]
        self._nodes[index] = TreeNode(node.directory, node.ceiling, values[1])
        self._sealed = False
        return bool(values[0]), values[1]

    def prepare(self):
        self._require(mutable=True)
        self._sealed = True
        _check("retirement_prepare", self._call("retirement_prepare", self._cookie, len(self._nodes)))
        self._sealed = False

    def delete(self, index):
        self._require(mutable=True)
        self._index(index)
        self._sealed = True
        _check("retirement_delete", self._call("retirement_delete", self._cookie, index))
        self._sealed = False

    def finish(self):
        self._require(mutable=True)
        self._sealed = True
        _check("retirement_finish", self._call("retirement_finish", self._cookie))
        self._finished = True
        self._sealed = False
