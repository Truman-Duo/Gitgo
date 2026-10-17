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



@pytest.mark.parametrize("operation", ["acknowledge", "maintain"])
@pytest.mark.parametrize("mode", ["before", "after"])
def test_fact_control_commit_gap_quarantines_writer(tmp_path_factory, operation, mode):
    from backend.core.storage import StorageCommitUnknown
    from backend.core.storage.runtime_events import acknowledge_batch, maintain_facts
    from tests.test_storage_transaction_outcomes import inject, ResponseLost
    runtime = StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state")
    try:
        runtime.save_agent_checkpoint(checkpoint())
        event = read_facts(runtime, "observer")[0]
        if operation == "maintain":
            runtime._state.execute("INSERT INTO deleted_processes VALUES(?,?,?)", ("root", "plan", "time"))
        action = (lambda: acknowledge_batch(runtime, "observer", [event])) if operation == "acknowledge" else (
            lambda: maintain_facts(runtime))
        with inject(runtime, mode):
            with pytest.raises(StorageCommitUnknown) as caught:
                action()
            assert isinstance(caught.value.__cause__, ResponseLost)
            assert caught.value.stage == "commit" and runtime._state.quarantined
            with pytest.raises(StorageCommitUnknown):
                runtime.save_agent_checkpoint(checkpoint(1))
        with closing(sqlite3.connect(runtime.paths.state_db)) as reader:
            if operation == "acknowledge":
                assert reader.execute("SELECT COUNT(*) FROM runtime_consumers").fetchone()[0] == (mode == "after")
                assert reader.execute("SELECT COUNT(*) FROM runtime_outbox").fetchone()[0] == 1
            else:
                assert reader.execute("SELECT COUNT(*) FROM runtime_outbox").fetchone()[0] == (mode == "before")
                assert bool(reader.execute("SELECT 1 FROM object_refs WHERE owner_type='runtime_fact'").fetchone()) == (mode == "before")
    finally:
        runtime.close()


def test_fact_ack_rollback_failure_keeps_original_cause_and_quarantine(tmp_path_factory):
    from backend.core.storage import StorageCommitUnknown
    from backend.core.storage.runtime_events import acknowledge_batch
    from tests.test_storage_transaction_outcomes import inject
    runtime = StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state")
    try:
        runtime.save_agent_checkpoint(checkpoint())
        event = read_facts(runtime, "observer")[0]
        with inject(runtime, "rollback"):
            with pytest.raises(StorageCommitUnknown) as caught:
                acknowledge_batch(runtime, "observer", [{**event, "stream_sequence": 99}])
            assert isinstance(caught.value.__cause__, ValueError)
            assert caught.value.rollback_failed and runtime._state.quarantined
    finally:
        runtime.close()


def test_fact_control_refuses_nested_transaction_without_rolling_back_owner(tmp_path_factory):
    from backend.core.storage.runtime_events import acknowledge_batch, maintain_facts
    runtime = StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state")
    try:
        runtime.save_agent_checkpoint(checkpoint())
        event = read_facts(runtime, "observer")[0]
        runtime._state.execute("INSERT INTO deleted_processes VALUES(?,?,?)", ("root", "plan", "time"))
        for action in (lambda: acknowledge_batch(runtime, "observer", [event]),
                       lambda: maintain_facts(runtime)):
            runtime._state.execute("BEGIN IMMEDIATE")
            try:
                with pytest.raises(RuntimeError, match="STATE_TRANSACTION_ALREADY_ACTIVE"):
                    action()
                assert runtime._state.in_transaction
            finally:
                runtime._state.execute("ROLLBACK")
    finally:
        runtime.close()



def test_retired_fact_ownership_is_bounded_and_survives_restart(tmp_path_factory):
    from backend.core.storage.runtime_events import maintain_facts
    state = tmp_path_factory / "state"
    runtime = StorageRuntime(tmp_path_factory, state_home=state)
    try:
        first = checkpoint()
        first["messages"] = [{"role": "user", "content": str(i)} for i in range(12)]
        runtime.save_agent_checkpoint(first)
        runtime.save_agent_checkpoint(checkpoint(1))
        runtime._state.execute("INSERT INTO deleted_processes VALUES(?,?,?)", ("root", "plan", "time"))
        before = runtime._state.execute("SELECT COUNT(*) FROM object_refs WHERE owner_type='runtime_fact'").fetchone()[0]
        assert before > 3
        assert maintain_facts(runtime, limit=3) == 2
        assert runtime._state.execute("SELECT COUNT(*) FROM runtime_outbox").fetchone()[0] == 0
        assert runtime._state.execute("SELECT COUNT(*) FROM object_refs WHERE owner_type='runtime_fact'").fetchone()[0] == before - 3
        remaining = {r[0] for r in runtime._state.execute("SELECT digest FROM object_refs WHERE owner_type='runtime_fact'")}
        direct, owned, _ = runtime._cas_reference_inventory()
        assert remaining <= owned
    finally:
        runtime.close()
    runtime = StorageRuntime(tmp_path_factory, state_home=state)
    try:
        previous = before - 3
        while previous:
            assert maintain_facts(runtime, limit=3) == 0
            current = runtime._state.execute("SELECT COUNT(*) FROM object_refs WHERE owner_type='runtime_fact'").fetchone()[0]
            assert current == max(0, previous - 3)
            previous = current
        assert maintain_facts(runtime, limit=3) == 0
    finally:
        runtime.close()


