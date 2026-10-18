"""Actual State retirement transactions; native proofs belong to another test."""
from contextlib import closing
import sqlite3
import pytest

from backend.core.storage import StorageBlocked
from backend.core.storage.models import StorageCommitUnknown
from backend.core.storage.transactions import authoritative_transaction
from tests.test_storage_batch_sql import prepared,materialized,count
from backend.core.storage import batch_sql as batch
from backend.core.storage import batch_retirement as retirement


@pytest.mark.parametrize('published',[False,True])
def test_preparation_retirement_preserves_actual_published_business(prepared,published):
    runtime,plan,records=prepared;db=runtime._state
    materialized(runtime,plan,records)
    if published:
        with authoritative_transaction(db):batch.publish(db,plan,records)
    with authoritative_transaction(db):
        observed=retirement.observe(db,plan.context.request)
        assert observed.published is published
        retirement.mark_retiring(db,observed)
    with authoritative_transaction(db):
        retained=retirement.observe(db,plan.context.request)
        retirement.finish(db,retained)
    assert count(db,'storage_batches')==count(db,'storage_batch_members')==0
    assert count(db,'runtime_outbox')==int(published)
    assert count(db,'storage_cas_generations')==(len(records) if published else 0)


@pytest.mark.parametrize('fault',['member_missing','bytes','phase','fact','generation'])
def test_missing_or_conflicting_evidence_blocks_retirement(prepared,fault):
    runtime,plan,records=prepared;db=runtime._state
    materialized(runtime,plan,records)
    with authoritative_transaction(db):batch.publish(db,plan,records)
    if fault=='member_missing':db.execute('DELETE FROM storage_batch_members WHERE ordinal=0')
    elif fault=='bytes':db.execute('UPDATE storage_batch_members SET planned=zeroblob(byte_length) WHERE ordinal=0')
    elif fault=='phase':db.execute("UPDATE storage_batch_members SET phase='CREATED' WHERE ordinal=0")
    elif fault=='fact':db.execute('DELETE FROM runtime_outbox')
    else:db.execute("UPDATE storage_cas_generations SET phase='RETIRING' WHERE generation=?",(records[0].intent.generation,))
    before=count(db,'storage_batch_members')
    with pytest.raises(StorageBlocked):
        with authoritative_transaction(db):retirement.mark_retiring(db,retirement.observe(db,plan.context.request))
    assert count(db,'storage_batches')==1 and count(db,'storage_batch_members')==before


@pytest.mark.parametrize('published',[False,True])
@pytest.mark.parametrize('when',['before','after'])
def test_unknown_retirement_commit_is_resolved_by_independent_committed_read(prepared,monkeypatch,published,when):
    runtime,plan,records=prepared;db=runtime._state
    materialized(runtime,plan,records)
    if published:
        with authoritative_transaction(db):batch.publish(db,plan,records)
    with authoritative_transaction(db):retirement.mark_retiring(db,retirement.observe(db,plan.context.request))
    original=db.execute
    def execute(sql,*args,**kwargs):
        if sql=='COMMIT' and when=='before':raise OSError('retirement commit unknown')
        result=original(sql,*args,**kwargs)
        if sql=='COMMIT' and when=='after':
            result.close();raise OSError('retirement commit unknown')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(db,'execute',execute)
        with pytest.raises(StorageCommitUnknown):
            with authoritative_transaction(db):retirement.finish(db,retirement.observe(db,plan.context.request))
    assert db.quarantined
    with closing(sqlite3.connect(runtime.paths.state_db.as_uri()+'?mode=ro',uri=True)) as reader:
        assert count(reader,'storage_batches')==int(when=='before')
        assert count(reader,'storage_batch_members')==(len(records) if when=='before' else 0)
        assert count(reader,'runtime_outbox')==int(published)
        assert count(reader,'storage_cas_generations')==(len(records) if published else 0)
        assert reader.total_changes==0


def test_final_delete_failure_rolls_back_members_with_the_batch(prepared,monkeypatch):
    runtime,plan,records=prepared;db=runtime._state
    materialized(runtime,plan,records)
    with authoritative_transaction(db):retirement.mark_retiring(db,retirement.observe(db,plan.context.request))
    original=db.execute
    def execute(sql,*args,**kwargs):
        if sql.startswith('DELETE FROM storage_batches'):raise RuntimeError('retirement final boundary')
        return original(sql,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(db,'execute',execute)
        with pytest.raises(RuntimeError,match='final boundary'):
            with authoritative_transaction(db):retirement.finish(db,retirement.observe(db,plan.context.request))
    assert count(db,'storage_batches')==1 and count(db,'storage_batch_members')==len(records)
