"""Real State/outbox atomicity at the staged batch SQL seam; no OS qualification."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import uuid

import pytest

from backend.core.sandbox_read import ObjectIdentity
from backend.core.storage import StorageBlocked, StorageRuntime
from backend.core.storage.models import StorageCommitUnknown
from backend.core.storage.native_windows_cas import CasFileIntent, CasFileRecord
from backend.core.storage.runtime_lifecycle import RuntimeReference
from backend.core.storage.support import _StorageConnection
from backend.core.storage.transactions import authoritative_transaction
from backend.core.storage.repositories.checkpoints import plan_agent_checkpoint
from tests.test_checkpoint_work import profile,snapshot
from backend.core.storage.batch_records import BatchContext,BatchPreparation
from backend.core.storage import batch_sql as batch


@pytest.fixture
def prepared(tmp_path_factory,monkeypatch):
    workspace=tmp_path_factory/'work';workspace.mkdir()
    with StorageRuntime(workspace,state_home=tmp_path_factory/'state') as runtime:
        db=runtime._state
        with authoritative_transaction(db):
            for sql in batch.SCHEMA:db.execute(sql).close()
        plan=plan_agent_checkpoint(runtime,snapshot(),limits=profile())
        context=BatchContext('a'*64,runtime.paths.project_id,str(uuid.uuid4()),0,'b'*64,
            RuntimeReference(str(uuid.uuid4()),'c'*64,1),1,str(uuid.uuid4()),str(uuid.uuid4()),
            123,456,ObjectIdentity('win32',10,20))
        intents=tuple(CasFileIntent(obj.digest,str(uuid.uuid4()),len(obj.data)) for obj in plan.objects.members)
        staged=BatchPreparation(context,plan,intents)
        records=tuple(CasFileRecord(intent,ObjectIdentity('win32',10,30+i)) for i,intent in enumerate(intents))
        def forbidden(*args,**kwargs):pytest.fail('SQL batch seam performed file work')
        monkeypatch.setattr(runtime,'_write_blob_file',forbidden)
        yield runtime,staged,records
        assert not list(runtime.paths.cas_dir.rglob('*'))


def materialized(runtime,plan,records):
    db=runtime._state
    with authoritative_transaction(db):assert batch.prepare(db,plan) is None
    for ordinal,record in enumerate(records):
        with authoritative_transaction(db):batch.birth(db,plan,ordinal,record)
        with authoritative_transaction(db):batch.seal(db,plan,ordinal,record)


def count(db,table):return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]


def test_context_roundtrip_binds_native_parent_runtime_and_worker(prepared):
    _,plan,_=prepared
    assert BatchContext.from_bytes(plan.context.to_bytes())==plan.context
    for name,value in [('slot',True),('epoch',0),('pid',0),('created',0),('profile','bad')]:
        with pytest.raises(StorageBlocked):replace(plan.context,**{name:value})


def test_prepare_precedes_birth_and_has_no_rehydrated_ticket(prepared):
    runtime,plan,records=prepared;db=runtime._state
    with authoritative_transaction(db):
        with pytest.raises(StorageBlocked):batch.birth(db,plan,0,records[0])
    with authoritative_transaction(db):assert batch.prepare(db,plan) is None
    assert count(db,'session_processes')==count(db,'runtime_outbox')==0
    assert count(db,'storage_batch_members')==len(records)
    assert db.execute('SELECT COUNT(*) FROM storage_batch_members WHERE birth IS NULL').fetchone()[0]==len(records)
    with pytest.raises(StorageBlocked,match='fresh ticket'):
        with authoritative_transaction(db):batch.prepare(db,plan)


def test_real_state_outbox_roots_and_all_generation_ownership_commit_together(prepared):
    runtime,plan,records=prepared;db=runtime._state
    materialized(runtime,plan,records)
    with authoritative_transaction(db):batch.publish(db,plan,records)
    assert runtime.process_revision('p')==1
    assert count(db,'runtime_outbox')==1
    assert count(db,'storage_cas_generations')==count(db,'storage_cas_current')==len(records)
    with authoritative_transaction(db):batch.require_published(db,plan,records)
    # Handoff retains preparation debt until independent native retirement.
    assert count(db,'storage_batches')==1 and count(db,'storage_batch_members')==len(records)
    with pytest.raises(StorageBlocked):
        with authoritative_transaction(db):batch.publish(db,plan,records)
    assert runtime.process_revision('p')==1


@pytest.mark.parametrize('target',['generation','current','members','batch'])
def test_failure_inside_handoff_rolls_back_actual_business_and_catalogue(prepared,monkeypatch,target):
    runtime,plan,records=prepared;db=runtime._state
    materialized(runtime,plan,records)
    prefix={'generation':'INSERT INTO storage_cas_generations','current':'INSERT INTO storage_cas_current',
            'members':"UPDATE storage_batch_members SET phase='PUBLISHED'",
            'batch':"UPDATE storage_batches SET phase='PUBLISHED'"}[target]
    original=db.execute
    def execute(sql,*args,**kwargs):
        result=original(sql,*args,**kwargs)
        if sql.startswith(prefix):
            result.close();raise RuntimeError('handoff boundary fault')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(db,'execute',execute)
        with pytest.raises(RuntimeError,match='handoff boundary'):
            with authoritative_transaction(db):batch.publish(db,plan,records)
    assert count(db,'session_processes')==count(db,'runtime_outbox')==0
    assert count(db,'objects')==count(db,'object_refs')==0
    assert count(db,'storage_cas_generations')==count(db,'storage_cas_current')==0
    assert db.execute('SELECT phase FROM storage_batches').fetchone()[0]=='PREPARED'
    assert {row[0] for row in db.execute('SELECT phase FROM storage_batch_members')}=={'SEALED'}


@pytest.mark.parametrize('phase',['prepare','publish'])
@pytest.mark.parametrize('when',['before','after'])
def test_unknown_commit_preserves_outcome_for_independent_read_only_evidence(prepared,monkeypatch,phase,when):
    runtime,plan,records=prepared;db=runtime._state
    if phase=='publish':materialized(runtime,plan,records)
    original=db.execute
    def execute(sql,*args,**kwargs):
        if sql=='COMMIT' and when=='before':raise OSError('unknown before COMMIT')
        result=original(sql,*args,**kwargs)
        if sql=='COMMIT' and when=='after':
            result.close();raise OSError('unknown after COMMIT')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(db,'execute',execute)
        with pytest.raises(StorageCommitUnknown):
            with authoritative_transaction(db):
                (batch.prepare(db,plan) if phase=='prepare' else batch.publish(db,plan,records))
    assert db.quarantined
    with pytest.raises(StorageCommitUnknown):db.execute('SELECT 1')
    with closing(sqlite3.connect(runtime.paths.state_db.as_uri()+'?mode=ro',uri=True)) as reader:
        row=reader.execute('SELECT phase FROM storage_batches').fetchone()
        expected=(None if when=='before' else 'PREPARED') if phase=='prepare' else (
            'PREPARED' if when=='before' else 'PUBLISHED')
        assert (None if row is None else row[0])==expected
        published=phase=='publish' and when=='after'
        assert count(reader,'runtime_outbox')==int(published)
        assert count(reader,'storage_cas_generations')==(len(records) if published else 0)
        assert reader.total_changes==0


@pytest.mark.parametrize('fault',['missing_member','changed_bytes','wrong_birth','unsealed','foreign_context'])
def test_incomplete_or_changed_preparation_never_partially_publishes(prepared,fault):
    runtime,plan,records=prepared;db=runtime._state
    materialized(runtime,plan,records)
    if fault=='missing_member':
        db.execute('DELETE FROM storage_batch_members WHERE ordinal=0')
    elif fault=='changed_bytes':
        db.execute("UPDATE storage_batch_members SET planned=zeroblob(byte_length) WHERE ordinal=0")
    elif fault=='wrong_birth':
        records=(replace(records[0],identity=ObjectIdentity('win32',10,999)),*records[1:])
    elif fault=='unsealed':
        db.execute("UPDATE storage_batch_members SET phase='CREATED' WHERE ordinal=0")
    else:
        plan=replace(plan,context=replace(plan.context,launch=str(uuid.uuid4())))
    with pytest.raises(StorageBlocked):
        with authoritative_transaction(db):batch.publish(db,plan,records)
    assert count(db,'session_processes')==count(db,'runtime_outbox')==count(db,'storage_cas_generations')==0


def test_live_and_pending_generation_debt_are_aggregated_before_preparation(prepared):
    runtime,plan,records=prepared;db=runtime._state
    for i in range(29):
        db.execute("INSERT INTO storage_cas_generations VALUES(?,?,?,?,'LIVE')",
                   (str(uuid.uuid4()),str(i).zfill(64),0,
                    ('{"file_id":'+str(1000+i)+',"platform":"win32","volume":10}').encode()))
    before=count(db,'storage_cas_generations')
    with pytest.raises(StorageBlocked,match='charged'):
        with authoritative_transaction(db):batch.prepare(db,plan)
    assert count(db,'storage_cas_generations')==before and count(db,'storage_batches')==0

