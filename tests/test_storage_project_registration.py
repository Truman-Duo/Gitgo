"""First logical registration is bound to the same actual State publication."""
from dataclasses import asdict,replace
from contextlib import closing
import hashlib,json
import sqlite3

import pytest
from backend.core.storage import StorageBlocked
from backend.core.storage.project_registration import ProjectRegistration
from backend.core.storage.repositories.checkpoints import plan_agent_checkpoint
from backend.core.storage.transactions import authoritative_transaction
from backend.core.storage.lifecycle import RevisionConflict
from backend.core.storage.models import StorageCommitUnknown
from backend.core.storage.checkpoint_sql import _encoded_scalar
from tests.test_checkpoint_work import runtime,profile,snapshot


def request(runtime,**changes):
    return replace(ProjectRegistration(runtime.paths.project_id,str(runtime.paths.workspace),'fixed'),**changes)


def publish(runtime,plan):
    plan.objects.materialize_legacy(runtime._write_blob_file)
    with authoritative_transaction(runtime._state):plan.apply(runtime._state)


def test_first_registration_is_pure_until_same_business_commit(runtime):
    runtime._state.execute('DELETE FROM projects')
    before=runtime._state.total_changes
    plan=plan_agent_checkpoint(runtime,snapshot(),limits=profile(),registration=request(runtime))
    assert plan.basis.registration.previous is None
    assert runtime._state.total_changes==before and not list(runtime.paths.cas_dir.rglob('*'))
    publish(runtime,plan)
    assert tuple(runtime._state.execute('SELECT * FROM projects').fetchone())==(
        runtime.paths.project_id,str(runtime.paths.workspace),'fixed','fixed')
    assert runtime.process_revision('p')==1
    assert runtime._state.execute('SELECT COUNT(*) FROM runtime_outbox').fetchone()[0]==1


def test_existing_registration_is_observed_without_silent_rewrite(runtime):
    before=tuple(runtime._state.execute('SELECT * FROM projects').fetchone())
    plan=plan_agent_checkpoint(runtime,snapshot(),limits=profile(),registration=request(runtime))
    assert plan.basis.registration.previous==before
    assert not any('INSERT INTO projects' in item.sql for item in plan.writes.statements)
    publish(runtime,plan)
    assert tuple(runtime._state.execute('SELECT * FROM projects').fetchone())==before


@pytest.mark.parametrize('change',['workspace','remove','updated'])
def test_registration_dependency_changes_refuse_before_publication(runtime,change):
    plan=plan_agent_checkpoint(runtime,snapshot(),limits=profile(),registration=request(runtime))
    if change=='workspace':runtime._state.execute("UPDATE projects SET workspace_hint='changed'")
    elif change=='remove':runtime._state.execute('DELETE FROM projects')
    else:runtime._state.execute("UPDATE projects SET updated_at='changed'")
    with pytest.raises(RevisionConflict):
        publish(runtime,plan)
    assert runtime.process_revision('p')==0
    assert runtime._state.execute('SELECT COUNT(*) FROM runtime_outbox').fetchone()[0]==0


def test_project_creation_rolls_back_with_failed_business_write(runtime,monkeypatch):
    runtime._state.execute('DELETE FROM projects')
    plan=plan_agent_checkpoint(runtime,snapshot(),limits=profile(),registration=request(runtime))
    original=runtime._state.execute
    def execute(sql,*args,**kwargs):
        result=original(sql,*args,**kwargs)
        if 'INSERT INTO sessions(' in sql:
            result.close();raise RuntimeError('business boundary')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(runtime._state,'execute',execute)
        with pytest.raises(RuntimeError,match='business boundary'):publish(runtime,plan)
    assert runtime._state.execute('SELECT COUNT(*) FROM projects').fetchone()[0]==0
    assert runtime._state.execute('SELECT COUNT(*) FROM objects').fetchone()[0]==0


def test_registration_metadata_shares_the_complete_input_budget(runtime):
    with pytest.raises(StorageBlocked,match='input_bytes'):
        plan_agent_checkpoint(runtime,snapshot(),limits=replace(profile(),input_bytes=8192),
                              registration=request(runtime,workspace_hint='x'*8192))


