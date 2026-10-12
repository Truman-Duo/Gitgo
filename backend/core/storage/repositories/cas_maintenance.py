"""Cas Maintenance repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
import json
import time
from pathlib import Path
from .ports import CasMaintenancePort

def _digest_from_ref(value: object) -> str | None:
    ref = str(value or "")
    if ref.startswith("sha256:"):
        digest = ref.removeprefix("sha256:")
    elif ref.startswith("trace-object:"):
        digest = ref.removeprefix("trace-object:")
    else:
        return None
    if len(digest) != 64 or any(
        char not in "0123456789abcdef" for char in digest
    ):
        return None
    return digest


def _referenced_cas_digests(self) -> set[str]:
    """Mark the transitive CAS closure used by current durable state.

    Older checkpoints embedded split-object refs inside ``metadata_ref``
    before ``object_refs`` was populated.  Reading those metadata objects is
    therefore part of marking, not an optional migration convenience.
    """
    direct, owned, nested = self._cas_reference_inventory()
    return direct | owned | nested


def _nested_cas_digests(value: object) -> set[str]:
    found: set[str] = set()
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
        else:
            digest = _digest_from_ref(current)
            if digest:
                found.add(digest)
    return found


def _cas_reference_inventory(self) -> tuple[set[str], set[str], set[str]]:
    """Return direct-column, explicit-owner and legacy nested references."""
    state_columns = (
        ("sessions", "metadata_ref"),
        ("tasks", "contract_ref"), ("tasks", "outcome_ref"),
        ("messages", "content_ref"), ("messages", "reasoning_ref"),
        ("tool_calls", "arguments_ref"), ("tool_calls", "result_ref"),
        ("receipts", "evidence_ref"),
        ("governance_signals", "payload_ref"),
        ("test_evidence", "payload_ref"),
        ("provider_attempts", "usage_ref"),
        ("mailbox_messages", "payload_ref"),
        ("worktrees", "metadata_ref"),
        ("dependency_nodes", "metadata_ref"),
        ("dependency_edges", "evidence_ref"),
        ("storage_kv", "value_ref"),
        ("session_events", "payload_ref"),
        ("session_processes", "runtime_state_ref"),
        ("runtime_outbox", "payload_ref"),
        ("command_receipts", "result_ref"),
        ("process_dependencies", "artifact_ref"),
        ("history_events", "record_ref"),
        ("lessons", "record_ref"),
        ("tool_result_objects", "content_ref"),
        ("session_lineage", "snapshot_ref"),
        ("custom_tool_versions", "spec_ref"),
        ("custom_tool_versions", "source_ref"),
    )
    observability_columns = (
        ("events", "payload_ref"),
        ("event_rollups", "sample_ref"),
        ("trace_events", "detail_ref"),
    )
    direct: set[str] = set()
    metadata_digests: set[str] = set()
    with self._state_lock:
        for table, column in state_columns:
            for row in self._state.execute(
                f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall():
                digest = self._digest_from_ref(row[0])
                if digest:
                    direct.add(digest)
                    if table == "sessions" and column == "metadata_ref":
                        metadata_digests.add(digest)
        owned = {
            str(row[0])
            for row in self._state.execute(
                "SELECT DISTINCT digest FROM object_refs"
            ).fetchall()
            if self._digest_from_ref(f"sha256:{row[0]}")
        }
    with self._observability_lock:
        for table, column in observability_columns:
            for row in self._observability.execute(
                f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall():
                digest = self._digest_from_ref(row[0])
                if digest:
                    direct.add(digest)
    nested: set[str] = set()
    for digest in metadata_digests:
        path = self.paths.cas_dir / digest[:2] / digest[2:]
        if not path.exists() and self._restore_canonical_blob(digest) is None:
            continue
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        nested.update(self._nested_cas_digests(metadata))
    return direct, owned, nested


def _refresh_cas_reference_status(self: CasMaintenancePort, *, force: bool = False) -> None:
    """Audit live references without scanning unrelated CAS objects."""
    now = time.monotonic()
    if (
        not force
        and now - self._last_cas_reference_check_at
        < self.policy.cas_rescan_interval_seconds
    ):
        return
    direct, owned, nested = self._cas_reference_inventory()
    reasons: dict[str, str] = {}
    # Later classes are stronger: a digest referenced by both nested
    # metadata and an authoritative column must remain a direct failure.
    for kind, digests in (("nested", nested), ("owned", owned), ("direct", direct)):
        for digest in digests:
            path = self.paths.cas_dir / digest[:2] / digest[2:]
            if path.exists() or self._restore_canonical_blob(digest) is not None:
                continue
            reasons[digest] = f"cas_{kind}_reference_missing:{digest}"
    with self._health_lock:
        self._cas_reference_reasons = reasons
        self._last_cas_reference_check_at = now


def maintain_cas(self: CasMaintenancePort, *, force: bool = False) -> dict:
    """Bounded mark-and-sweep with a grace period for crash orphans.

    CAS files are written before their referencing transaction.  The grace
    window guarantees a concurrent or recently crashed writer cannot have
    its object reclaimed.  No VACUUM and no per-write deletion is used.
    """
    self._ensure_open()
    now_monotonic = time.monotonic()
    if (
        not force
        and now_monotonic - self._last_cas_gc_at
        < self.policy.cas_gc_interval_seconds
    ):
        return {"ran": False, "deleted": 0}
    self._last_cas_gc_at = now_monotonic
    marked = self._referenced_cas_digests()
    cutoff = time.time() - self.policy.cas_gc_grace_days * 86400
    candidates: list[tuple[str, Path, int]] = []
    for prefix in self.paths.cas_dir.iterdir():
        if not prefix.is_dir() or len(prefix.name) != 2:
            continue
        for path in prefix.iterdir():
            digest = prefix.name + path.name
            if (
                len(digest) == 64
                and digest not in marked
                and path.is_file()
                and path.stat().st_mtime < cutoff
            ):
                candidates.append((digest, path, path.stat().st_size))
                if len(candidates) >= self.policy.cas_gc_batch_objects:
                    break
        if len(candidates) >= self.policy.cas_gc_batch_objects:
            break
    if not candidates:
        return {"ran": True, "deleted": 0}
    self._reserve_authoritative_write(max(4096, len(candidates) * 64))
    deleted: list[str] = []
    reclaimed = 0
    for digest, path, size in candidates:
        try:
            path.unlink()
            deleted.append(digest)
            reclaimed += size
        except FileNotFoundError:
            deleted.append(digest)
        except OSError:
            continue
    if deleted:
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                self._state.executemany(
                    "DELETE FROM object_refs WHERE digest=?",
                    [(digest,) for digest in deleted],
                )
                self._state.executemany(
                    "DELETE FROM objects WHERE digest=?",
                    [(digest,) for digest in deleted],
                )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        with self._health_lock:
            if self._cas_bytes_hint is not None:
                self._cas_bytes_hint = max(0, self._cas_bytes_hint - reclaimed)
    return {
        "ran": True, "deleted": len(deleted),
        "reclaimed_bytes": reclaimed,
    }
