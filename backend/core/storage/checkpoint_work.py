"""Complete immutable checkpoint publication shared by admission and writer.

The repository constructs this value. It is neither a grant nor a fresh file
ticket. Callers must own their connection/transaction and, for CAS v2, a durable
prepared batch with exact native generations and the same publication digest.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

from .checkpoint_limits import CheckpointFootprint, CheckpointLimits, capture_footprint
from .checkpoint_plan import CheckpointBasis, CheckpointObjects, recheck_basis
from .checkpoint_sql import CheckpointSql
from .fact_record import INSERT_FACT_SQL, RuntimeFactRecord
from .lifecycle import CheckpointRef, RevisionConflict
from .models import StorageBlocked


@dataclass(frozen=True)
class CheckpointPublication:
    process_id: str
    session_id: str
    parent_process_id: str
    basis: CheckpointBasis
    objects: CheckpointObjects
    writes: CheckpointSql
    estimated_sql_bytes: int
    messages: int
    dependencies: int
    terminal: bool
    facts: tuple[RuntimeFactRecord, ...]
    limits: CheckpointLimits | None = None
    footprint: CheckpointFootprint | None = None

    def __post_init__(self):
        if (type(self.facts) is not tuple or not 1 <= len(self.facts) <= 2
                or any(type(fact) is not RuntimeFactRecord for fact in self.facts)
                or len({fact.event_id for fact in self.facts}) != len(self.facts)):
            raise StorageBlocked('checkpoint_fact_plan_invalid')
        written = tuple(statement.parameters for statement in self.writes.statements
                        if statement.sql == INSERT_FACT_SQL)
        if written != tuple(fact.parameters for fact in self.facts):
            raise StorageBlocked('checkpoint_fact_sql_mismatch')
        if ((self.limits is None) != (self.footprint is None)
                or self.limits is not None and (
                    type(self.limits) is not CheckpointLimits
                    or type(self.footprint) is not CheckpointFootprint)):
            raise StorageBlocked('checkpoint_publication_profile_mismatch')
        if (any(type(value) is not int or value < 0 for value in (
                self.estimated_sql_bytes,self.messages,self.dependencies))
                or type(self.terminal) is not bool):
            raise StorageBlocked('checkpoint_publication_work_invalid')

    @property
    def digest(self):
        value = ['checkpoint-publication-v2',self.writes.digest,
                 self.process_id,self.session_id,self.parent_process_id,
                 self.estimated_sql_bytes,self.messages,self.dependencies,self.terminal,
                 [fact.parameters for fact in self.facts],
                 asdict(self.limits) if self.limits is not None else None,
                 asdict(self.footprint) if self.footprint is not None else None]
        raw = json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
        return hashlib.sha256(raw).hexdigest()

    def apply(self, connection):
        """No file work, conversion, admission or implicit transaction here."""
        if not connection.in_transaction:
            raise RuntimeError('CHECKPOINT_PUBLICATION_REQUIRES_TRANSACTION')
        if self.limits is not None:
            current = capture_footprint(connection,self.limits,self.process_id,self.session_id)
            if current != self.footprint:
                raise RevisionConflict('STATE_REVISION_CHANGED: checkpoint footprint changed')
            self.limits.require_transition(current,len(self.objects.members),self.messages,self.dependencies,self.terminal)
        recheck_basis(connection,self.process_id,self.parent_process_id,self.basis)
        self.writes.apply(connection)

    def require_retained_facts(self, connection):
        """Caller owns a committed snapshot; missing evidence never replays."""
        for fact in self.facts:
            fact.require_row(connection)

    def reference(self):
        # Construct a return value only after the owner confirms COMMIT.
        return CheckpointRef(f'sqlite:session/{self.process_id}/{self.basis.event_sequence}',self.basis.revision+1)
