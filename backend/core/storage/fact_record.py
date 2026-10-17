"""Exact retained State fact row, shared by SQL publication and recovery.

This value describes evidence; it never grants execution, creates a receipt or
repairs a missing row. The caller owns the committed snapshot/lifetime. Fact
retirement needs its own durable evidence before absence can be accepted.
"""
from __future__ import annotations

from dataclasses import astuple, dataclass

from .models import StorageBlocked


INSERT_FACT_SQL = """INSERT INTO runtime_outbox(
        event_id,schema_version,kind,project_id,session_id,task_id,process_id,stream_id,
        stream_sequence,producer,correlation_id,causation_id,payload_ref,occurred_at)
        VALUES(?,1,?,?,?,?,?,?,?,'StateRepository',?,?,?,?)"""

FACT_COLUMNS = (
    'event_id', 'kind', 'project_id', 'session_id', 'task_id', 'process_id',
    'stream_id', 'stream_sequence', 'correlation_id', 'causation_id',
    'payload_ref', 'occurred_at',
)


@dataclass(frozen=True)
class RuntimeFactRecord:
    event_id: str
    kind: str
    project_id: str
    session_id: str
    task_id: str
    process_id: str
    stream_id: str
    stream_sequence: int
    correlation_id: str
    causation_id: str
    payload_ref: str
    occurred_at: str

    def __post_init__(self):
        for name in FACT_COLUMNS:
            value = getattr(self, name)
            valid = (type(value) is int and 0 < value < (1 << 63)
                     if name == 'stream_sequence' else type(value) is str)
            if not valid:
                raise StorageBlocked('runtime_fact_record_invalid')

    @property
    def parameters(self) -> tuple:
        return astuple(self)

    def require_row(self, connection) -> None:
        """Check exact facts in the caller's snapshot, without repair/replay."""
        cursor = connection.execute(
            'SELECT ' + ','.join(FACT_COLUMNS) + ',schema_version,producer '
            'FROM runtime_outbox WHERE event_id=?', (self.event_id,),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None or tuple(row) != (*self.parameters, 1, 'StateRepository'):
            raise StorageBlocked('runtime_fact_evidence_mismatch')
