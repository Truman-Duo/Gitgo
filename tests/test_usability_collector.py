from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import queue
from types import SimpleNamespace
from contextlib import closing

from backend.core.storage import StorageRuntime
from backend.core.usability import UsabilityCollector
from backend.core.usability.collector import read_summary
from backend.core.usability.projection import DetailReader, DetailUnavailable
from backend.core.usability.projection import project
from backend.core.loop.provider_protocol import normalize_usage
import pytest


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Collector did not reach the expected state")
        time.sleep(.02)


def observed(root):
    with closing(sqlite3.connect(root / "usability" / "metrics.sqlite3")) as db:
        return db.execute("SELECT event_type,metrics,detail_status FROM observations ORDER BY sequence").fetchall()


def test_background_collection_delta_restart_and_no_copied_text(tmp_path_factory):
    (tmp_path_factory / "workspace").mkdir()
    with StorageRuntime(tmp_path_factory / "workspace") as storage:
        root = storage.paths.project_root
        snapshot = {"messages": [{"role": "system", "content": "private-instructions"},
                    {"role": "user", "message_type": "conversation", "content": "private-user-message"},
                    {"role": "user", "host_authority": True, "content": "[HOST CURRENT-TURN ENVELOPE]\npolicy"}],
                    "tools": [{"name": "private-tool-description"}]}
        first = storage.append_trace_record("trace", {"event": "provider_request_started", "process_id": "p", "monotonic_ns": 1_000_000}, detail=snapshot)
        storage.append_trace_record("trace", {"event": "provider_response_completed", "process_id": "p", "monotonic_ns": 6_000_000})
        storage.append_trace_record("trace", {"event": "provider_request_started", "process_id": "p"}, detail={
            "snapshot_mode": "prefix_delta", "base_detail_ref": first["detail_ref"],
            "common_prefix_messages": 2, "appended_messages": [{"role": "user", "content": "next"}],
            "tools_reused": True, "tools": []})
        storage.append_trace_record("trace", {"event": "provider_usage", "usage": {"input_tokens": 12, "output_tokens": 0}})
        storage.append_trace_record("trace", {"event": "completion_gate", "accepted": False})
        storage.append_trace_record("trace", {"event": "decision_required", "decision": {"kind": "permission", "question": "private-question"}})
        storage.append_trace_record("trace", {"event": "agent_complete", "outcome": {"status": "completed", "response": "private-answer", "duration_ms": 20}})
        source_before = storage.read_trace_events("trace", limit=50)
        collector = UsabilityCollector(root, interval=.05)
        collector.start()
        try:
            wait_for(lambda: (root / "usability" / "health.json").exists() and len(observed(root)) == 7)
        finally:
            collector.stop()
        rows = observed(root)
        assert json.loads(rows[1][1])["provider_elapsed_ms"] == 5
        assert json.loads(rows[2][1])["latest_user_characters"] == 4
        assert json.loads(rows[3][1])["output_tokens"] == 0
        assert "reasoning_tokens" not in json.loads(rows[3][1])
        assert storage.read_trace_events("trace", limit=50) == source_before
        restart = UsabilityCollector(root, interval=.05)
        restart.start()
        try:
            wait_for(lambda: restart.status().get("last_poll_at"))
        finally:
            restart.stop()
        assert len(observed(root)) == 7
        summary = read_summary(root)
        assert sum(r["total"] for r in summary["daily"] if r["metric"] == "events") == 7
        serialized = json.dumps(summary) + str(observed(root))
        assert "private-" not in serialized


