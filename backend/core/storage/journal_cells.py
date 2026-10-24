"""Bounded two-cell journal framing shared by Host registry record types.

This is a codec, not I/O, admission, or a cleanup capability. A nonempty torn
peer is unknown: it cannot be ignored to resurrect an older reusable state.
Native writers must compare the full prior bytes, flush, and verify readback.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import struct
from typing import Callable, Generic, TypeVar
import uuid

from .models import StorageBlocked

CELL_BYTES = 4096
SLOT_BYTES = 2 * CELL_BYTES
MAX_SEQUENCE = (1 << 64) - 1
HEADER = struct.Struct("<8sQII32s")
PAYLOAD_BYTES = CELL_BYTES - HEADER.size - 32
ZERO = bytes(32)
T = TypeVar("T")


class JournalUnknown(StorageBlocked):
    code = "STORAGE_JOURNAL_UNKNOWN"


def require(condition, reason):
    if not condition:
        raise JournalUnknown(reason)


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("ascii")
    except (ValueError, TypeError, RecursionError) as error:
        raise JournalUnknown("invalid canonical data") from error


def _pairs(values):
    result = {}
    for key, value in values:
        require(key not in result, "duplicate key")
        result[key] = value
    return result


def _nonfinite(_):
    raise JournalUnknown("nonfinite metadata")


def decode(raw):
    require(type(raw) is bytes and 0 < len(raw) <= PAYLOAD_BYTES, "bounded metadata")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (ValueError, RecursionError, UnicodeError) as error:
        raise JournalUnknown("invalid metadata") from error
    require(canonical(value) == raw, "noncanonical metadata")
    return value


def number(value, maximum, minimum=0):
    require(type(value) is int and minimum <= value <= maximum, "bounded integer")


def guid(value):
    require(type(value) is str, "identity type")
    try:
        require(str(uuid.UUID(value)) == value, "canonical identity")
    except ValueError as error:
        raise JournalUnknown("invalid identity") from error


def digest(value):
    require(type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None, "digest")


@dataclass(frozen=True)
class JournalCell(Generic[T]):
    index: int
    sequence: int
    previous: bytes
    state: T
    checksum: bytes


@dataclass(frozen=True)
class JournalView(Generic[T]):
    raw: bytes
    head: JournalCell[T] | None
    peer: JournalCell[T] | None


@dataclass(frozen=True)
class JournalUpdate:
    prior_digest: bytes
    target: int
    sequence: int
    encoded: bytes
    result_digest: bytes

    def require_prior(self, view):
        require(type(view) is JournalView and hashlib.sha256(view.raw).digest() == self.prior_digest,
                "stale journal view")

    def require_result(self, view):
        require(type(view) is JournalView and view.head is not None and view.head.index == self.target
                and view.head.sequence == self.sequence and view.head.checksum == self.encoded[-32:]
                and hashlib.sha256(view.raw).digest() == self.result_digest,
                "journal result unconfirmed")


@dataclass(frozen=True)
class JournalFormat(Generic[T]):
    """Trusted code selects its record contract; bytes cannot select a codec."""
    magic: bytes
    parse_state: Callable[[bytes], T]
    encode_state: Callable[[T], bytes]
    transition: Callable[[T | None, T], None]

    def __post_init__(self):
        require(type(self.magic) is bytes and len(self.magic) == 8 and any(self.magic), "journal magic")

    def _cell(self, raw: bytes, index: int):
        require(type(raw) is bytes and len(raw) == CELL_BYTES, "exact cell bytes")
        if not any(raw):
            return None
        checksum = hashlib.sha256(raw[:-32]).digest()
        require(checksum == raw[-32:], "unknown or torn cell checksum")
        magic, sequence, length, reserved, previous = HEADER.unpack(raw[:HEADER.size])
        require(magic == self.magic and not reserved, "journal version")
        number(sequence, MAX_SEQUENCE, 1)
        number(length, PAYLOAD_BYTES, 1)
        require(index == (sequence - 1) % 2, "journal sequence position")
        require(not any(raw[HEADER.size + length:-32]), "journal padding")
        state = self.parse_state(raw[HEADER.size:HEADER.size + length])
        return JournalCell(index, sequence, previous, state, checksum)

    def read(self, raw: bytes) -> JournalView[T]:
        require(type(raw) is bytes and len(raw) == SLOT_BYTES, "exact preallocated journal")
        known = sorted((cell for i in range(2)
                        if (cell := self._cell(raw[i*CELL_BYTES:(i+1)*CELL_BYTES], i)) is not None),
                       key=lambda cell: cell.sequence)
        if not known:
            return JournalView(raw, None, None)
        if len(known) == 1:
            head = known[0]
            require(head.sequence == 1 and head.previous == ZERO, "missing peer or unknown sequence")
            self.transition(None, head.state)
            return JournalView(raw, head, None)
        older, newer = known
        require(newer.sequence == older.sequence + 1 and newer.previous == older.checksum, "journal chain")
        self.transition(older.state, newer.state)
        return JournalView(raw, newer, older)

    def next_update(self, view: JournalView[T], state: T) -> JournalUpdate:
        require(type(view) is JournalView and view == self.read(view.raw), "exact journal snapshot")
        self.transition(view.head.state if view.head else None, state)
        sequence = view.head.sequence + 1 if view.head else 1
        number(sequence, MAX_SEQUENCE, 1)
        payload = self.encode_state(state)
        require(type(payload) is bytes and 0 < len(payload) <= PAYLOAD_BYTES, "bounded journal payload")
        require(self.parse_state(payload) == state, "canonical immutable journal state")
        previous = view.head.checksum if view.head else ZERO
        prefix = HEADER.pack(self.magic, sequence, len(payload), 0, previous) + payload
        prefix += bytes(CELL_BYTES - 32 - len(prefix))
        encoded = prefix + hashlib.sha256(prefix).digest()
        target = 1 - view.head.index if view.head else 0
        raw = view.raw[:target*CELL_BYTES] + encoded + view.raw[(target+1)*CELL_BYTES:]
        return JournalUpdate(hashlib.sha256(view.raw).digest(), target, sequence, encoded,
                             hashlib.sha256(raw).digest())
