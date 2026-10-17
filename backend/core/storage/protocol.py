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
    # SQLite object lookup is ASCII case-insensitive; sqlite_schema preserves
    # the spelling used to create an object. Inspect all application objects
    # so missing history cannot hide an already populated database either.
    objects = [(name, kind) for name, kind in connection.execute(
        'SELECT lower(name), type FROM sqlite_schema',
    ).fetchall() if not name.startswith('sqlite_')]
    reserved = {'schema_migration_records', 'storage_protocol', 'schema_migrations_append'}
    if any(name in reserved for name, _ in objects):
        raise StorageProtocolUnsupported("guarded_format")
    history = [kind for name, kind in objects if name == 'schema_migrations']
    other_objects = [name for name, _ in objects if name != 'schema_migrations']
    if not history:
        if other_objects:
            raise StorageProtocolUnsupported('missing_history')
        return
    if history != ['table']:
        raise StorageProtocolUnsupported("guarded_history")
    migrations = tuple(migrations)
    known = {migration.version: migration.checksum for migration in migrations}
    rows = connection.execute(
        'SELECT version, checksum FROM schema_migrations ORDER BY version',
    ).fetchall()
    if not rows and other_objects:
        raise StorageProtocolUnsupported('empty_history')
    for version, checksum in rows:
        if type(version) is not int:
            raise StorageProtocolUnsupported('invalid_history')
        if version not in known:
            raise StorageProtocolUnsupported("unknown_migration")
        if checksum != known[version]:
            raise RuntimeError(f"Migration checksum mismatch at version {version}")
    if [row[0] for row in rows] != [migration.version for migration in migrations[:len(rows)]]:
        raise StorageProtocolUnsupported('non_prefix_history')
