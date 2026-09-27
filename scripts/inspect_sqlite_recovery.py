"""Read-only table reachability probe for a damaged Gitgo SQLite copy.

This utility never opens the source in write mode.  It is intentionally small:
actual replacement/recovery remains an explicit operator action after backup.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    # A normal read-only connection includes committed WAL frames. immutable=1
    # would silently inspect a different, potentially obsolete database state.
    uri = args.database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rootpage"
        )
    ]
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        try:
            count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
            print(f"{table}\t{count}\tok")
        except sqlite3.DatabaseError as error:
            print(f"{table}\t-\t{type(error).__name__}: {error}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
