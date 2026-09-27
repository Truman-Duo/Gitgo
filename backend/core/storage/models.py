"""Value objects and policy defaults for the storage subsystem."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class StorageBlocked(RuntimeError):
    """Raised when an authoritative write cannot be performed safely."""


class StorageRuntimeUnsupported(StorageBlocked):
    """The loaded SQLite library has a known WAL corruption vulnerability."""

    code = "STORAGE_UNSAFE_SQLITE_VERSION"

    def __init__(self, version: str):
        self.version = version
        super().__init__(
            f"SQLite {version} has the known WAL-reset corruption risk. "
            "Use SQLite >= 3.51.3 (or patched 3.50.7 / 3.44.6 branches). "
            "Select a patched Gitgo Python runtime with GITGO_PYTHON; "
            "no database was opened or reset."
        )


class StorageCorruptionDetected(RuntimeError):
    """Raised before opening a structurally inconsistent SQLite file."""

    def __init__(self, database: str, reason: str):
        self.database = database
        self.reason = reason
        super().__init__(
            f"SQLite corruption detected at {database}: {reason}. "
            "The source was not modified; recover from a verified backup or an "
            "explicit salvage copy."
        )


class StorageReferenceMissing(StorageBlocked):
    """An authoritative CAS reference points to an object that is unavailable.

    This is deliberately distinct from SQLite corruption: the relational
    database can be structurally healthy while one of its immutable payloads is
    missing.  Callers may isolate a damaged historical projection, but must not
    silently invent continuation/governance state.
    """

    code = "STORAGE_CAS_REFERENCE_MISSING"

    def __init__(self, ref: str, path: str):
        self.ref = ref
        self.path = path
        super().__init__(
            f"CAS object {ref} is missing at {path}. "
            "The database was left unchanged; restore the object from a verified "
            "backup or continue from an unaffected checkpoint."
        )


class StorageHealthLevel(str, Enum):
    OK = "ok"
    WARNING = "warning"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class StoragePolicy:
    """Hard limits apply to the whole file family, not only the main DB file.

    Defaults are intentionally conservative.  Environment/config plumbing may
    tune these later, but callers cannot disable the global ceilings.
    """

    state_max_bytes: int = 1 * 1024**3
    observability_max_bytes: int = 512 * 1024**2
    cas_max_bytes: int = 2 * 1024**3
    total_max_bytes: int = 3 * 1024**3
    minimum_free_bytes: int = 1 * 1024**3
    warning_ratio: float = 0.70
    critical_ratio: float = 0.90
    recovery_ratio: float = 0.60
    observability_transactions_per_minute: int = 240
    observability_logical_bytes_per_minute: int = 16 * 1024**2
    sqlite_transactions_per_minute: int = 600
    sqlite_estimated_bytes_per_minute: int = 64 * 1024**2
    sqlite_write_amplification_reserve: int = 4
    max_observability_record_bytes: int = 64 * 1024
    max_cas_object_bytes: int = 64 * 1024**2
    observability_retention_days: int = 30
    observability_maintenance_interval_seconds: int = 6 * 60 * 60
    observability_retention_batch_rows: int = 10_000
    storage_metric_interval_seconds: int = 5 * 60
    cas_gc_interval_seconds: int = 6 * 60 * 60
    cas_gc_grace_days: int = 7
    cas_gc_batch_objects: int = 1000
    health_stderr_interval_seconds: float = 60.0
    cas_rescan_interval_seconds: float = 60.0
    integrity_check_interval_seconds: float = 5 * 60

    def __post_init__(self) -> None:
        positive = (
            self.state_max_bytes,
            self.observability_max_bytes,
            self.cas_max_bytes,
            self.total_max_bytes,
            self.minimum_free_bytes,
            self.observability_transactions_per_minute,
            self.observability_logical_bytes_per_minute,
            self.sqlite_transactions_per_minute,
            self.sqlite_estimated_bytes_per_minute,
            self.sqlite_write_amplification_reserve,
            self.max_observability_record_bytes,
            self.max_cas_object_bytes,
            self.observability_retention_days,
            self.observability_maintenance_interval_seconds,
            self.observability_retention_batch_rows,
            self.storage_metric_interval_seconds,
            self.cas_gc_interval_seconds,
            self.cas_gc_grace_days,
            self.cas_gc_batch_objects,
            self.cas_rescan_interval_seconds,
            self.integrity_check_interval_seconds,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("storage limits must be positive")
        if not 0 < self.recovery_ratio < self.warning_ratio < self.critical_ratio < 1:
            raise ValueError("storage health ratios must be ordered within (0, 1)")


@dataclass(frozen=True)
class StorageHealth:
    level: StorageHealthLevel
    checked_at: str
    project_id: str
    state_bytes: int
    observability_bytes: int
    cas_bytes: int
    total_bytes: int
    free_bytes: int
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["level"] = self.level.value
        result["reasons"] = list(self.reasons)
        return result
