"""Migrate existing shadow databases through the storage facade, never paths to
business files. SQL comes from this installed runtime's migration catalogue.
The caller holds source maintenance leases and only supplies disposable copies.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

from .migrations import STATE_MIGRATIONS, OBSERVABILITY_MIGRATIONS
from .models import StorageBlocked
from .runtime import StorageRuntime
from .support import validate_sqlite_runtime


def migrate_shadow_database(path: Path, name: str):
    validate_sqlite_runtime()
    catalog = {"state": STATE_MIGRATIONS, "observability": OBSERVABILITY_MIGRATIONS}
    if name not in catalog or path.is_symlink() or path != path.resolve() or not path.is_file():
        raise StorageBlocked("UPDATE_DATA_PATH")
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if any(row[0] != "ok" for row in db.execute("PRAGMA quick_check")):
            raise StorageBlocked("UPDATE_DATA_INTEGRITY")
        StorageRuntime._apply_migrations(db, catalog[name])
        if any(row[0] != "ok" for row in db.execute("PRAGMA quick_check")) or db.execute("PRAGMA foreign_key_check").fetchone():
            raise StorageBlocked("UPDATE_DATA_INTEGRITY")
        result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result[0]: raise StorageBlocked("UPDATE_DATA_BUSY")
