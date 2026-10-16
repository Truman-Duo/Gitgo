"""Read-only format admission before migrations, salvage or replacement.

CAS lifecycle v2 is deliberately NOT supported yet. Recognizing its guard
must never register a compatibility function that admits legacy writers.
There is no upgrade/activation API in this increment.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

from .migrations import Migration
from .models import StorageBlocked


class StorageProtocolUnsupported(StorageBlocked):
    code = "STORAGE_PROTOCOL_UNSUPPORTED"

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"{self.code}: {reason}; use a compatible storage runtime")


def require_legacy_storage_format(
    connection: sqlite3.Connection, migrations: Iterable[Migration],
) -> None:
    """Reject guarded or newer stores without changing schema or running a UDF.

    Call before writer PRAGMAs and before creating a salvage destination. A
    missing or unreadable history is not evidence that a guarded store is new.
    SQLite errors propagate; callers must not turn them into empty history.
    """
    objects = dict(connection.execute(
        """SELECT name, type FROM sqlite_schema WHERE name IN (
               'schema_migrations', 'schema_migration_records',
               'storage_protocol', 'schema_migrations_append')""",
    ).fetchall())
    if any(name != "schema_migrations" for name in objects):
        raise StorageProtocolUnsupported("guarded_format")
    kind = objects.get("schema_migrations")
    if kind is None:
        return
    if kind != "table":
        raise StorageProtocolUnsupported("guarded_history")
    known = {migration.version: migration.checksum for migration in migrations}
    for version, checksum in connection.execute(
        "SELECT version, checksum FROM schema_migrations",
    ).fetchall():
        if version not in known:
            raise StorageProtocolUnsupported("unknown_migration")
        if checksum != known[version]:
            raise RuntimeError(f"Migration checksum mismatch at version {version}")