@pytest.mark.parametrize('kind',['foreign','workspace','no_limits','mutable'])
def test_unqualified_registration_is_refused_before_files(runtime,kind):
    import uuid
    registration=request(runtime)
    limits=profile()
    if kind=='foreign':registration=replace(registration,project_id=str(uuid.uuid4()))
    elif kind=='workspace':registration=replace(registration,workspace_hint='different')
    elif kind=='no_limits':limits=None
    else:registration={'project_id':runtime.paths.project_id}
    before=runtime._state.total_changes
    with pytest.raises(StorageBlocked):
        plan_agent_checkpoint(runtime,snapshot(),limits=limits,registration=registration)
    assert runtime._state.total_changes==before and not list(runtime.paths.cas_dir.rglob('*'))


def test_legacy_no_registration_preserves_its_previous_sql_digest(runtime):
    plan=plan_agent_checkpoint(runtime,snapshot(),limits=profile())
    basis=asdict(plan.basis)
    assert basis.pop('registration') is None
    encoded=[[item.sql,[_encoded_scalar(value) for value in item.parameters],item.expected_rows]
             for item in plan.writes.statements]
    binding=['checkpoint-sql-v1',plan.process_id,plan.parent_process_id,basis,
             [dict(item.descriptor) for item in plan.objects.members],encoded]
    raw=json.dumps(binding,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
    assert hashlib.sha256(raw).hexdigest()==plan.writes.digest


@pytest.mark.parametrize('value',['x'*8193, '\u6c49'*2731, '', b'blob'])
def test_retained_metadata_is_bounded_before_returning_to_python(runtime,monkeypatch,value):
    from backend.core.storage.project_registration import capture_project_basis
    runtime._state.execute('UPDATE projects SET workspace_hint=?',(value,))
    original=runtime._state.execute
    observed=[]
    class Cursor:
        def __init__(self,cursor):self.cursor=cursor
        def fetchall(self):
            rows=self.cursor.fetchall()
            observed.extend(tuple(row) for row in rows)
            return rows
        def close(self):self.cursor.close()
    def execute(sql,*args,**kwargs):
        cursor=original(sql,*args,**kwargs)
        return Cursor(cursor) if sql.startswith('SELECT CASE WHEN typeof(project_id)') else cursor
    with monkeypatch.context() as patch:
        patch.setattr(runtime._state,'execute',execute)
        with pytest.raises(StorageBlocked):
            with authoritative_transaction(runtime._state):
                capture_project_basis(runtime._state,request(runtime))
    assert len(observed)==1 and observed[0][1] is None
    assert not list(runtime.paths.cas_dir.rglob('*'))


@pytest.mark.parametrize('value',[[], '', '\ud800', '\u6c49'*2731])
def test_invalid_registration_values_fail_closed(runtime,value):
    with pytest.raises(StorageBlocked):request(runtime,workspace_hint=value)


@pytest.mark.parametrize('when',['before','after'])
def test_unknown_first_business_commit_keeps_registration_with_its_facts(runtime,monkeypatch,when):
    runtime._state.execute('DELETE FROM projects')
    plan=plan_agent_checkpoint(runtime,snapshot(),limits=profile(),registration=request(runtime))
    original=runtime._state.execute
    def execute(sql,*args,**kwargs):
        if sql=='COMMIT' and when=='before':raise OSError('lost registration commit')
        result=original(sql,*args,**kwargs)
        if sql=='COMMIT' and when=='after':
            result.close();raise OSError('lost registration commit')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(runtime._state,'execute',execute)
        with pytest.raises(StorageCommitUnknown):publish(runtime,plan)
    assert runtime._state.quarantined
    with closing(sqlite3.connect(runtime.paths.state_db.as_uri()+'?mode=ro',uri=True)) as reader:
        for table in ('projects','session_processes','runtime_outbox'):
            assert reader.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==int(when=='after')
        assert reader.total_changes==0
