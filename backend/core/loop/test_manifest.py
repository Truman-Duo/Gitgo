"""Registered, reproducible test evidence for completion hard gates."""

from __future__ import annotations

import json
import errno
import os
import subprocess
import sys
import tempfile
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


MANIFEST_PATH = ".gitgo/test_manifest.json"


_MANIFEST_LOCKS_GUARD = threading.Lock()
_MANIFEST_LOCKS: dict[str, threading.RLock] = {}


def _manifest_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _MANIFEST_LOCKS_GUARD:
        return _MANIFEST_LOCKS.setdefault(key, threading.RLock())


def _replace_with_windows_retry(source: str, destination: Path) -> None:
    """Commit an atomic JSON snapshot despite transient Windows sharing locks.

    Defender, indexers, and a concurrent reader may briefly open the existing
    destination without delete sharing.  A bounded retry preserves the atomic
    replacement contract; it never falls back to truncating the live manifest.
    """
    delays = (0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.0, 1.0, 1.0, 1.0)
    for attempt in range(len(delays) + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            retryable = (
                isinstance(exc, PermissionError)
                or getattr(exc, "winerror", None) in {5, 32, 33}
                or exc.errno in {errno.EACCES, errno.EPERM}
            )
            if not retryable or attempt >= len(delays):
                raise
            time.sleep(delays[attempt])


_DECLARED_TEST_ID_PATTERNS = (
    re.compile(
        r"(?i)\btest[_ -]?id\s*(?:(?:=|:)\s*|\s+)"
        r"[\"']?([A-Za-z0-9_.-]+:[A-Za-z0-9_.:/-]+)"
    ),
    re.compile(
        r"(?i)\brequired_test_ids?\s*(?:=|:)\s*"
        r"[\"']?([A-Za-z0-9_.-]+:[A-Za-z0-9_.:/-]+)"
    ),
)


def extract_declared_test_ids(text: str) -> list[str]:
    """Extract only explicitly labelled test ids from task text.

    This is a deterministic admission shortcut, not semantic guessing.  It
    lets a UI or user state ``test_id foo:bar`` without forcing an A Agent to
    copy the identifier into every child contract.
    """
    found: list[str] = []
    for pattern in _DECLARED_TEST_ID_PATTERNS:
        for match in pattern.finditer(str(text or "")):
            value = match.group(1).rstrip(".,;)]}")
            if value and value not in found:
                found.append(value)
    return found


@dataclass(frozen=True)
class SeedResult:
    seed: int
    exit_code: int
    duration_ms: float
    output_tail: str

    def to_dict(self) -> dict:
        return {
            "seed": self.seed,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output_tail": self.output_tail,
        }


@dataclass
class TestRecord:
    __test__ = False
    test_id: str
    target: str
    seeds: list[int] = field(default_factory=list)
    results: list[SeedResult] = field(default_factory=list)
    last_run_at: str = ""

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.exit_code == 0 for r in self.results)

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "target": self.target,
            "seeds": list(self.seeds),
            "results": [r.to_dict() for r in self.results],
            "passed": self.passed,
            "last_run_at": self.last_run_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "TestRecord":
        return cls(
            test_id=str(raw.get("test_id", "")),
            target=str(raw.get("target", "")),
            seeds=[int(s) for s in raw.get("seeds", [])],
            results=[SeedResult(
                seed=int(item.get("seed", 0)),
                exit_code=int(item.get("exit_code", -1)),
                duration_ms=float(item.get("duration_ms", 0)),
                output_tail=str(item.get("output_tail", "")),
            ) for item in raw.get("results", [])],
            last_run_at=str(raw.get("last_run_at", "")),
        )


