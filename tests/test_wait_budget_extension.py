"""Post-freeze developer SDK component fixtures, not real Provider grades."""
from pathlib import Path
from types import SimpleNamespace
import runpy
import pytest
inspect_waits=runpy.run_path(str(Path(__file__).resolve().parent/'fixtures/mechanism_extensions/postfreeze-wait-budget.py'))['inspect_waits']

def run(limit,partial=False,task='task'):
    calls=[]
    def call(name,payload):
        calls.append(name)
        return {'task_id':task,'questions':{'partial':partial,'items':[
            {'task_id':task,'decision_id':'real-card','status':'awaiting_user','kind':'clarification'},
            {'task_id':task,'decision_id':'answered-card','status':'answered','kind':'clarification'}]}}
    ctx=SimpleNamespace(task_id='task',state={},check=lambda:None,call=call)
    return inspect_waits(ctx,{'max_pending':limit,'kinds':[]},{}),calls

def test_budget_change_changes_observation_without_answering_or_enforcing():
    a,calls=run(0);b,_=run(1)
    assert a.outputs['report']['over_budget'] is True and b.outputs['report']['over_budget'] is False
    assert a.outputs['report']['pending_decision_ids']==['real-card']
    assert calls==['decision.read','context.project']
    assert a.outputs['report']['authority_granted'] is False

def test_partial_low_count_is_unknown_but_observed_excess_is_real():
    a,_=run(1,partial=True);b,_=run(0,partial=True)
    assert a.outputs['report']['over_budget'] is None and b.outputs['report']['over_budget'] is True

def test_wait_budget_cannot_read_another_task():
    with pytest.raises(ValueError,match='SCOPE_CHANGED'):run(1,task='another')
