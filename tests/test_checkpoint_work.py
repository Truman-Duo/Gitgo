"""The actual publication is planned before files and used unchanged at commit."""
from dataclasses import FrozenInstanceError, replace

import pytest

from backend.core.storage import StorageBlocked, StorageRuntime
from backend.core.storage.checkpoint_limits import CheckpointLimits
from backend.core.storage.lifecycle import RevisionConflict
from backend.core.storage.repositories.checkpoints import plan_agent_checkpoint
from backend.core.storage.transactions import authoritative_transaction


@pytest.fixture
def runtime(tmp_path_factory):
    workspace = tmp_path_factory/'work'
    workspace.mkdir()
    with StorageRuntime(workspace,state_home=tmp_path_factory/'state') as storage:
        yield storage


def profile():
    return CheckpointLimits(4096,16,262144,8192,4096,262144,128,262144,64,512,128)


def snapshot(**updates):
    return dict(process_id='p',session_id='s',task_id='t',status='running',messages=[],**updates)


def test_real_plan_can_be_built_without_new_sql_or_file_effects(runtime,monkeypatch):
    def forbidden(*args,**kwargs): pytest.fail('planning created a file or spent write capacity')
    monkeypatch.setattr(runtime,'_write_blob_file',forbidden)
    monkeypatch.setattr(runtime,'_reserve_authoritative_write',forbidden)
    before = runtime._state.total_changes
    plan = plan_agent_checkpoint(runtime,snapshot(),limits=profile())
    assert runtime._state.total_changes == before
    assert len(plan.objects.members) == 4 and len(plan.writes.statements) == 20
    assert plan.footprint.total_references == 0
    with pytest.raises(FrozenInstanceError): plan.messages = 999


def test_same_real_plan_applies_after_exact_materialization(runtime):
    plan = plan_agent_checkpoint(runtime,snapshot(),limits=profile())
    digest = plan.digest
    plan.objects.materialize_legacy(runtime._write_blob_file)
    with runtime._state_lock,authoritative_transaction(runtime._state):
        plan.apply(runtime._state)
    assert runtime.process_revision('p') == plan.reference().revision == 1
    assert plan.digest == digest
    assert runtime._state.execute('SELECT COUNT(*) FROM runtime_outbox').fetchone()[0] == 1


def test_plan_cannot_apply_without_an_owning_transaction(runtime):
    plan = plan_agent_checkpoint(runtime,snapshot())
    with pytest.raises(RuntimeError,match='REQUIRES_TRANSACTION'): plan.apply(runtime._state)
    assert runtime._state.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


def test_qualified_limits_are_bound_to_the_batch_digest(runtime,monkeypatch):
    monkeypatch.setattr('backend.core.storage.repositories.checkpoints._utc_now',lambda:'fixed')
    first = plan_agent_checkpoint(runtime,snapshot(),limits=profile())
    second = plan_agent_checkpoint(runtime,snapshot(),limits=replace(profile(),reference_rows=513))
    assert first.writes.digest == second.writes.digest
    assert first.digest != second.digest


def test_shared_history_growth_between_plan_and_publication_refuses(runtime):
    plan = plan_agent_checkpoint(runtime,snapshot(),limits=profile())
    runtime.append_session_event('unrelated','progress',{'n':1})
    plan.objects.materialize_legacy(runtime._write_blob_file)
    with pytest.raises(RevisionConflict,match='footprint'):
        with runtime._state_lock,authoritative_transaction(runtime._state):
            plan.apply(runtime._state)
    assert runtime._state.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


@pytest.mark.parametrize('field,value,reason',[
    ('cas_objects',3,'cas_objects'),('sql_statements',19,'sql_statements'),
    ('reference_rows',7,'new_roots'),('sql_parameter_bytes',1,'sql_parameter_bytes'),
    ('cas_bytes',1,'cas_bytes'),('replacement_rows',3,'new_references'),
])
def test_full_new_closure_must_fit_before_preparation(runtime,monkeypatch,field,value,reason):
    def forbidden(*args,**kwargs): pytest.fail('file effect before complete admission')
    monkeypatch.setattr(runtime,'_write_blob_file',forbidden)
    with pytest.raises(StorageBlocked,match=reason):
        plan_agent_checkpoint(runtime,snapshot(),limits=replace(profile(),**{field:value}))


def test_next_checkpoint_reserves_new_historical_roots_after_replacing_current_ones(runtime):
    runtime.save_agent_checkpoint(snapshot())
    # Four current and four historical roots; next checkpoint retains the old
    # history, replaces current roots and adds four new historical roots.
    with pytest.raises(StorageBlocked,match='new_roots'):
        plan_agent_checkpoint(runtime,snapshot(),limits=replace(profile(),reference_rows=11))
    plan = plan_agent_checkpoint(runtime,snapshot(),limits=replace(profile(),reference_rows=12))
    assert plan.footprint.total_references == 8



@pytest.mark.parametrize('change',[
    {'estimated_sql_bytes':1},{'messages':1},{'dependencies':1},{'terminal':True},
    {'session_id':'different'},
])
def test_complete_publication_cost_and_identity_are_bound(runtime,change):
    plan = plan_agent_checkpoint(runtime,snapshot(),limits=profile())
    assert replace(plan,**change).digest != plan.digest


def test_profile_and_footprint_cannot_be_detached(runtime):
    plan = plan_agent_checkpoint(runtime,snapshot(),limits=profile())
    for changes in ({'limits':None},{'footprint':None},{'messages':True},{'terminal':1}):
        with pytest.raises(StorageBlocked): replace(plan,**changes)


def test_oversized_history_refuses_before_dependency_scan(runtime,monkeypatch):
    for i in range(3): runtime.append_session_event('p','progress',{'n':i})
    def forbidden(*args,**kwargs): pytest.fail('unbounded dependency scan before history admission')
    monkeypatch.setattr('backend.core.storage.repositories.checkpoints.capture_basis',forbidden)
    with pytest.raises(StorageBlocked,match='existing_rows'):
        plan_agent_checkpoint(runtime,snapshot(),limits=replace(profile(),history_rows=2))
    assert not runtime._state.in_transaction


def test_independent_writer_cannot_grow_history_between_snapshot_queries(runtime,monkeypatch):
    import sqlite3
    from contextlib import closing
    from backend.core.storage.repositories import checkpoints
    original = checkpoints.capture_basis
    attempts = []
    def capture(connection,*args):
        with closing(sqlite3.connect(runtime.paths.state_db,timeout=0)) as competing:
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                competing.execute("INSERT INTO deleted_processes VALUES('other','plan','now')")
        attempts.append(True)
        return original(connection,*args)
    monkeypatch.setattr(checkpoints,'capture_basis',capture)
    plan_agent_checkpoint(runtime,snapshot(),limits=profile())
    assert attempts == [True]
    assert not runtime._state.in_transaction
