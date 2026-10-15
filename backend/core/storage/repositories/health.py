"""Health repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from ..models import StorageHealth, StorageHealthLevel
from .ports import HealthPort
from ..support import _utc_now, _file_family_bytes, _directory_bytes, _quick_check

def _measure(self) -> tuple[int, int, int, int]:
    state = _file_family_bytes(self.paths.state_db)
    observability = _file_family_bytes(self.paths.observability_db)
    now = time.monotonic()
    if (
        self._cas_bytes_hint is None
        or now - self._last_cas_scan_at >= self.policy.cas_rescan_interval_seconds
    ):
        self._cas_bytes_hint = _directory_bytes(self.paths.cas_dir)
        self._last_cas_scan_at = now
    cas = self._cas_bytes_hint
    free = shutil.disk_usage(self.paths.project_root).free
    return state, observability, cas, free


def _health_from_measurement(self) -> StorageHealth:
    state, observability, cas, free = self._measure()
    total = state + observability + cas
    reasons: list[str] = []
    level = StorageHealthLevel.OK

    def raise_level(candidate: StorageHealthLevel) -> None:
        nonlocal level
        order = {
            StorageHealthLevel.OK: 0,
            StorageHealthLevel.WARNING: 1,
            StorageHealthLevel.DEGRADED: 2,
            StorageHealthLevel.BLOCKED: 3,
        }
        if order[candidate] > order[level]:
            level = candidate

    if free < self.policy.minimum_free_bytes:
        reasons.append("low_disk_space")
        raise_level(StorageHealthLevel.BLOCKED)
    for name, size, maximum in (
        ("state", state, self.policy.state_max_bytes),
        ("observability", observability, self.policy.observability_max_bytes),
        ("cas", cas, self.policy.cas_max_bytes),
        ("total", total, self.policy.total_max_bytes),
    ):
        ratio = size / maximum
        if ratio >= 1:
            reasons.append(f"{name}_capacity_exceeded")
            raise_level(
                StorageHealthLevel.BLOCKED
                if name in {"state", "total"}
                else StorageHealthLevel.DEGRADED
            )
        elif ratio >= self.policy.critical_ratio:
            reasons.append(f"{name}_capacity_critical")
            raise_level(StorageHealthLevel.DEGRADED)
        elif ratio >= self.policy.warning_ratio:
            reasons.append(f"{name}_capacity_warning")
            raise_level(StorageHealthLevel.WARNING)
    now_monotonic = time.monotonic()
    self._transient_degradations = {
        reason: expires_at
        for reason, expires_at in self._transient_degradations.items()
        if expires_at > now_monotonic
    }
    if self._transient_degradations:
        reasons.extend(sorted(self._transient_degradations))
        raise_level(StorageHealthLevel.DEGRADED)
    for database, reason in sorted(self._integrity_reasons.items()):
        reasons.append(f"{database}_integrity_failed:{reason}")
        raise_level(
            StorageHealthLevel.BLOCKED
            if database == "state" else StorageHealthLevel.DEGRADED
        )
    if self._state.quarantined:
        reasons.append("state_commit_unknown")
        raise_level(StorageHealthLevel.BLOCKED)
    for reason in sorted(self._cas_reference_reasons.values()):
        reasons.append(reason)
        raise_level(
            StorageHealthLevel.BLOCKED
            if reason.startswith(("cas_direct_", "cas_owned_"))
            else StorageHealthLevel.DEGRADED
        )
    return StorageHealth(
        level=level,
        checked_at=_utc_now(),
        project_id=self.paths.project_id,
        state_bytes=state,
        observability_bytes=observability,
        cas_bytes=cas,
        total_bytes=total,
        free_bytes=free,
        reasons=tuple(reasons),
    )


def check_health(self: HealthPort, *, force_publish: bool = False) -> StorageHealth:
    self._ensure_open()
    if not self._state.quarantined:
        self._refresh_integrity_status(force=force_publish)
        self._refresh_cas_reference_status(force=force_publish)
    with self._health_lock:
        health = self._health_from_measurement()
        previous = self._last_health
        if (
            previous is not None
            and previous.level != StorageHealthLevel.OK
            and health.level == StorageHealthLevel.OK
        ):
            maximum_ratio = max(
                health.state_bytes / self.policy.state_max_bytes,
                health.observability_bytes / self.policy.observability_max_bytes,
                health.cas_bytes / self.policy.cas_max_bytes,
                health.total_bytes / self.policy.total_max_bytes,
            )
            free_recovered = health.free_bytes >= int(
                self.policy.minimum_free_bytes * 1.10
            )
            if maximum_ratio >= self.policy.recovery_ratio or not free_recovered:
                health = StorageHealth(
                    level=StorageHealthLevel.WARNING,
                    checked_at=health.checked_at,
                    project_id=health.project_id,
                    state_bytes=health.state_bytes,
                    observability_bytes=health.observability_bytes,
                    cas_bytes=health.cas_bytes,
                    total_bytes=health.total_bytes,
                    free_bytes=health.free_bytes,
                    reasons=("storage_recovery_hysteresis",),
                )
        changed = previous is None or previous.level != health.level or previous.reasons != health.reasons
        self._last_health = health
        if force_publish or changed:
            self._write_health_snapshot(health)
            if self._health_listener is not None:
                self._health_listener(
                    {"event": "storage_health", "storage": health.to_dict()}
                )
            if health.level != StorageHealthLevel.OK:
                self._stderr_health(health)
        return health


def _refresh_integrity_status(self: HealthPort, *, force: bool = False) -> None:
    """Low-frequency corruption detection; never run on every write."""
    now = time.monotonic()
    if (
        not force
        and now - self._last_integrity_check_at
        < self.policy.integrity_check_interval_seconds
    ):
        return
    # Corruption is sticky for this connection lifetime; an unrelated read
    # or successful sample must not declare a known damaged database healthy.
    reasons: dict[str, str] = dict(self._integrity_reasons)
    with self._state_lock:
        reason = reasons.get("state") or _quick_check(self._state)
        if reason:
            reasons["state"] = reason
    with self._observability_lock:
        reason = reasons.get("observability") or _quick_check(self._observability)
        if reason:
            reasons["observability"] = reason
    self._integrity_reasons = reasons
    self._last_integrity_check_at = now


def _publish_degraded(self: HealthPort, reason: str) -> None:
    with self._health_lock:
        # Keep the signal visible for at least one complete limiter window.
        # A normal state write or Dashboard poll must not erase it
        # immediately after an observability write was rejected.
        self._transient_degradations[reason] = time.monotonic() + 60.0
        measured = self._health_from_measurement()
        health = StorageHealth(
            level=max(
                measured.level,
                StorageHealthLevel.DEGRADED,
                key=lambda item: list(StorageHealthLevel).index(item),
            ),
            checked_at=measured.checked_at,
            project_id=measured.project_id,
            state_bytes=measured.state_bytes,
            observability_bytes=measured.observability_bytes,
            cas_bytes=measured.cas_bytes,
            total_bytes=measured.total_bytes,
            free_bytes=measured.free_bytes,
            reasons=tuple(dict.fromkeys((*measured.reasons, reason))),
        )
        previous = self._last_health
        self._last_health = health
        if previous is None or previous.level != health.level or previous.reasons != health.reasons:
            self._write_health_snapshot(health)
            if self._health_listener is not None:
                self._health_listener(
                    {"event": "storage_health", "storage": health.to_dict()}
                )
            self._stderr_health(health)


def _write_health_snapshot(self: HealthPort, health: StorageHealth) -> None:
    temporary = self.paths.health_file.with_name(
        self.paths.health_file.name + f".{uuid.uuid4().hex}.tmp"
    )
    payload = json.dumps(
        health.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.paths.health_file)
    finally:
        temporary.unlink(missing_ok=True)


def _stderr_health(self: HealthPort, health: StorageHealth) -> None:
    now = time.monotonic()
    if now - self._last_stderr_at < self.policy.health_stderr_interval_seconds:
        return
    self._last_stderr_at = now
    print(
        "gitgo storage " + health.level.value + ": " + ", ".join(health.reasons),
        file=sys.stderr,
    )


def diagnostics(self) -> dict:
    self._ensure_open()
    health = self.check_health()
    with self._state_lock, self._observability_lock:
        state_pragmas = {
            name: self._state.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("journal_mode", "synchronous", "foreign_keys", "max_page_count")
        }
        observability_pragmas = {
            name: self._observability.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("journal_mode", "synchronous", "foreign_keys", "max_page_count")
        }
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "paths": {
            "project_root": str(self.paths.project_root),
            "state_db": str(self.paths.state_db),
            "observability_db": str(self.paths.observability_db),
            "health_file": str(self.paths.health_file),
        },
        "health": health.to_dict(),
        "state_pragmas": state_pragmas,
        "observability_pragmas": observability_pragmas,
    }
