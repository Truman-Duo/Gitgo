"""Checkpoint read dependencies and immutable CAS bytes, not a CAS v2 ticket.

The legacy writer uses these to keep file work outside final SQL publication.
Durable preparation, generation pins and qualified hard quotas are still gates
for the future protocol; an in-memory manifest grants none of them.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import NamedTuple

from .lifecycle import RevisionConflict
from .models import StorageBlocked


def _text(value, *, optional=False):
    if optional and value is None:
        return True
    if type(value) is not str:
        return False
    try:
        value.encode("utf8", errors="strict")
    except UnicodeError:
        return False
    return True


def _count(value):
    return type(value) is int and 0 <= value < (1 << 63)


class ProcessBasis(NamedTuple):
    process_id: str
    session_id: str
    task_id: str | None
    parent_process_id: str | None
    revision: int
    status: str
    runtime_ref: str | None


class TerminalBasis(NamedTuple):
    event_id: str
    sequence: int
    payload_ref: str
    occurred_at: str


@dataclass(frozen=True)
class CheckpointBasis:
    # Keep absence distinct from an existing process at revision zero.
    process: ProcessBasis | None
    event_sequence: int
    event_count: int
    terminal: TerminalBasis | None
    terminal_count: int
    deleted: bool
    parent_task_id: str | None

    def __post_init__(self):
        # Frozen outer containers do not freeze mutable nested values. Accept
        # only the exact repository value shapes before deriving any digest.
        process, terminal = self.process, self.terminal
        if (any(not _count(value) for value in (
                self.event_sequence, self.event_count, self.terminal_count))
                or type(self.deleted) is not bool
                or not _text(self.parent_task_id, optional=True)
                or process is not None and (
                    type(process) is not ProcessBasis
                    or not all(_text(value) for value in (
                        process.process_id, process.session_id, process.status))
                    or not all(_text(value, optional=True) for value in (
                        process.task_id, process.parent_process_id, process.runtime_ref))
                    or not _count(process.revision))
                or terminal is not None and (
                    type(terminal) is not TerminalBasis
                    or not _count(terminal.sequence)
                    or not all(_text(value) for value in (
                        terminal.event_id, terminal.payload_ref, terminal.occurred_at)))):
            raise StorageBlocked("checkpoint_basis_invalid")

    @property
    def revision(self):
        return int(self.process.revision) if self.process else 0

    @property
    def status(self):
        return str(self.process.status) if self.process else None

    @property
    def runtime_ref(self):
        return self.process.runtime_ref if self.process else None


def capture_basis(connection, process_id: str, parent_id: str) -> CheckpointBasis:
    """One SELECT observes all planning dependencies in one SQLite snapshot.

    Call under the State lock. No separate reader, write transaction, object
    read, repair or callback is needed to acquire or compare this basis.
    """
    cursor = connection.execute(
        """SELECT p.process_id, p.session_id, p.task_id, p.parent_process_id,
                  p.state_revision, p.status, p.runtime_state_ref,
                  COALESCE((SELECT MAX(sequence) FROM session_events
                            WHERE process_id=i.process_id), 0),
                  b.event_id, b.sequence, b.payload_ref, b.occurred_at,
                  (SELECT COUNT(*) FROM session_events
                   WHERE process_id=i.process_id AND event_type='agent_complete'),
                  EXISTS(SELECT 1 FROM deleted_processes WHERE process_id=i.process_id),
                  parent.task_id,
                  (SELECT COUNT(*) FROM session_events WHERE process_id=i.process_id)
           FROM (SELECT ? AS process_id, ? AS parent_id) AS i
           LEFT JOIN session_processes AS p ON p.process_id=i.process_id
           LEFT JOIN session_processes AS parent ON parent.process_id=i.parent_id
           LEFT JOIN session_events AS b ON b.event_id=(
               SELECT event_id FROM session_events WHERE process_id=i.process_id
                   AND event_type='agent_complete' ORDER BY sequence DESC LIMIT 1)""",
        (process_id, parent_id),
    )
    try:
        row = tuple(cursor.fetchone())
    finally:
        cursor.close()
    return CheckpointBasis(
        ProcessBasis(*row[:7]) if row[0] is not None else None, int(row[7]), int(row[15]),
        TerminalBasis(*row[8:12]) if row[8] is not None else None, int(row[12]),
        bool(row[13]), str(row[14]) if row[14] else None,
    )


def require_live_basis(basis: CheckpointBasis):
    if basis.deleted:
        raise ValueError("PROCESS_DELETED: a deleted process cannot be checkpointed again")


def recheck_basis(connection, process_id, parent_id, basis):
    """Invoke before publication writes, inside the final State transaction."""
    current = capture_basis(connection, process_id, parent_id)
    require_live_basis(current)
    if current != basis:
        raise RevisionConflict("STATE_REVISION_CHANGED: checkpoint dependencies changed; refresh before committing")


@dataclass(frozen=True)
class CheckpointObject:
    data: bytes
    digest: str
    created_at: str
    media_type: str = "application/json"

    def __post_init__(self):
        if (type(self.data) is not bytes or type(self.digest) is not str
                or hashlib.sha256(self.data).hexdigest() != self.digest
                or not _text(self.created_at) or not _text(self.media_type)):
            raise StorageBlocked("checkpoint_object_invalid")

    @property
    def descriptor(self):
        return MappingProxyType({
            "digest": self.digest, "ref": "sha256:" + self.digest,
            "byte_length": len(self.data), "media_type": self.media_type,
            "created_at": self.created_at,
        })


@dataclass(frozen=True)
class CheckpointObjects:
    members: tuple[CheckpointObject, ...]
    byte_length: int

    def __post_init__(self):
        if (type(self.members) is not tuple
                or any(type(member) is not CheckpointObject for member in self.members)
                or not _count(self.byte_length)
                or self.byte_length != sum(len(member.data) for member in self.members)
                or len({member.digest for member in self.members}) != len(self.members)):
            raise StorageBlocked("checkpoint_objects_invalid")

    def materialize_legacy(self, write_blob):
        """Exact precomputed bytes only; this is not durable batch admission."""
        for member in self.members:
            actual = write_blob(member.data, media_type=member.media_type)
            expected = member.descriptor
            if any(actual.get(key) != expected[key] for key in (
                "digest", "ref", "byte_length", "media_type",
            )):
                raise StorageBlocked("checkpoint_object_descriptor_changed")


class CheckpointObjectBuilder:
    """Encode the whole closure before creating any file, using legacy limits.

    Aggregate bytes count unique output objects. This preflight is neither a
    reservation of free disk nor a bound on caller input/Python heap/SQL growth.
    """
    def __init__(self, encode, policy, now):
        self._encode, self._policy, self._now = encode, policy, now
        self._members = {}
        self._byte_length = 0
        self._sealed = False

    def prepare(self, value):
        if self._sealed:
            raise RuntimeError("checkpoint manifest already sealed")
        data = self._encode(value)
        if len(data) > self._policy.max_cas_object_bytes:
            raise StorageBlocked("cas_object_too_large")
        digest = hashlib.sha256(data).hexdigest()
        prior = self._members.get(digest)
        if prior is not None:
            if prior.data != data:
                raise StorageBlocked("checkpoint_object_digest_collision")
            return prior.descriptor
        total = self._byte_length + len(data)
        if total > min(self._policy.cas_max_bytes, self._policy.total_max_bytes):
            raise StorageBlocked("checkpoint_closure_too_large")
        member = CheckpointObject(data, digest, self._now)
        self._members[digest] = member
        self._byte_length = total
        return member.descriptor

    def seal(self):
        self._sealed = True
        return CheckpointObjects(tuple(self._members.values()), self._byte_length)
