"""Authority survives stale writers, rollback, observer failure and restart."""
import sqlite3
from contextlib import closing
import pytest

from backend.core.storage import StorageRuntime
from backend.core.storage.lifecycle import RevisionConflict
from backend.core.storage.runtime_events import read_facts, acknowledge, FactDispatcher
from backend.core.loop.lifecycle import transition, InvalidTransition
from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus


def checkpoint(revision=0):
    return {"process_id": "root", "session_id": "session", "task_id": "task",
        "expected_revision": revision, "status": "running", "messages": [], "runtime_state": {}}


def test_stale_writer_cannot_change_state_or_publish_fact(tmp_path_factory):
    workspace = tmp_path_factory / "work"; workspace.mkdir()
    first = StorageRuntime(workspace, state_home=tmp_path_factory / "state")
    second = StorageRuntime(workspace, state_home=tmp_path_factory / "state")
    try:
        ref = first.save_agent_checkpoint(checkpoint())
        assert ref.revision == 1
        stale = checkpoint(); stale["status"] = "completed"
        with pytest.raises(RevisionConflict): second.save_agent_checkpoint(stale)
        assert second.load_agent_process_state("root")["process"]["status"] == "running"
        facts = read_facts(first, "test")
        assert len(facts) == 1 and facts[0]["payload"]["revision"] == 1
    finally:
        second.close(); first.close()


def test_outbox_error_rolls_back_checkpoint(tmp_path_factory):
    workspace = tmp_path_factory / "work"; workspace.mkdir()
    runtime = StorageRuntime(workspace, state_home=tmp_path_factory / "state")
    try:
        runtime.save_agent_checkpoint(checkpoint())
        with closing(sqlite3.connect(runtime.paths.state_db)) as connection:
            connection.execute("CREATE TRIGGER reject_fact BEFORE INSERT ON runtime_outbox BEGIN SELECT RAISE(ABORT,'test failure'); END")
        changed = checkpoint(1); changed["status"] = "completed"
        with pytest.raises(sqlite3.IntegrityError): runtime.save_agent_checkpoint(changed)
        assert runtime.process_revision("root") == 1
        assert runtime.load_agent_process_state("root")["process"]["status"] == "running"
        assert len(read_facts(runtime, "test")) == 1
    finally: runtime.close()


def test_consumer_failure_replays_same_fact_without_execution(tmp_path_factory):
    workspace = tmp_path_factory / "work"; workspace.mkdir()
    runtime = StorageRuntime(workspace, state_home=tmp_path_factory / "state")
    runtime.save_agent_checkpoint(checkpoint())
    runtime.append_session_event("root", "user_decision_requested", {"decision_id": "existing"})
    failures = []; delivered = []
    def broken(event): raise RuntimeError("observer unavailable")
    dispatcher = FactDispatcher(runtime, "ui", broken, failures.append)
    assert dispatcher.drain() == 0
    assert dispatcher.drain() == 0 and len(failures) == 1
    dispatcher.handler = delivered.append
    assert dispatcher.drain() == 2 and len(delivered) == 2
    assert read_facts(runtime, "ui") == []
    assert len(read_facts(runtime, "metrics")) == 2
    with pytest.raises(ValueError): acknowledge(runtime, "ui", {**delivered[0], "stream_sequence": 100})
    runtime.close()
    reopened = StorageRuntime(workspace, state_home=tmp_path_factory / "state")
    try:
        assert read_facts(reopened, "ui") == []
        assert len(read_facts(reopened, "metrics")) == 2
    finally: reopened.close()


def test_terminal_lease_cannot_resume_and_cancellation_wins():
    process = AgentProcess("p", "worker", RingLevel.RING_3)
    process.cancel_requested = True
    transition(process, ProcessStatus.COMPLETED)
    assert process.status == ProcessStatus.CANCELLED
    transition(process, ProcessStatus.FAILED)
    assert process.status == ProcessStatus.CANCELLED
    with pytest.raises(InvalidTransition): transition(process, ProcessStatus.RUNNING)


def test_finality_barrier_is_atomic_and_cannot_be_reopened(tmp_path_factory):
    runtime = StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state")
    try:
        runtime.save_agent_checkpoint(checkpoint())
        final = checkpoint(1); final.update(status="completed", result={"status": "degraded"},
            runtime_state={"lifecycle_finalized": True})
        with closing(sqlite3.connect(runtime.paths.state_db)) as c:
            c.execute("CREATE TRIGGER reject_barrier BEFORE INSERT ON session_events WHEN NEW.event_type='agent_complete' BEGIN SELECT RAISE(ABORT,'barrier failure'); END")
        with pytest.raises(sqlite3.IntegrityError): runtime.save_agent_checkpoint(final)
        assert runtime.process_revision("root") == 1
        assert runtime.list_incomplete_processes() == ["root"]
        with closing(sqlite3.connect(runtime.paths.state_db)) as c:
            c.execute("DROP TRIGGER reject_barrier")
        runtime.save_agent_checkpoint(final)
        assert runtime.list_incomplete_processes() == []
        assert any(e['kind'] == 'agent_complete' and e['payload']['task_status'] == 'degraded' for e in read_facts(runtime, 'test'))
        forged = checkpoint(2); forged.update(status="failed", runtime_state={"lifecycle_finalized": False})
        with pytest.raises(InvalidTransition): runtime.save_agent_checkpoint(forged)
    finally: runtime.close()


def test_stream_order_failed_prefix_and_historical_immutable_refs(tmp_path_factory):
    runtime = StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state")
    try:
        for revision in range(12):
            data = checkpoint(revision); data['runtime_state'] = {'old_value': revision}
            runtime.save_agent_checkpoint(data)
        facts = read_facts(runtime, 'ui')
        assert [f['stream_sequence'] for f in facts] == list(range(1, 13))
        with pytest.raises(ValueError): acknowledge(runtime, 'ui', facts[1])
        assert runtime._read_json_blob(facts[0]['payload']['runtime_state_ref']) == {'old_value': 0}
        direct, owned, _ = runtime._cas_reference_inventory()
        assert facts[0]['payload']['runtime_state_ref'].split(':')[-1] in owned
        delivered = []; failures = []
        def handler(event):
            if event['stream_sequence'] == 2: raise ValueError('observer failure')
            delivered.append(event)
        dispatcher = FactDispatcher(runtime, 'ui', handler, failures.append)
        assert dispatcher.drain() == 0 and len(delivered) == 1
        assert read_facts(runtime, 'ui')[0]['stream_sequence'] == 2
        dispatcher.handler = delivered.append
        assert dispatcher.drain() == 11
    finally: runtime.close()
