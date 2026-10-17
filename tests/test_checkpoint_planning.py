"""Real SQLite checkpoint races and publication fault boundaries."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

from backend.core.storage import StorageBlocked, StorageRuntime
from backend.core.storage.lifecycle import RevisionConflict


@pytest.fixture
def runtime(tmp_path_factory):
    workspace = tmp_path_factory / "work"
    workspace.mkdir()
    with StorageRuntime(workspace, state_home=tmp_path_factory / "state",
                        exclusive_maintenance=True) as storage:
        yield storage


def checkpoint(**updates):
    return {"process_id": "proc", "session_id": "sess", "task_id": "task",
            "status": "running", "messages": [], "runtime_state": {}, **updates}


def rows(runtime, table):
    with closing(sqlite3.connect(runtime.paths.state_db)) as reader:
        return reader.execute("SELECT * FROM " + table).fetchall()


def interleave_first_file(monkeypatch, runtime, change):
    original = runtime._write_blob_file
    fired = []
    def write(data, **kwargs):
        if not fired:
            fired.append(True)
            change()
        return original(data, **kwargs)
    monkeypatch.setattr(runtime, "_write_blob_file", write)
    return fired


def test_deleted_process_is_not_resurrected_during_preparation(runtime, monkeypatch):
    runtime.save_agent_checkpoint(checkpoint(process_id="root", session_id="root-session",
        task_id="root-task", status="completed", role="supervisor", actor_kind="supervisor"))
    child = checkpoint(parent_id="root", status="completed", role="worker", actor_kind="worker")
    runtime.save_agent_checkpoint(child)
    # The public maintenance API commits the tombstone and removes child State.
    interleave_first_file(monkeypatch, runtime,
        lambda: runtime.purge_b_session("sess", "fixture-deletion"))
    with pytest.raises(ValueError, match="PROCESS_DELETED"):
        runtime.save_agent_checkpoint(child)
    assert runtime.is_process_deleted("proc")
    assert all(row[0] != "proc" for row in rows(runtime, "session_processes"))


@pytest.mark.parametrize("event_type", ["progress", "agent_complete"])
def test_new_event_during_preparation_refuses_stale_publication(runtime, monkeypatch, event_type):
    runtime.save_agent_checkpoint(checkpoint())
    original = runtime.append_session_event
    fired = interleave_first_file(monkeypatch, runtime,
        lambda: original("proc", event_type, {"status": "completed"}))
    with pytest.raises(RevisionConflict):
        runtime.save_agent_checkpoint(checkpoint())
    assert fired and runtime.process_revision("proc") == 1
    assert len(rows(runtime, "session_events")) == 1


@pytest.mark.parametrize("dependency", ["revision", "status", "runtime", "parent", "terminal", "identity"])
def test_independent_writer_changes_captured_dependency(runtime, monkeypatch, dependency):
    runtime.save_agent_checkpoint(checkpoint(process_id="parent", session_id="parent-session",
        task_id="parent-task"))
    runtime.save_agent_checkpoint(checkpoint(parent_id="parent"))
    runtime.append_session_event("proc", "agent_complete", {"status": "completed"})
    sql = {
        "revision": "UPDATE session_processes SET state_revision=2 WHERE process_id='proc'",
        "status": "UPDATE session_processes SET status='waiting' WHERE process_id='proc'",
        "runtime": "UPDATE session_processes SET runtime_state_ref=NULL WHERE process_id='proc'",
        "parent": "UPDATE session_processes SET task_id=NULL WHERE process_id='parent'",
        "terminal": "UPDATE session_events SET occurred_at='changed' WHERE process_id='proc'",
        "identity": "UPDATE session_processes SET parent_process_id=NULL WHERE process_id='proc'",
    }[dependency]
    def change():
        with closing(sqlite3.connect(runtime.paths.state_db)) as writer:
            writer.execute(sql)
            writer.commit()
    interleave_first_file(monkeypatch, runtime, change)
    before = rows(runtime, "runtime_outbox")
    with pytest.raises(RevisionConflict):
        runtime.save_agent_checkpoint(checkpoint(parent_id="parent"))
    assert rows(runtime, "runtime_outbox") == before


def test_stale_revision_and_invalid_transition_fail_before_files(runtime, monkeypatch):
    runtime.save_agent_checkpoint(checkpoint(status="completed", runtime_state={"lifecycle_finalized": True}))
    def forbidden(*args, **kwargs):
        pytest.fail("validation must precede file creation")
    monkeypatch.setattr(runtime, "_write_blob_file", forbidden)
    with pytest.raises(RevisionConflict):
        runtime.save_agent_checkpoint(checkpoint(expected_revision=0))
    with pytest.raises(ValueError, match="PROCESS_TRANSITION_REJECTED"):
        runtime.save_agent_checkpoint(checkpoint(expected_revision=1))


@pytest.mark.parametrize("failure", ["missing", "corrupt", "oversized"])
def test_prior_lifecycle_object_is_verified_without_repair(runtime, monkeypatch, failure):
    runtime.save_agent_checkpoint(checkpoint(status="completed"))
    ref = runtime._state.execute("SELECT runtime_state_ref FROM session_processes").fetchone()[0]
    digest = ref.split(":")[1]
    path = runtime.paths.cas_dir / digest[:2] / digest[2:]
    if failure == "missing":
        path.unlink()
    elif failure == "corrupt":
        path.write_bytes(b'{"lifecycle_finalized":false}')
    else:
        path.write_bytes(b" " * 100)
        monkeypatch.setattr(runtime, "policy", replace(runtime.policy, max_cas_object_bytes=64))
    def forbidden(*args, **kwargs):
        pytest.fail("lifecycle validation cannot repair or prepare files")
    monkeypatch.setattr(runtime, "_restore_canonical_blob", forbidden)
    monkeypatch.setattr(runtime, "_write_blob_file", forbidden)
    with pytest.raises(StorageBlocked):
        runtime.save_agent_checkpoint(checkpoint(status="failed", runtime_state={"lifecycle_finalized": False}))
    assert runtime.process_revision("proc") == 1
    if failure == "missing":
        assert not path.exists()


def test_provisional_success_can_fail_using_prior_bytes_outside_sql(runtime, monkeypatch):
    runtime.save_agent_checkpoint(checkpoint(status="completed", runtime_state={"lifecycle_finalized": False}))
    read = runtime._read_json_blob
    observed = []
    def checked(ref, **kwargs):
        assert not runtime._state.in_transaction
        assert kwargs == {"repair": False}
        observed.append(ref)
        return read(ref, **kwargs)
    monkeypatch.setattr(runtime, "_read_json_blob", checked)
    assert runtime.save_agent_checkpoint(checkpoint(status="failed",
        runtime_state={"lifecycle_finalized": False})).revision == 2
    assert len(observed) == 1


def test_complete_closure_precedes_sql_and_fact_owns_every_member(runtime, monkeypatch):
    write = runtime._write_blob_file
    written = []
    def checked(data, **kwargs):
        assert not runtime._state.in_transaction, "file write inside SQL"
        result = write(data, **kwargs)
        written.append(result["digest"])
        return result
    monkeypatch.setattr(runtime, "_write_blob_file", checked)
    snapshot = checkpoint(status="completed", runtime_state={"lifecycle_finalized": True},
        provider_state={"provider": {"opaque": "sample"}},
        session_metadata={"context_memo": "memo", "host_ledger": {"record": 1}},
        messages=[{"role": "assistant", "content": "sample", "provider_state_id": "provider"}],
        result={"status": "degraded"}, tool_receipts=[{"receipt_id": "receipt", "effect": "read"}])
    ref = runtime.save_agent_checkpoint(snapshot)
    assert ref.revision == 1 and ref.endswith("/0")
    with runtime._state_lock:
        owned = {row[0] for row in runtime._state.execute(
            "SELECT digest FROM object_refs WHERE owner_type='runtime_fact' AND owner_id='proc:1'")}
        current = {row[0] for row in runtime._state.execute(
            "SELECT digest FROM object_refs WHERE owner_type='session_checkpoint' AND owner_id='sess'")}
    assert len(written) == len(set(written)) and set(written) == owned == current
    barrier = runtime._state.execute("SELECT sequence,payload_ref FROM session_events").fetchone()
    assert barrier[0] == 1
    assert runtime._read_json_blob(barrier[1])["state_revision"] == ref.revision
    runtime.save_agent_checkpoint(checkpoint(status="completed", runtime_state={"lifecycle_finalized": True}))
    assert len(rows(runtime, "session_events")) == 1
    assert owned <= {row[0] for row in runtime._state.execute("SELECT digest FROM object_refs WHERE owner_type='runtime_fact' AND owner_id='proc:1'")}
    assert all((runtime.paths.cas_dir / d[:2] / d[2:]).exists() for d in owned)


def test_snapshot_mutation_after_planning_cannot_change_rows_or_bytes(runtime, monkeypatch):
    snapshot = checkpoint(messages=[{"role": "user", "content": "original"}],
        runtime_state={"marker": ["original"]})
    def mutate():
        snapshot["status"] = "completed"
        snapshot["messages"][0]["role"] = "assistant"
        snapshot["runtime_state"]["marker"].append("changed")
        snapshot["runtime_state"]["lifecycle_finalized"] = True
    interleave_first_file(monkeypatch, runtime, mutate)
    runtime.save_agent_checkpoint(snapshot)
    process = runtime._state.execute("SELECT status,runtime_state_ref FROM session_processes").fetchone()
    assert process[0] == "running"
    assert runtime._read_json_blob(process[1]) == {"marker": ["original"]}
    assert runtime._state.execute("SELECT role FROM messages").fetchone()[0] == "user"
    assert rows(runtime, "session_events") == []


@pytest.mark.parametrize("failure", ["last_object", "aggregate", "reserve", "self_dependency"])
def test_rejected_complete_plan_creates_no_files(runtime, monkeypatch, failure):
    snapshot = checkpoint(status="completed", runtime_state={"lifecycle_finalized": True})
    if failure == "last_object":
        # Individual component fits; checkpoint fact exceeds this object limit.
        monkeypatch.setattr(runtime, "policy", replace(runtime.policy, max_cas_object_bytes=256))
    elif failure == "aggregate":
        monkeypatch.setattr(runtime, "policy", replace(runtime.policy, cas_max_bytes=400))
    elif failure == "reserve":
        def reject(_):
            raise StorageBlocked("fixture budget refusal")
        monkeypatch.setattr(runtime, "_reserve_authoritative_write", reject)
    else:
        snapshot["depends_on"] = ["proc"]
    def forbidden(*args, **kwargs):
        pytest.fail("complete plan must be accepted before file work")
    monkeypatch.setattr(runtime, "_write_blob_file", forbidden)
    with pytest.raises((StorageBlocked, ValueError)):
        runtime.save_agent_checkpoint(snapshot)
    assert rows(runtime, "session_processes") == []


@pytest.mark.parametrize("fault_at", [1, 3, 4, 5])
def test_interrupted_materialization_never_publishes_any_rows(runtime, monkeypatch, fault_at):
    write = runtime._write_blob_file
    count = 0
    def fail(data, **kwargs):
        nonlocal count
        count += 1
        result = write(data, **kwargs)
        if count == fault_at:
            raise OSError("fixture materialization response lost")
        return result
    monkeypatch.setattr(runtime, "_write_blob_file", fail)
    with pytest.raises(OSError):
        runtime.save_agent_checkpoint(checkpoint(status="completed", runtime_state={"lifecycle_finalized": True}))
    assert count == fault_at
    for table in ("session_processes", "object_refs", "runtime_outbox", "objects"):
        assert rows(runtime, table) == []


@pytest.mark.parametrize("key", ["digest", "ref", "byte_length", "media_type"])
def test_descriptor_mismatch_cannot_publish(runtime, monkeypatch, key):
    write = runtime._write_blob_file
    def wrong(data, **kwargs):
        result = write(data, **kwargs)
        result[key] = "different"
        return result
    monkeypatch.setattr(runtime, "_write_blob_file", wrong)
    with pytest.raises(StorageBlocked, match="checkpoint_object_descriptor_changed"):
        runtime.save_agent_checkpoint(checkpoint())
    assert rows(runtime, "runtime_outbox") == []


def test_nested_transaction_refused_before_files_without_rolling_back_owner(runtime, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("nested checkpoint cannot prepare files")
    monkeypatch.setattr(runtime, "_write_blob_file", forbidden)
    runtime._state.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError, match="STATE_TRANSACTION_ALREADY_ACTIVE"):
            runtime.save_agent_checkpoint(checkpoint())
        assert runtime._state.in_transaction
    finally:
        runtime._state.execute("ROLLBACK")


def test_manifest_contains_immutable_deduplicated_final_bytes(runtime):
    from backend.core.storage.checkpoint_plan import CheckpointObjectBuilder
    builder = CheckpointObjectBuilder(runtime._encoded_object, runtime.policy, "fixture-time")
    payload = {"items": [1]}
    descriptor = builder.prepare(payload)
    assert builder.prepare(payload) == descriptor
    manifest = builder.seal()
    payload["items"].append(2)
    assert len(manifest.members) == 1
    member = manifest.members[0]
    assert json.loads(member.data) == {"items": [1]}
    assert manifest.byte_length == len(member.data)
    assert hashlib.sha256(member.data).hexdigest() == descriptor["digest"]
    with pytest.raises(TypeError):
        descriptor["ref"] = "changed"
    with pytest.raises(RuntimeError, match="sealed"):
        builder.prepare({})


@pytest.mark.parametrize("failure", ["barrier", "outbox", "ownership"])
def test_final_sql_failure_rolls_back_complete_publication(runtime, failure):
    table, condition = {
        "barrier": ("session_events", "NEW.event_type='agent_complete'"),
        "outbox": ("runtime_outbox", "NEW.kind='agent_complete'"),
        "ownership": ("object_refs", "NEW.owner_type='runtime_fact'"),
    }[failure]
    runtime._state.execute("CREATE TRIGGER reject_publication BEFORE INSERT ON " + table
        + " WHEN " + condition + " BEGIN SELECT RAISE(ABORT,'fixture publication failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture publication failure"):
        runtime.save_agent_checkpoint(checkpoint(status="completed", runtime_state={"lifecycle_finalized": True}))
    for table in ("session_processes", "object_refs", "runtime_outbox", "objects", "session_events"):
        assert rows(runtime, table) == []
    assert not runtime._state.quarantined


@pytest.mark.parametrize("mode", ["before", "after"])
def test_terminal_checkpoint_commit_gap_preserves_uncertainty(runtime, mode):
    from backend.core.storage import StorageCommitUnknown
    from tests.test_storage_transaction_outcomes import inject
    with inject(runtime, mode):
        with pytest.raises(StorageCommitUnknown):
            runtime.save_agent_checkpoint(checkpoint(status="completed", runtime_state={"lifecycle_finalized": True}))
        assert runtime._state.quarantined
        assert len(rows(runtime, "session_processes")) == (mode == "after")
        assert len(rows(runtime, "session_events")) == (mode == "after")
        assert len(rows(runtime, "runtime_outbox")) == (2 if mode == "after" else 0)
        assert bool(rows(runtime, "object_refs")) == (mode == "after")
        with pytest.raises(StorageCommitUnknown):
            runtime.save_agent_checkpoint(checkpoint())


def test_event_backfilled_below_last_sequence_refuses_stale_publication(runtime, monkeypatch):
    runtime.save_agent_checkpoint(checkpoint())
    runtime.append_session_event("proc", "progress", {"position": "later"}, sequence=2)
    interleave_first_file(monkeypatch, runtime, lambda: runtime.append_session_event(
        "proc", "progress", {"position": "backfill"}, sequence=1))
    with pytest.raises(RevisionConflict):
        runtime.save_agent_checkpoint(checkpoint())
    assert runtime.process_revision("proc") == 1
    assert runtime._state.execute("SELECT checkpoint_event_sequence FROM session_processes").fetchone()[0] == 0
    assert len(rows(runtime, "session_events")) == 2
