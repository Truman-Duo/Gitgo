"""Inactive storage-envelope values; neither allocation nor execution authority.

Every concurrent file copy is charged at its individual rounded ceiling. The
Host registry must reserve this entire envelope before store initialization.
This arithmetic excludes filesystem metadata and is not an OS volume quota,
free-space reservation, or evidence that a backend enforces these ceilings.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from backend.core.storage.models import StorageBlocked


MAX_ENVELOPE_BYTES = 8192
MAX_ACCOUNTED_BYTES = (1 << 53) - 1
MAX_STORE_SLOTS = 64


class StorageEnvelopeInvalid(StorageBlocked):
    code = "STORAGE_ENVELOPE_INVALID"


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise StorageEnvelopeInvalid(reason)


def _integer(value: int, maximum: int = MAX_ACCOUNTED_BYTES) -> None:
    _require(type(value) is int and 0 < value <= maximum, "invalid positive bounded integer")


@dataclass(frozen=True)
class FileEnvelope:
    """A named file class and its maximum simultaneously retained copies."""
    name: str
    max_bytes: int
    max_files: int

    def __post_init__(self) -> None:
        _require(type(self.name) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", self.name) is not None,
                 "invalid file class name")
        _integer(self.max_bytes)
        _integer(self.max_files, 65536)


@dataclass(frozen=True)
class StorageEnvelope:
    """Canonical, finite descriptor consumed by a future qualified registry.

    No default limits or path-derived policy are supplied. In particular,
    changing state_home cannot construct a second budget authority from this
    value. Its digest identifies a descriptor; it is not a reservation ticket.
    """
    version: int
    allocation_unit: int
    slots: int
    host_max_bytes: int
    registry_files: tuple[FileEnvelope, ...]
    files: tuple[FileEnvelope, ...]

    def __post_init__(self) -> None:
        _require(type(self.version) is int and self.version == 1, "unsupported envelope version")
        _integer(self.allocation_unit, 65536)
        _require(self.allocation_unit & (self.allocation_unit-1) == 0, "allocation unit must be a power of two")
        _integer(self.slots, MAX_STORE_SLOTS)
        _integer(self.host_max_bytes)
        self._file_classes(self.registry_files)
        self._file_classes(self.files)
        _require(self.reserved_max_bytes <= self.host_max_bytes, "Host envelope is underfunded")

    @staticmethod
    def _file_classes(files: tuple[FileEnvelope, ...]) -> None:
        _require(type(files) is tuple and 1 <= len(files) <= 32,
                 "bounded immutable file classes required")
        _require(all(type(item) is FileEnvelope for item in files), "invalid file class")
        names = [item.name for item in files]
        _require(names == sorted(set(names)), "file classes must be unique and sorted")

    def _rounded(self, size: int) -> int:
        result = ((size + self.allocation_unit-1)//self.allocation_unit)*self.allocation_unit
        _integer(result)
        return result

    def _total(self, files: tuple[FileEnvelope, ...]) -> int:
        total = sum(self._rounded(item.max_bytes)*item.max_files for item in files)
        _integer(total)
        return total

    @property
    def registry_max_bytes(self) -> int:
        return self._total(self.registry_files)

    @property
    def store_max_bytes(self) -> int:
        return self._total(self.files)

    @property
    def reserved_max_bytes(self) -> int:
        total = self.registry_max_bytes + self.slots*self.store_max_bytes
        _integer(total)
        return total

    def to_bytes(self) -> bytes:
        def encode(files):
            return [dict(name=item.name,max_bytes=item.max_bytes,max_files=item.max_files) for item in files]
        value = dict(version=self.version, allocation_unit=self.allocation_unit,
                     slots=self.slots, host_max_bytes=self.host_max_bytes,
                     registry_files=encode(self.registry_files),files=encode(self.files))
        raw = json.dumps(value,sort_keys=True,separators=(",", ":"),ensure_ascii=True).encode("ascii")
        _require(len(raw) <= MAX_ENVELOPE_BYTES, "envelope encoding too large")
        return raw

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, raw: bytes) -> StorageEnvelope:
        _require(type(raw) is bytes and 0 < len(raw) <= MAX_ENVELOPE_BYTES, "invalid envelope encoding size")
        def pairs(items):
            result = {}
            for key,value in items:
                _require(key not in result, "duplicate envelope field")
                result[key] = value
            return result
        def nonfinite(value):
            raise StorageEnvelopeInvalid("nonfinite envelope value")
        try:
            value = json.loads(raw,object_pairs_hook=pairs,parse_constant=nonfinite)
        except (ValueError,RecursionError) as error:
            raise StorageEnvelopeInvalid("invalid envelope encoding") from error
        fields = {"version","allocation_unit","slots","host_max_bytes","registry_files","files"}
        _require(type(value) is dict and set(value) == fields, "unknown or missing envelope field")
        def parse(files):
            _require(type(files) is list and 1 <= len(files) <= 32, "invalid file class array")
            for item in files:
                _require(type(item) is dict and set(item) == {"name","max_bytes","max_files"}, "invalid file class fields")
            return tuple(FileEnvelope(**item) for item in files)
        descriptor = cls(**dict(value,registry_files=parse(value["registry_files"]),files=parse(value["files"])))
        _require(raw == descriptor.to_bytes(), "noncanonical envelope encoding")
        return descriptor
