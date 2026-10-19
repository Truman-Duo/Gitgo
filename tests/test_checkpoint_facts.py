"""Planned outbox evidence must agree with the real retained State rows."""
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
import sqlite3

import pytest

from backend.core.storage import StorageBlocked, StorageRuntime
from backend.core.storage.fact_record import FACT_COLUMNS
from backend.core.storage.models import StorageCommitUnknown
from backend.core.storage.repositories.checkpoints import plan_agent_checkpoint
from backend.core.storage.transactions import authoritative_transaction
from tests.test_checkpoint_work import profile


@pytest.fixture
def planned(tmp_path_factory):
    workspace = tmp_path_factory/'work'
    workspace.mkdir()
    with StorageRuntime(workspace,state_home=tmp_path_factory/'state') as runtime:
        plan = plan_agent_checkpoint(runtime,dict(process_id='p',session_id='s',task_id='t',
            status='completed',runtime_state={'lifecycle_finalized':True},
            messages=[],result={'response':'done'}),limits=profile())
        assert plan.terminal and len(plan.facts) == 2
        yield runtime,plan


def publish(runtime,plan):
    plan.objects.materialize_legacy(runtime._write_blob_file)
    with runtime._state_lock,authoritative_transaction(runtime._state):
        plan.apply(runtime._state)


def test_prepared_evidence_is_immutable_and_cannot_claim_a_missing_commit(planned):
    runtime,plan = planned
    before = runtime._state.total_changes
    with pytest.raises(FrozenInstanceError):
        plan.facts[0].payload_ref = 'changed'
    with pytest.raises(StorageBlocked,match='evidence_mismatch'):
        plan.require_retained_facts(runtime._state)
    assert runtime._state.total_changes == before
    assert not list(runtime.paths.cas_dir.rglob('*'))


def test_exact_checkpoint_and_terminal_evidence_match_after_one_commit(planned):
    runtime,plan = planned
    assert tuple(f.kind for f in plan.facts) == ('checkpoint_committed','agent_complete')
    publish(runtime,plan)
    before = runtime._state.total_changes
    plan.require_retained_facts(runtime._state)
    assert runtime._state.total_changes == before


@pytest.mark.parametrize('change',['empty','list','reversed','duplicate','payload','causation','stream'])
def test_evidence_cannot_diverge_from_the_closed_publication_sql(planned,change):
    _,plan = planned
    facts = plan.facts
    if change == 'empty': facts = ()
    elif change == 'list': facts = list(facts)
    elif change == 'reversed': facts = tuple(reversed(facts))
    elif change == 'duplicate': facts = (facts[0],facts[0])
    elif change == 'payload': facts = (replace(facts[0],payload_ref='sha256:'+'0'*64),facts[1])
    elif change == 'causation': facts = (replace(facts[0],causation_id='other-command'),facts[1])
    elif change == 'stream': facts = (replace(facts[0],stream_sequence=2),facts[1])
    with pytest.raises(StorageBlocked,match='checkpoint_fact_'):
        replace(plan,facts=facts)


@pytest.mark.parametrize('column',[*FACT_COLUMNS,'schema_version','producer'])
def test_every_retained_fact_field_is_checked_without_repair(planned,column):
    runtime,plan = planned
    publish(runtime,plan)
    # Deliberate inconsistency only inside this owned disposable database.
    value = 99 if column in {'stream_sequence','schema_version'} else 'different'
    runtime._state.execute('UPDATE runtime_outbox SET '+column+'=? WHERE event_id=?',
                           (value,plan.facts[0].event_id))
    before = runtime._state.total_changes
    with pytest.raises(StorageBlocked,match='evidence_mismatch'):
        plan.require_retained_facts(runtime._state)
    assert runtime._state.total_changes == before


@pytest.mark.parametrize('index',[0,1])
def test_missing_checkpoint_or_terminal_fact_is_not_reconstructed(planned,index):
    runtime,plan = planned
    publish(runtime,plan)
    runtime._state.execute('DELETE FROM runtime_outbox WHERE event_id=?',(plan.facts[index].event_id,))
    before = runtime._state.total_changes
    with pytest.raises(StorageBlocked,match='evidence_mismatch'):
        plan.require_retained_facts(runtime._state)
    assert runtime._state.execute('SELECT COUNT(*) FROM runtime_outbox').fetchone()[0] == 1
    assert runtime._state.total_changes == before


@pytest.mark.parametrize('when',['before','after'])
def test_lost_commit_response_needs_an_independent_retained_fact_read(planned,monkeypatch,when):
    runtime,plan = planned
    original = runtime._state.execute
    def execute(sql,*args,**kwargs):
        if sql == 'COMMIT' and when == 'before':
            raise OSError('commit response lost before native entry')
        result = original(sql,*args,**kwargs)
        if sql == 'COMMIT' and when == 'after':
            raise OSError('commit response lost after native return')
        return result
    monkeypatch.setattr(runtime._state,'execute',execute)
    with pytest.raises(StorageCommitUnknown):
        publish(runtime,plan)
    assert runtime._state.quarantined
    with pytest.raises(StorageCommitUnknown):
        plan.require_retained_facts(runtime._state)
    with closing(sqlite3.connect(runtime.paths.state_db.as_uri()+'?mode=ro',uri=True)) as reader:
        if when == 'before':
            with pytest.raises(StorageBlocked,match='evidence_mismatch'):
                plan.require_retained_facts(reader)
        else:
            plan.require_retained_facts(reader)
        assert reader.total_changes == 0
