from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from backend.core.history import HistoryManager
from backend.core.knowledge.harvest import get_unprocessed_signals
from backend.core.knowledge.manager import LessonManager
from backend.core.knowledge.models import Lesson
from backend.core.storage import StoragePolicy, StorageRuntime


def test_legacy_knowledge_import_is_idempotent_and_becomes_single_authority(
    tmp_path_factory,
):
    workspace = tmp_path_factory
    instance = workspace / ".gitgo" / "knowledge" / "instances" / "demo"
    instance.mkdir(parents=True)
    lesson = Lesson(
        id="legacy-lesson",
        project_name="demo",
        trigger="auth.py",
        rule="when auth changes, must run the complete authentication test suite",
        created_at="2026-01-01T00:00:00",
    )
    (instance / "pending.jsonl").write_text(
        json.dumps(lesson.to_dict()) + "\n", encoding="utf-8",
    )

    with StorageRuntime(
        workspace, state_home=workspace / "state",
    ) as storage:
        LessonManager.bind_storage(workspace, storage)
        assert [item.id for item in LessonManager.load_pending(workspace, "demo")] == [
            "legacy-lesson",
        ]
        verified = LessonManager.verify(workspace, "legacy-lesson", "demo")
        assert verified is not None
        assert storage.lesson_count(scope="pending", project_name="demo") == 0
        assert storage.lesson_count(scope="instance", project_name="demo") == 1
        assert not (workspace / ".gitgo" / "knowledge").exists()
        assert list((workspace / ".gitgo").glob("knowledge.legacy-imported-*"))


def test_legacy_harvest_signal_uses_governance_signal_table_without_dual_write(
    tmp_path_factory,
):
    workspace = tmp_path_factory
    legacy = workspace / ".gitgo" / "harvest_signals.json"
    legacy.parent.mkdir(parents=True)
    record = {
        "signal_id": "hs_legacy_signal",
        "project_name": "demo",
        "signal_type": "rejection",
        "detail": {"reason": "missing test"},
        "source_event_id": "event-1",
        "state": "pending",
        "retry_count": 0,
        "lease_id": "",
        "lease_until": 0.0,
        "lesson_ids": [],
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    legacy.write_text(json.dumps({
        "version": 1, "records": {record["signal_id"]: record},
    }), encoding="utf-8")

    with StorageRuntime(
        workspace, state_home=workspace / "state",
    ) as storage:
        HistoryManager.set_workspace(str(workspace), storage=storage)
        pending = get_unprocessed_signals("demo")
        assert [item["signal_id"] for item in pending] == ["hs_legacy_signal"]
        assert storage.harvest_signal_count() == 1
        assert not legacy.exists()
        assert list(legacy.parent.glob("harvest_signals.json.legacy-imported-*"))


def test_metrics_are_low_cadence_and_retention_is_one_bounded_batch(
    tmp_path_factory,
):
    workspace = tmp_path_factory
    policy = StoragePolicy(
        observability_retention_days=1,
        storage_metric_interval_seconds=3600,
    )
    with StorageRuntime(
        workspace, state_home=workspace / "state", policy=policy,
    ) as storage:
        # Startup does not spend a telemetry write merely to record startup.
        assert storage.observability_counts()["storage_metrics"] == 0
        assert storage.record_storage_metric(force=True)
        assert not storage.record_storage_metric()
        assert storage.observability_counts()["storage_metrics"] == 1

        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        assert storage.append_event(
            event_type="old.event", summary="expired", occurred_at=old,
        )
        result = storage.maintain_observability(force=True)
        assert result["ran"] is True
        assert result["deleted"] == 1
        counts = storage.observability_counts()
        assert counts["events"] == 0
        assert counts["event_rollups"] == 1


def test_cas_mark_and_sweep_keeps_references_and_bounds_crash_orphans(
    tmp_path_factory,
):
    workspace = tmp_path_factory
    policy = StoragePolicy(cas_gc_grace_days=1, cas_gc_batch_objects=10)
    with StorageRuntime(
        workspace, state_home=workspace / "state", policy=policy,
    ) as storage:
        retained_ref = storage.put_blob(b"retained")
        storage.put_state_ref("test", "retained", retained_ref)
        orphan = storage._write_blob_file(b"orphan")
        orphan_path = (
            storage.paths.cas_dir / orphan["digest"][:2] / orphan["digest"][2:]
        )
        old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
        os.utime(orphan_path, (old, old))

        result = storage.maintain_cas(force=True)
        assert result["deleted"] == 1
        assert not orphan_path.exists()
        assert storage.read_blob(retained_ref) == b"retained"


def test_trace_detail_cannot_bypass_observability_byte_budget(tmp_path_factory):
    workspace = tmp_path_factory
    policy = StoragePolicy(
        observability_logical_bytes_per_minute=1024,
        sqlite_estimated_bytes_per_minute=1024 * 1024,
    )
    with StorageRuntime(
        workspace, state_home=workspace / "state", policy=policy,
    ) as storage:
        result = storage.append_trace_record(
            "bounded-detail",
            {"seq": 1, "event": "provider_request"},
            detail={"payload": "x" * 4096},
        )
        assert result is None
        assert storage.observability_counts()["trace_events"] == 0
        assert not any(storage.paths.cas_dir.rglob("*"))


def test_legacy_trace_import_resumes_without_rewriting_committed_prefix(
    tmp_path_factory,
):
    workspace = tmp_path_factory
    policy = StoragePolicy(
        observability_logical_bytes_per_minute=12 * 1024,
        sqlite_estimated_bytes_per_minute=1024 * 1024,
    )
    records = [
        (
            {"seq": sequence, "event": "reasoning_delta", "delta": "x" * 900},
            None,
        )
        for sequence in range(1, 25)
    ]
    with StorageRuntime(
        workspace, state_home=workspace / "state", policy=policy,
    ) as storage:
        assert storage.import_trace_records("legacy", records) is None
        first_count = storage.observability_counts()["trace_events"]
        assert 0 < first_count < len(records)

    # Fresh runtimes represent later bounded migration passes.  Existing
    # sequence keys are filtered before reservation, so each pass writes only
    # the missing suffix even when more than two minute envelopes are needed.
    completed = False
    for _pass in range(4):
        with StorageRuntime(
            workspace, state_home=workspace / "state", policy=policy,
        ) as storage:
            completed = storage.import_trace_records("legacy", records) is not None
            if completed:
                assert storage.observability_counts()["trace_events"] == len(records)
                replay = storage.read_trace_events("legacy", limit=100)
                assert [item["seq"] for item in replay["events"]] == list(
                    range(1, 25)
                )
                break
    assert completed