def test_live_fact_ownership_is_not_treated_as_retirement_debt(tmp_path_factory):
    from backend.core.storage.runtime_events import maintain_facts
    runtime = StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state")
    try:
        runtime.save_agent_checkpoint(checkpoint())
        before = runtime._state.execute("SELECT COUNT(*) FROM object_refs").fetchone()[0]
        assert maintain_facts(runtime, limit=1) == 0
        assert runtime._state.execute("SELECT COUNT(*) FROM object_refs").fetchone()[0] == before
        assert len(read_facts(runtime, "observer")) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("mode", ["before", "after"])
def test_partial_retirement_commit_gap_retains_exact_ownership(tmp_path_factory, mode):
    from backend.core.storage import StorageCommitUnknown
    from backend.core.storage.runtime_events import maintain_facts
    from tests.test_storage_transaction_outcomes import inject
    runtime = StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state")
    try:
        runtime.save_agent_checkpoint(checkpoint())
        before = runtime._state.execute("SELECT COUNT(*) FROM object_refs WHERE owner_type='runtime_fact'").fetchone()[0]
        runtime._state.execute("INSERT INTO deleted_processes VALUES(?,?,?)", ("root", "plan", "time"))
        with inject(runtime, mode):
            with pytest.raises(StorageCommitUnknown):
                maintain_facts(runtime, limit=1)
        with closing(sqlite3.connect(runtime.paths.state_db)) as reader:
            assert reader.execute("SELECT COUNT(*) FROM runtime_outbox").fetchone()[0] == (mode == "before")
            expected = before - (mode == "after")
            assert reader.execute("SELECT COUNT(*) FROM object_refs WHERE owner_type='runtime_fact'").fetchone()[0] == expected
        assert runtime._state.quarantined
    finally:
        runtime.close()



def test_retention_rechecks_consumer_state_after_acquiring_writer(tmp_path_factory):
    from backend.core.storage.runtime_events import acknowledge_batch, maintain_facts
    from tests.test_storage_repository_outcomes import inject
    runtime = StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state")
    try:
        for revision in range(35):
            runtime.save_agent_checkpoint(checkpoint(revision))
        acknowledge_batch(runtime, "fast", read_facts(runtime, "fast"))
        before = runtime._state.execute("SELECT COUNT(*) FROM object_refs").fetchone()[0]
        def add_reader(connection):
            connection.execute("INSERT INTO runtime_consumers VALUES(?,?,?)", ("slow", "process:root", 0))
        with inject(runtime, "state", "none", before_begin=add_reader):
            assert maintain_facts(runtime, retain_per_stream=32) == 0
        assert len(read_facts(runtime, "slow")) == 35
        assert runtime._state.execute("SELECT COUNT(*) FROM object_refs").fetchone()[0] == before
    finally:
        runtime.close()


def test_retirement_rechecks_new_fact_before_removing_old_candidate_root(tmp_path_factory):
    from backend.core.storage.runtime_events import insert_fact, maintain_facts
    from tests.test_storage_repository_outcomes import inject
    runtime = StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state")
    try:
        runtime.save_agent_checkpoint(checkpoint())
        payload = runtime._state.execute("SELECT payload_ref FROM runtime_outbox").fetchone()[0]
        digest = payload.split(":", 1)[1]
        runtime._state.execute("INSERT INTO object_refs VALUES(?,?,?,?,?)", ("runtime_fact", "root:99", digest, digest, "time"))
        def publish(connection):
            insert_fact(connection, event_id="checkpoint:root:99", kind="checkpoint_committed",
                project_id=runtime.paths.project_id, session_id="session", task_id="task", process_id="root",
                stream_id="process:root", sequence=99, payload_ref=payload, occurred_at="time")
        with inject(runtime, "state", "none", before_begin=publish):
            assert maintain_facts(runtime, limit=1) == 0
        assert runtime._state.execute("SELECT COUNT(*) FROM object_refs WHERE owner_id='root:99'").fetchone()[0] == 1
    finally:
        runtime.close()
