"""Observation Retention repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from .ports import ObservationRetentionPort

def maintain_observability(self: ObservationRetentionPort, *, force: bool = False) -> dict:
    """Run bounded retention at startup or an explicit idle boundary.

    There is no per-event delete loop and no full VACUUM.  One transaction
    rolls up and removes at most ``observability_retention_batch_rows`` old
    rows, then records its durable cadence in the observability database.
    """
    self._ensure_open()
    now = datetime.now(timezone.utc)
    with self._observability_lock:
        row = self._observability.execute(
            """SELECT completed_at FROM observability_maintenance
               WHERE maintenance_key='retention'"""
        ).fetchone()
    if row is not None and not force:
        try:
            completed = datetime.fromisoformat(str(row[0]))
            if (now - completed).total_seconds() < (
                self.policy.observability_maintenance_interval_seconds
            ):
                return {"ran": False, "deleted": 0}
        except ValueError:
            pass

    cutoff = (now - timedelta(
        days=self.policy.observability_retention_days
    )).isoformat()
    batch = self.policy.observability_retention_batch_rows
    with self._observability_lock:
        has_expired = self._observability.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM events WHERE occurred_at<?
                   UNION ALL
                   SELECT 1 FROM trace_events WHERE occurred_at<?
                   UNION ALL
                   SELECT 1 FROM storage_metrics WHERE sampled_at<?
               )""",
            (cutoff, cutoff, cutoff),
        ).fetchone()
    if not bool(has_expired and has_expired[0]):
        # An empty/new database must not spend a write-budget token merely
        # to remember that there was nothing to delete.  Rechecking is a
        # read-only startup cost and cannot wear the disk.
        return {"ran": True, "deleted": 0, "cutoff": cutoff}
    logical = max(4096, batch * 64)
    if not self._reserve_observability_write(
        logical, publish_degraded=False,
    ):
        return {"ran": False, "deleted": 0, "throttled": True}
    with self._observability_lock:
        self._observability.execute("BEGIN IMMEDIATE")
        try:
            event_rows = self._observability.execute(
                """SELECT event_id, event_type, severity, occurred_at
                   FROM events WHERE occurred_at<? ORDER BY occurred_at LIMIT ?""",
                (cutoff, batch),
            ).fetchall()
            for event_type, severity, bucket, count, first_at, last_at in self._observability.execute(
                """SELECT event_type, severity, substr(occurred_at, 1, 10),
                          COUNT(*), MIN(occurred_at), MAX(occurred_at)
                   FROM events WHERE event_id IN (
                       SELECT event_id FROM events WHERE occurred_at<?
                       ORDER BY occurred_at LIMIT ?
                   ) GROUP BY event_type, severity, substr(occurred_at, 1, 10)""",
                (cutoff, batch),
            ).fetchall():
                bucket_start = str(bucket) + "T00:00:00+00:00"
                self._observability.execute(
                    """INSERT INTO event_rollups(
                           bucket_start, bucket_seconds, event_type, severity,
                           event_count, first_at, last_at, sample_ref
                       ) VALUES(?, 86400, ?, ?, ?, ?, ?, NULL)
                       ON CONFLICT(bucket_start, bucket_seconds, event_type, severity)
                       DO UPDATE SET
                           event_count=event_count+excluded.event_count,
                           first_at=MIN(first_at, excluded.first_at),
                           last_at=MAX(last_at, excluded.last_at)""",
                    (
                        bucket_start, str(event_type), str(severity),
                        int(count), str(first_at), str(last_at),
                    ),
                )
            if event_rows:
                self._observability.executemany(
                    "DELETE FROM events WHERE event_id=?",
                    [(str(item["event_id"]),) for item in event_rows],
                )
            remaining = max(0, batch - len(event_rows))
            trace_rows = self._observability.execute(
                """SELECT trace_id, sequence FROM trace_events
                   WHERE occurred_at<? ORDER BY occurred_at LIMIT ?""",
                (cutoff, remaining),
            ).fetchall() if remaining else []
            if trace_rows:
                self._observability.executemany(
                    "DELETE FROM trace_events WHERE trace_id=? AND sequence=?",
                    [(str(item["trace_id"]), int(item["sequence"])) for item in trace_rows],
                )
            metric_rows = self._observability.execute(
                """SELECT sampled_at FROM storage_metrics
                   WHERE sampled_at<? ORDER BY sampled_at LIMIT ?""",
                (cutoff, max(0, batch - len(event_rows) - len(trace_rows))),
            ).fetchall()
            if metric_rows:
                self._observability.executemany(
                    "DELETE FROM storage_metrics WHERE sampled_at=?",
                    [(str(item["sampled_at"]),) for item in metric_rows],
                )
            deleted = len(event_rows) + len(trace_rows) + len(metric_rows)
            self._observability.execute(
                """INSERT INTO observability_maintenance(
                       maintenance_key, completed_at, detail_json
                   ) VALUES('retention', ?, ?)
                   ON CONFLICT(maintenance_key) DO UPDATE SET
                       completed_at=excluded.completed_at,
                       detail_json=excluded.detail_json""",
                (
                    now.isoformat(),
                    json.dumps({"cutoff": cutoff, "deleted": deleted}),
                ),
            )
            self._observability.execute("COMMIT")
        except BaseException:
            self._observability.execute("ROLLBACK")
            raise
    return {"ran": True, "deleted": deleted, "cutoff": cutoff}
