"""Migrate existing shadow databases through the storage facade, never paths to
business files. SQL comes from this installed runtime's migration catalogue.
The caller holds source maintenance leases and only supplies disposable copies.
"""
from contextlib import closing
from pathlib import Path

from .migrations import STATE_MIGRATIONS, OBSERVABILITY_MIGRATIONS
from .models import StorageBlocked
from .runtime import StorageRuntime, open_migration_shadow


def migrate_shadow_database(path: Path, name: str):
    catalog = {"state": STATE_MIGRATIONS, "observability": OBSERVABILITY_MIGRATIONS}
    if name not in catalog or path.is_symlink() or path != path.resolve() or not path.is_file():
        raise StorageBlocked("UPDATE_DATA_PATH")
    with closing(open_migration_shadow(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        if any(row[0] != "ok" for row in db.execute("PRAGMA quick_check")):
            raise StorageBlocked("UPDATE_DATA_INTEGRITY")
        StorageRuntime._apply_migrations(db, catalog[name])
        if any(row[0] != "ok" for row in db.execute("PRAGMA quick_check")) or db.execute("PRAGMA foreign_key_check").fetchone():
            raise StorageBlocked("UPDATE_DATA_INTEGRITY")
        result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result[0]: raise StorageBlocked("UPDATE_DATA_BUSY")
