"""Native lifetime of a trusted storage worker, separate from admission.

The selected installed runtime supplies the native module, immutable command,
and retained code/namespace pins. A command is not an approval or reservation.
Creation is suspended; the coordinator must persist the exact process identity
before resume. Retirement reports native quiescence, never business success,
SQL commit, generation absence, or permission to reuse storage capacity.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import PureWindowsPath
import subprocess
import sys
from typing import Any

from .models import StorageBlocked

_U64 = ctypes.c_uint64
_U32 = ctypes.c_uint32
_MAX_OWNERS = 8
_LIVE_PROCESSES: dict[int, WindowsStorageProcess] = {}


class StorageProcessError(StorageBlocked):
    code = "STORAGE_NATIVE_PROCESS"

    def __init__(self, operation: str, native_error: int):
        self.operation = operation
        self.native_error = native_error
        super().__init__(f"{self.code}: {operation} ({native_error})")


def _check(operation: str, result: int) -> None:
    if result:
        raise StorageProcessError(operation, result)


def _string(value: str, maximum: int) -> None:
    if type(value) is not str or "\0" in value or len(value.encode("utf-16-le")) // 2 > maximum:
        raise ValueError("A bounded native Unicode string is required")


def _absolute(value: str) -> None:
    _string(value, 32760)
    path = PureWindowsPath(value)
    if (not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":"
            or str(path) != value or any(part in {".", ".."} for part in path.parts)):
        raise ValueError("An explicit canonical local Windows path is required")


@dataclass(frozen=True)
class StorageProcessIdentity:
    pid: int
    creation_time: int

    def __post_init__(self) -> None:
        if (type(self.pid) is not int or not 0 < self.pid < 2**32
                or type(self.creation_time) is not int or not 0 < self.creation_time < 2**64):
            raise ValueError("An exact bounded native process identity is required")


@dataclass(frozen=True)
class StorageWorkerCommand:
    """Closed launch inputs supplied by the independently verified runtime.

    No inherited environment, PATH executable lookup, shell, venv launcher,
    or default working directory is supplied here. Trust and the content
    contract still belong to the selected installation, not these strings.
    """
    argv: tuple[str, ...]
    directory: str
    environment: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if type(self.argv) is not tuple or not 1 <= len(self.argv) <= 64:
            raise ValueError("A bounded immutable argument vector is required")
        for arg in self.argv:
            _string(arg, 32760)
        _absolute(self.argv[0])
        _absolute(self.directory)
        _string(self.command_line, 32766)
        if (type(self.environment) is not tuple or len(self.environment) != 3
                or any(type(pair) is not tuple or len(pair) != 2 for pair in self.environment)
                or tuple(pair[0] for pair in self.environment) != ("PATH", "SYSTEMROOT", "WINDIR")):
            raise ValueError("An explicit closed storage runtime environment is required")
        for _, value in self.environment:
            _absolute(value)
            if ";" in value:
                raise ValueError("The storage worker PATH must be one qualified directory")
        if self.environment[1][1] != self.environment[2][1]:
            raise ValueError("Windows system roots must agree")
        if len(self.environment_block.encode("utf-16-le")) // 2 > 65536:
            raise ValueError("Storage environment is too large")

    @property
    def command_line(self) -> str:
        return subprocess.list2cmdline(self.argv)

    @property
    def environment_block(self) -> str:
        return "\0".join(f"{key}={value}" for key, value in self.environment) + "\0\0"


@dataclass(frozen=True)
class StorageProcessStatus:
    started: bool
    sealed: bool
    pid: int
    active: int
    total: int
    handles: int
    unknown_closes: int
    last_error: int
    close_calls: int


class WindowsProcessLibrary:
    """Bind the trusted loaded ABI; no DLL discovery or runtime activation.

    ABI 1 permits eight owners, one process per Job, 256 MiB Job memory, 30
    seconds of aggregate user CPU, a hard CPU rate cap, and no breakaway.
    The admission profile must charge these bounds and all retained owners.
    This is a trusted storage worker, not the untrusted tool sandbox backend.
    """

    def __init__(self, loaded_module: Any):
        if sys.platform != "win32":
            raise StorageProcessError("unsupported_platform", 50)
        self._module = loaded_module
        self._owners: dict[int, WindowsStorageProcess] = {}
        signatures = {
            "abi": [], "reserve": [ctypes.POINTER(_U64)],
            "start": [_U64, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p],
            "identity": [_U64, ctypes.POINTER(_U64), _U32],
            "resume": [_U64, _U32, _U64],
            "ended": [_U64, _U32, _U64, ctypes.POINTER(ctypes.c_int)],
            "read": [_U64, ctypes.c_int, ctypes.c_void_p, _U32, ctypes.POINTER(_U32)],
            "release": [_U64], "status": [_U64, ctypes.POINTER(_U64), _U32],
            "retire": [_U64, ctypes.c_int],
        }
        self._calls = {}
        for name, arguments in signatures.items():
            call = getattr(loaded_module, "gitgo_process_" + name)
            call.argtypes, call.restype = arguments, _U32
            self._calls[name] = call
        if self._calls["abi"]() != 1:
            raise StorageProcessError("unsupported_abi", 50)

    @property
    def retained_processes(self) -> int:
        return len(self._owners)

    def reserve(self) -> WindowsStorageProcess:
        return WindowsStorageProcess(self)


class WindowsStorageProcess:
    def __init__(self, library: WindowsProcessLibrary):
        if (type(library) is not WindowsProcessLibrary or library.retained_processes >= _MAX_OWNERS
                or len(_LIVE_PROCESSES) >= _MAX_OWNERS):
            raise StorageProcessError("owner_capacity", 1816)
        self._library = library
        self._cookie = _U64()
        self._sealed = False
        self._retired = False
        library._owners[id(self)] = self
        _LIVE_PROCESSES[id(self)] = self
        try:
            _check("reserve", self._call("reserve", ctypes.byref(self._cookie)))
        except BaseException:
            if not self._cookie.value:
                library._owners.pop(id(self), None)
                _LIVE_PROCESSES.pop(id(self), None)
            raise

    def _call(self, name: str, *arguments):
        return self._library._calls[name](*arguments)

    def _require(self) -> None:
        if self._sealed or self._retired:
            raise StorageProcessError("owner_sealed", 5023)

    def create(self, command: StorageWorkerCommand) -> None:
        self._require()
        if type(command) is not StorageWorkerCommand:
            raise ValueError("An immutable storage worker command is required")
        # All allocations/conversions precede the first native effect.
        line = ctypes.create_unicode_buffer(command.command_line)
        environment = ctypes.create_unicode_buffer(command.environment_block)
        self._sealed = True
        _check("create", self._call("start", self._cookie, command.argv[0], line,
                                   command.directory, environment))
        self._sealed = False

    def identity(self) -> tuple[StorageProcessIdentity, bool]:
        values = (_U64 * 3)()
        _check("identity", self._call("identity", self._cookie, values, 3))
        return StorageProcessIdentity(values[0], values[1]), bool(values[2])

    def resume(self, identity: StorageProcessIdentity) -> None:
        self._require()
        if type(identity) is not StorageProcessIdentity:
            raise ValueError("An exact process identity is required")
        self._sealed = True
        _check("resume", self._call("resume", self._cookie, identity.pid, identity.creation_time))
        self._sealed = False

    def ended(self, identity: StorageProcessIdentity) -> bool:
        """Observe one exact process; both observed owners still need retirement."""
        self._require()
        if type(identity) is not StorageProcessIdentity:
            raise ValueError("An exact process identity is required")
        output = ctypes.c_int()
        self._sealed = True
        _check("ended", self._call("ended", self._cookie, identity.pid,
                                  identity.creation_time, ctypes.byref(output)))
        result = bool(output.value)
        self._sealed = False
        return result

    def status(self) -> StorageProcessStatus:
        values = (_U64 * 9)()
        _check("status", self._call("status", self._cookie, values, 9))
        return StorageProcessStatus(bool(values[0]), bool(values[1]), *values[2:])

    def read(self, *, stderr: bool = False) -> bytes:
        self._require()
        if type(stderr) is not bool:
            raise ValueError("A native output stream selector is required")
        data, count = ctypes.create_string_buffer(4096), _U32()
        self._sealed = True
        result = self._call("read", self._cookie, stderr, data, len(data), ctypes.byref(count))
        if result == 109:  # ERROR_BROKEN_PIPE is EOF, never a completion receipt.
            self._sealed = False
            return b""
        _check("read", result)
        value = data.raw[:count.value]
        self._sealed = False
        return value

    def release(self) -> None:
        """Single bounded handoff byte; it carries no business authority."""
        self._require()
        self._sealed = True
        _check("release", self._call("release", self._cookie))
        self._sealed = False

    def retire(self, *, terminate: bool = True) -> bool:
        """Nonblocking native retirement. False preserves the entire owner.

        Unknown queries/closes raise and retain the owner as debt. A recorded
        successful retirement may be observed again after a lost FFI return;
        neither that observation nor retry touches a later generation.
        """
        if type(terminate) is not bool:
            raise ValueError("An explicit native termination choice is required")
        if not self._retired:
            self._sealed = True
            result = self._call("retire", self._cookie, terminate)
            if result == 170:  # ERROR_BUSY: actual root or Job still alive.
                return False
            _check("retire", result)
            self._retired = True
        self._library._owners.pop(id(self), None)
        _LIVE_PROCESSES.pop(id(self), None)
        return True
