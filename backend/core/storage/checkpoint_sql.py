"""Closed checkpoint SQL writes, compiled before any CAS materialization.

This internal plan records trusted repository SQL and already-normalized scalar
parameters. It is not a permission, a durable ticket, or a disk reservation.
Its digest binds the captured State basis and complete generated CAS closure as
well as SQL-only fields. Physical growth still requires a qualified profile.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math

from .checkpoint_plan import CheckpointBasis, CheckpointObjects, _text
from .lifecycle import RevisionConflict
from .models import StorageBlocked


def _scalar(value):
    # Do not retain mutable containers or invoke SQLite adapters after files
    # exist. SQL integers are signed 64-bit; NaN would silently become NULL.
    if value is None or type(value) is bytes:
        return value
    if type(value) is str:
        value.encode("utf8", errors="strict")
        return value
    if type(value) is int and -(1 << 63) <= value < (1 << 63):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise StorageBlocked("checkpoint_sql_scalar_invalid")


def _encoded_scalar(value):
    if value is None:
        return ["null"]
    if type(value) is bytes:
        return ["bytes", value.hex()]
    if type(value) is float:
        return ["float", value.hex()]
    return [type(value).__name__, value]


@dataclass(frozen=True)
class CheckpointStatement:
    sql: str
    parameters: tuple
    expected_rows: int | None = None

    def __post_init__(self):
        if (not _text(self.sql) or not self.sql
                or type(self.parameters) is not tuple
                or self.expected_rows is not None and (
                    type(self.expected_rows) is not int
                    or not 0 <= self.expected_rows < (1 << 63))):
            raise StorageBlocked("checkpoint_sql_statement_invalid")
        for value in self.parameters:
            _scalar(value)


def _parameter_bytes(statements):
    return sum(
        len(value.encode("utf8")) if type(value) is str else
        len(value) if type(value) is bytes else 0 if value is None else 8
        for item in statements for value in item.parameters
    )


def _binding_digest(statements, basis, objects, process_id, parent_process_id):
    if (type(basis) is not CheckpointBasis or type(objects) is not CheckpointObjects
            or not _text(process_id) or not _text(parent_process_id)):
        raise StorageBlocked("checkpoint_sql_binding_invalid")
    encoded = [
        [item.sql, [_encoded_scalar(value) for value in item.parameters],
         item.expected_rows] for item in statements
    ]
    binding = ["checkpoint-sql-v1", process_id, parent_process_id,
               asdict(basis), [dict(item.descriptor) for item in objects.members],
               encoded]
    data = json.dumps(binding, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf8")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CheckpointSql:
    statements: tuple[CheckpointStatement, ...]
    digest: str
    parameter_bytes: int

    def __post_init__(self):
        if (type(self.statements) is not tuple
                or any(type(item) is not CheckpointStatement for item in self.statements)
                or type(self.digest) is not str or len(self.digest) != 64
                or any(char not in "0123456789abcdef" for char in self.digest)
                or type(self.parameter_bytes) is not int
                or self.parameter_bytes != _parameter_bytes(self.statements)):
            raise StorageBlocked("checkpoint_sql_plan_invalid")

    def require_binding(self, basis, objects, process_id, parent_process_id):
        # A stored digest is a claim until it has been recomputed against the
        # complete immutable closure. This validates consistency, not authority
        # to accept SQL from a caller or persisted external request.
        expected = _binding_digest(self.statements, basis, objects,
                                   process_id, parent_process_id)
        if self.digest != expected:
            raise StorageBlocked("checkpoint_sql_binding_mismatch")

    def apply(self, connection):
        """Use only after basis revalidation inside the owning transaction.

        No snapshot conversion, file operation or caller callback occurs here.
        The caller owns transaction outcome handling and uncertain-commit debt.
        """
        for statement in self.statements:
            cursor = connection.execute(statement.sql, statement.parameters)
            try:
                if (statement.expected_rows is not None
                        and cursor.rowcount != statement.expected_rows):
                    raise RevisionConflict(
                        "STATE_REVISION_CHANGED: checkpoint was not committed")
            finally:
                cursor.close()


class CheckpointSqlBuilder:
    """Internal scalar-only sink for existing object/outbox SQL helpers.

    It never opens a database or executes SQL. Do not expose it as an arbitrary
    publication callback. Only the checkpoint repository constructs its writes.
    """
    def __init__(self):
        self._statements = []
        self._sealed = False

    def _append(self, sql, parameters, expected_rows=None):
        if self._sealed:
            raise RuntimeError("checkpoint SQL plan already sealed")
        if type(sql) is not str:
            raise TypeError("SQL must be a repository-owned string")
        values = tuple(_scalar(value) for value in parameters)
        self._statements.append(CheckpointStatement(sql, values, expected_rows))

    def execute(self, sql, parameters):
        self._append(sql, parameters)

    def executemany(self, sql, rows):
        for parameters in rows:
            self._append(sql, parameters)

    def expect_one(self, sql, parameters):
        self._append(sql, parameters, expected_rows=1)

    def seal(self, basis, objects, process_id, parent_process_id):
        if self._sealed:
            raise RuntimeError("checkpoint SQL plan already sealed")
        self._sealed = True
        statements = tuple(self._statements)
        digest = _binding_digest(statements, basis, objects, process_id, parent_process_id)
        return CheckpointSql(statements, digest, _parameter_bytes(statements))
