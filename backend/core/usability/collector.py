"""Independent worker: read committed traces; write bounded derived statistics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
import logging
from pathlib import Path
import shutil
import threading
import time
from typing import Callable

from .projection import EVENTS, project
from backend.core.storage.runtime import open_usability_source, open_usability_statistics

LOG = logging.getLogger(__name__)
SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UsabilityCollector:
    """No access to model, permissions, authoritative writes, or UI rendering.

    Only the worker owns its SQLite handles. Cursor and aggregate updates commit
    together; source row identities detect pruning/VACUUM/replacement. Replays
    deduplicate by trace identity, never by text or wall-clock order.
    """
    def __init__(self, project_root: Path, *, interval: float = 5, batch_size: int = 64,
                 on_warning: Callable[[dict], None] | None = None):
        self.root = Path(project_root)
        self.directory = self.root / "usability"
        self.interval = max(.05, interval)
        self.batch_size = max(1, min(128, batch_size))
        self.on_warning = on_warning
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_health_write = 0.0
        self._last_warning = 0.0
        self._last_retention = 0.0
        self._health = {"schema_version": SCHEMA_VERSION, "state": "starting",
                        "coverage": "persisted_trace_only", "upstream_losses": "unknown",
                        "database": str(self.directory / "metrics.sqlite3"),
                        "incomplete_samples": 0, "last_error_code": None}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="gitgo-usability")
        try:
            self._thread.start()
        except Exception:
            self._thread = None
            self._update(state="degraded", last_error_code="worker_start_failed")
            raise

    def status(self) -> dict:
        with self._lock:
            return dict(self._health)

    def _update(self, **values) -> None:
        with self._lock:
            self._health.update(values)

    def _warning(self, code: str) -> None:
        self._update(state="degraded", last_error_code=code, last_error_at=utc_now())
        # No exception text: it can contain source prompt fragments or credentials.
        if time.monotonic() - self._last_warning >= 60 or not self._last_warning:
            self._last_warning = time.monotonic()
            LOG.warning("USABILITY_COLLECTION_DEGRADED: %s", code)
            if self.on_warning:
                try:
                    self.on_warning({"event": "error", "code": "USABILITY_COLLECTION_DEGRADED",
                        "message": "Background usage statistics are incomplete; task execution is unaffected.",
                        "diagnostic_code": code})
                except Exception:
                    LOG.warning("Usability warning delivery failed; retained in backend health")

    def _save_health(self, *, force=False) -> None:
        if not force and time.monotonic() - self._last_health_write < 60:
            return
        temporary = self.directory / f"health-{threading.get_ident()}.tmp"
        try:
            temporary.write_text(json.dumps(self.status(), indent=2), encoding="utf-8")
            temporary.replace(self.directory / "health.json")
            self._last_health_write = time.monotonic()
        except OSError:
            self._warning("health_write_failed")
        finally:
            temporary.unlink(missing_ok=True)

    def _open(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        source = open_usability_source(self.root)
        database = None
        try:
            database = open_usability_statistics(self.root)
            version = database.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                database.close()
                raise ValueError("unsupported_statistics_schema")
            database.execute("PRAGMA journal_mode=DELETE")
            page_size = database.execute("PRAGMA page_size").fetchone()[0]
            database.execute(f"PRAGMA max_page_count={64 * 1024**2 // page_size}")
            database.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    trace_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    dimensions TEXT NOT NULL, metrics TEXT NOT NULL, detail_status TEXT NOT NULL,
                    PRIMARY KEY(trace_id, sequence));
                CREATE INDEX IF NOT EXISTS observations_time ON observations(occurred_at);
                CREATE TABLE IF NOT EXISTS daily (
                    day TEXT NOT NULL, event_type TEXT NOT NULL, dimensions TEXT NOT NULL,
                    metric TEXT NOT NULL, n INTEGER NOT NULL, total REAL NOT NULL,
                    minimum REAL NOT NULL, maximum REAL NOT NULL,
                    PRIMARY KEY(day,event_type,dimensions,metric));
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            database.commit()
            return source, database
        except BaseException:
            source.close()
            if database is not None:
                database.close()
            raise

    def _poll(self, source, database) -> int:
        if shutil.disk_usage(self.directory).free < 128 * 1024**2:
            raise OSError("statistics_low_disk")
        saved = database.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()
        cursor = json.loads(saved[0]) if saved else [0, "", 0]
        if cursor[0]:
            anchor = source.execute("SELECT trace_id,sequence FROM trace_events WHERE rowid=?", (cursor[0],)).fetchone()
            if anchor is None or list(anchor) != cursor[1:]:
                cursor = [0, "", 0]
                self._update(source_rescans=self.status().get("source_rescans", 0) + 1)
        placeholders = ",".join("?" for _ in EVENTS)
        rows = source.execute(f"""SELECT rowid,trace_id,sequence,record_json,occurred_at
            FROM trace_events WHERE rowid>? AND event_type IN ({placeholders}) ORDER BY rowid LIMIT ?""",
            (cursor[0], *EVENTS, self.batch_size)).fetchall()
        if not rows:
            self._update(last_poll_at=utc_now(), backlog_pending=False)
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        prepared = []
        deadline = time.monotonic() + .1
        for rowid, trace_id, sequence, raw, occurred_at in rows:
            cursor = [rowid, trace_id, sequence]
            if occurred_at >= cutoff and not database.execute(
                    "SELECT 1 FROM observations WHERE trace_id=? AND sequence=?", (trace_id, sequence)).fetchone():
                try:
                    record = json.loads(raw)
                    dimensions, metrics, detail_status = project(record, self.root / "cas")
                except (ValueError, TypeError, KeyError, AttributeError):
                    dimensions, metrics, detail_status = {}, {}, "invalid_record"
                    record = {}
                event_type = record.get("event") if record.get("event") in EVENTS else "invalid_record"
                # Provider latency is measurable only with a matching persisted start.
                if event_type in {"provider_response_completed", "provider_response_incomplete"}:
                    before = next((m.get("monotonic_ns") for tid, seq, e, at, d, m, s in reversed(prepared)
                        if tid == trace_id and e == "provider_request_started" and d.get("process_id") == dimensions.get("process_id")), None)
                    previous = database.execute("""SELECT metrics FROM observations
                        WHERE trace_id=? AND event_type='provider_request_started' AND sequence<?
                        AND json_extract(dimensions,'$.process_id') IS ?
                        ORDER BY sequence DESC LIMIT 1""", (trace_id, sequence, dimensions.get("process_id"))).fetchone()
                    if before is None and previous:
                        before = json.loads(previous[0]).get("monotonic_ns")
                    after = metrics.get("monotonic_ns")
                    if before is not None and after is not None and after >= before:
                        metrics["provider_elapsed_ms"] = (after - before) / 1e6
                prepared.append((trace_id, sequence, event_type, occurred_at, dimensions, metrics, detail_status))
            if time.monotonic() >= deadline:
                break
        with database:
            for trace_id, sequence, event_type, occurred_at, dimensions, metrics, detail_status in prepared:
                encoded_dimensions = json.dumps(dimensions, sort_keys=True, separators=(",", ":"))
                inserted = database.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?)",
                    (trace_id, sequence, event_type, occurred_at, encoded_dimensions,
                     json.dumps(metrics, sort_keys=True), detail_status)).rowcount
                if not inserted:
                    continue
                # Keep process IDs for trace correlation, not as daily cohort labels.
                cohort = json.dumps({k: v for k, v in dimensions.items() if k != "process_id"}, sort_keys=True, separators=(",", ":"))
                rollup = {"events": 1, **{k: v for k, v in metrics.items() if k not in {"monotonic_ns", "step"}}}
                if detail_status not in {"ok", "not_needed"}:
                    rollup["incomplete_samples"] = 1
                for metric, value in rollup.items():
                    database.execute("""INSERT INTO daily VALUES(?,?,?,?,1,?,?,?)
                        ON CONFLICT(day,event_type,dimensions,metric) DO UPDATE SET
                        n=n+1,total=total+excluded.total,minimum=min(minimum,excluded.minimum),maximum=max(maximum,excluded.maximum)""",
                        (occurred_at[:10], event_type, cohort, metric, value, value, value))
            database.execute("INSERT OR REPLACE INTO metadata VALUES('cursor',?)", (json.dumps(cursor),))
        incomplete = database.execute("SELECT coalesce(sum(total),0) FROM daily WHERE metric='incomplete_samples'").fetchone()[0]
        self._update(state="degraded" if incomplete else "running", incomplete_samples=int(incomplete),
                     last_poll_at=utc_now(), last_sample_at=occurred_at, last_error_code=None,
                     backlog_pending=len(rows) == self.batch_size or cursor[0] != rows[-1][0])
        if incomplete:
            self._warning("incomplete_trace_detail")
        return len(rows)

    def _retain(self, database) -> None:
        now = datetime.now(timezone.utc)
        with database:
            database.execute("DELETE FROM observations WHERE occurred_at<?", ((now - timedelta(days=90)).isoformat(),))
            database.execute("DELETE FROM daily WHERE day<?", ((now - timedelta(days=730)).date().isoformat(),))

    def _run(self) -> None:
        source = database = None
        try:
            while not self._stop.is_set():
                try:
                    if source is None:
                        source, database = self._open()
                        incomplete = database.execute("SELECT coalesce(sum(total),0) FROM daily WHERE metric='incomplete_samples'").fetchone()[0]
                        self._update(state="degraded" if incomplete else "running", incomplete_samples=int(incomplete), last_error_code=None)
                    if time.monotonic() - self._last_retention >= 3600 or not self._last_retention:
                        self._retain(database)
                        self._last_retention = time.monotonic()
                    # Bounded catch-up. A busy project cannot occupy the worker indefinitely.
                    cycle_deadline = time.monotonic() + .25
                    for _ in range(8):
                        if self._poll(source, database) < self.batch_size or self._stop.is_set() or time.monotonic() >= cycle_deadline:
                            break
                    self._save_health()
                except Exception as error:
                    self._warning(getattr(error, "sqlite_errorname", type(error).__name__))
                    self._save_health()
                    for connection in (source, database):
                        if connection is not None:
                            connection.close()
                    source = database = None
                self._stop.wait(self.interval)
            if source is not None:
                for _ in range(8):
                    if self._poll(source, database) < self.batch_size:
                        break
        except Exception as error:
            self._warning(type(error).__name__)
        finally:
            for connection in (source, database):
                if connection is not None:
                    connection.close()
            self._update(state="stopped", stopped_at=utc_now())
            self._save_health(force=True)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                self._warning("collector_shutdown_timeout")


def read_summary(project_root: Path, *, days: int = 30) -> dict:
    """Read-only offline access: no daemon/provider start, no database creation."""
    directory = Path(project_root) / "usability"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, min(730, days)))).date().isoformat()
    with closing(open_usability_statistics(project_root, read_only=True)) as database:
        rows = database.execute("SELECT day,event_type,dimensions,metric,n,total,minimum,maximum FROM daily WHERE day>=? ORDER BY day,event_type,dimensions,metric", (cutoff,)).fetchall()
    return {"schema_version": SCHEMA_VERSION, "coverage": "persisted_trace_only",
            "health": json.loads((directory / "health.json").read_text(encoding="utf-8")),
            "daily": [{"day": d, "event": e, "dimensions": json.loads(c), "metric": m,
                       "known_samples": n, "total": total, "minimum": low, "maximum": high}
                      for d, e, c, m, n, total, low, high in rows]}