def test_missing_detail_is_incomplete_and_later_events_continue(tmp_path_factory):
    (tmp_path_factory / "workspace").mkdir()
    with StorageRuntime(tmp_path_factory / "workspace") as storage:
        root = storage.paths.project_root
        storage.append_trace_record("trace", {"event": "provider_request_started", "detail_ref": "trace-object:" + "a" * 64})
        storage.append_trace_record("trace", {"event": "tool_result", "tool_name": "read_file", "is_error": True})
        warnings = []
        collector = UsabilityCollector(root, interval=.05, on_warning=warnings.append)
        collector.start()
        try:
            wait_for(lambda: (root / "usability" / "health.json").exists() and len(observed(root)) == 2)
        finally:
            collector.stop()
        assert observed(root)[0][2] == "missing_detail"
        assert "system_characters" not in json.loads(observed(root)[0][1])
        assert warnings[0]["code"] == "USABILITY_COLLECTION_DEGRADED"
        assert read_summary(root)["health"]["incomplete_samples"] == 1


def test_pruned_source_rowids_reused_without_loss_or_double_count(tmp_path_factory):
    (tmp_path_factory / "workspace").mkdir()
    with StorageRuntime(tmp_path_factory / "workspace") as storage:
        root = storage.paths.project_root
        storage.append_trace_record("old", {"event": "tool_result", "is_error": False})
        collector = UsabilityCollector(root)
        source, database = collector._open()
        try:
            assert collector._poll(source, database) == 1
            # Same condition as trace retention/VACUUM replacing a cursor anchor.
            with storage._observability:
                storage._observability.execute("DELETE FROM trace_events")
            storage.append_trace_record("new", {"event": "tool_result", "is_error": True})
            assert collector._poll(source, database) == 1
            assert collector._poll(source, database) == 0
            assert database.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
            assert database.execute("SELECT sum(total) FROM daily WHERE metric='events'").fetchone()[0] == 2
        finally:
            source.close()
            database.close()


def test_bad_detail_reference_never_reads_outside_cas(tmp_path_factory):
    with pytest.raises(DetailUnavailable, match="invalid_reference"):
        DetailReader(tmp_path_factory).read("../../outside")
    data = b'{"messages":[],"tools":[]}'
    digest = hashlib.sha256(data).hexdigest()
    path = tmp_path_factory / digest[:2] / digest[2:]
    path.parent.mkdir()
    path.write_bytes(b"corrupted")
    with pytest.raises(DetailUnavailable, match="detail_hash_mismatch"):
        DetailReader(tmp_path_factory).snapshot("trace-object:" + digest)


def test_provider_normalization_does_not_turn_unknown_usage_into_zero(tmp_path_factory):
    _, metrics, _ = project({"event": "provider_usage", "usage": normalize_usage({
        "prompt_tokens": 12, "completion_tokens": 0,
    }).to_dict()}, tmp_path_factory)
    assert metrics == {"input_tokens": 12, "output_tokens": 0, "total_tokens": 12}


def test_worker_retries_missing_source_without_creating_authoritative_database(tmp_path_factory):
    warnings = []
    collector = UsabilityCollector(tmp_path_factory, interval=.05, on_warning=warnings.append)
    collector.start()
    try:
        wait_for(lambda: warnings)
        assert not (tmp_path_factory / "observability.sqlite3").exists()
        assert not (tmp_path_factory / "usability" / "metrics.sqlite3").exists()
        with closing(sqlite3.connect(tmp_path_factory / "observability.sqlite3")) as db:
            db.execute("CREATE TABLE trace_events(trace_id TEXT,sequence INTEGER,event_type TEXT,record_json TEXT,occurred_at TEXT)")
        wait_for(lambda: collector.status()["state"] == "running")
        assert collector.status()["last_error_code"] is None
    finally:
        collector.stop()


def test_optional_worker_start_failure_becomes_diagnostic_not_daemon_failure(tmp_path_factory, monkeypatch):
    from backend.core.daemon import _start_usability_collector
    class BrokenWorker:
        def __init__(self, *_args, **_kwargs): pass
        def start(self): raise RuntimeError("cannot start thread")
    monkeypatch.setattr("backend.core.usability.UsabilityCollector", BrokenWorker)
    events = queue.Queue()
    assert _start_usability_collector(SimpleNamespace(paths=SimpleNamespace(project_root=tmp_path_factory)), events) is None
    assert events.get_nowait()["code"] == "USABILITY_COLLECTION_START_FAILED"