class TestManifest:
    __test__ = False
    SCHEMA_VERSION = 1

    def __init__(self, workspace_path: str | Path):
        self.workspace = Path(workspace_path).resolve()
        self.path = self.workspace / MANIFEST_PATH
        self.records: dict[str, TestRecord] = {}

    @classmethod
    def load(cls, workspace_path: str | Path) -> "TestManifest":
        manifest = cls(workspace_path)
        if not manifest.path.exists():
            return manifest
        try:
            raw = json.loads(manifest.path.read_text(encoding="utf-8"))
            for item in raw.get("tests", []):
                record = TestRecord.from_dict(item)
                if record.test_id:
                    manifest.records[record.test_id] = record
        except (OSError, json.JSONDecodeError, ValueError):
            return cls(workspace_path)
        return manifest

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "updated_at": datetime.now().isoformat(),
            "tests": [self.records[key].to_dict() for key in sorted(self.records)],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix="test_manifest.", suffix=".tmp", dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_windows_retry(tmp_name, self.path)
        finally:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass

    def save(self) -> None:
        with _manifest_lock(self.path):
            self._save_unlocked()

    def register(self, record: TestRecord) -> None:
        self.register_many([record])

    def register_many(self, records: list[TestRecord]) -> None:
        """Merge records and persist one atomic snapshot.

        Reloading inside the path-scoped critical section prevents two Agent
        lifecycle operations in this Host from overwriting each other's seed
        evidence with stale in-memory manifests.
        """
        if not records:
            return
        for record in records:
            if not record.test_id.strip():
                raise ValueError("test_id is required")
        with _manifest_lock(self.path):
            latest = type(self).load(self.workspace)
            self.records = latest.records
            for record in records:
                self._merge_record(record)
            self._save_unlocked()

    def _merge_record(self, record: TestRecord) -> None:
        if not record.test_id.strip():
            raise ValueError("test_id is required")
        previous = self.records.get(record.test_id)
        if previous is not None and previous.target == record.target:
            # Separate invocations for different deterministic seeds must not
            # erase earlier evidence.  Keep the latest result for each seed,
            # preserving first-seen seed order for stable, reviewable output.
            by_seed = {item.seed: item for item in previous.results}
            by_seed.update({item.seed: item for item in record.results})
            merged_seeds = list(dict.fromkeys(previous.seeds + record.seeds))
            record = TestRecord(
                test_id=record.test_id,
                target=record.target,
                seeds=merged_seeds,
                results=[by_seed[seed] for seed in merged_seeds if seed in by_seed],
                last_run_at=record.last_run_at,
            )
        self.records[record.test_id] = record

    def evaluate_required(self, required_ids: list[str]) -> tuple[bool, list[str]]:
        failures = []
        for test_id in required_ids:
            record = self.records.get(test_id)
            if record is None:
                failures.append(f"{test_id}:not_registered")
            elif not record.passed:
                failures.append(f"{test_id}:not_passing")
        return not failures, failures


def run_registered_test(workspace_path: str | Path, args: dict) -> dict:
    """Run one pytest target under one or more deterministic seeds."""
    workspace = Path(workspace_path).resolve()
    test_id = str(args.get("test_id", "")).strip()
    target = str(args.get("target", "")).strip()
    seeds = [int(seed) for seed in (args.get("seeds", [42]) or [42])]
    timeout = max(1, min(int(args.get("timeout", 120)), 900))
    if not test_id:
        return {"error": "test_id is required"}
    if not target or target.startswith("-"):
        return {"error": "target must be a pytest file/node target"}
    target_path_text = target.split("::", 1)[0]
    target_path = (workspace / target_path_text).resolve()
    try:
        target_path.relative_to(workspace)
    except ValueError:
        return {"error": "test target escapes workspace"}
    if not target_path.exists() or not target_path.is_file():
        return {"error": f"test target not found: {target_path_text}"}

    import time
    results: list[SeedResult] = []
    for seed in seeds:
        env = dict(os.environ)
        env["GITGO_TEST_SEED"] = str(seed)
        started = time.monotonic()
        try:
            from backend.core.child_process import python_command
            completed = subprocess.run(
                python_command(["-m", "pytest", target, "-q"]),
                cwd=str(workspace), env=env, capture_output=True, text=True,
                timeout=timeout, shell=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            output = ((exc.stdout or "") + (exc.stderr or ""))
            exit_code = 124
        results.append(SeedResult(
            seed=seed,
            exit_code=exit_code,
            duration_ms=(time.monotonic() - started) * 1000,
            output_tail=output[-4000:],
        ))

    record = TestRecord(
        test_id=test_id,
        target=target,
        seeds=seeds,
        results=results,
        last_run_at=datetime.now().isoformat(),
    )
    manifest = TestManifest.load(workspace)
    manifest.register(record)
    return {
        "test_id": test_id,
        "target": target,
        "passed": record.passed,
        "exit_code": 0 if record.passed else 1,
        "seed_results": [result.to_dict() for result in results],
        "manifest_path": str(manifest.path),
    }
