"""Post-v25 SDK checks; separate from the frozen core engineering score."""
from pathlib import Path
import runpy
from types import SimpleNamespace
import pytest

component=runpy.run_path(str(Path(__file__).resolve().parents[1]/'dev-artifacts/postfreeze-receipts-component.py'))


def run(items,confirmed=(),state=None,*,partial=False,next_offset=None,task='t',kinds=()):
    calls=[]
    def call(name,payload):
        calls.append((name,payload))
        return {'task_id':task,'questions':{'items':items,'partial':partial},
            'confirmed':[{'decision_id':key} for key in confirmed],'next_offset':next_offset} if name=='decision.read' else {}
    ctx=SimpleNamespace(task_id='t',state=state or {},check=lambda:None,call=call)
    return component['audit'](ctx,{'max_pages':1,'kinds':list(kinds)},{}),calls


def question(answer=None,*,kind='novel-kind',truncated=False):
    return {'task_id':'t','decision_id':'q','kind':kind,'status':'received' if answer is not None else 'awaiting_user',
        'answer':answer,'answer_state':'received' if answer is not None else 'not_received','answer_truncated':truncated}


def test_received_reply_is_never_invented_confirmation_or_permission():
    result,calls=run([question('actual reply')])
    report=result.outputs['report']
    assert report['received_not_confirmed_in_current_page']==['q'] and not report['authority_granted']
    assert [name for name,_ in calls]==['decision.read','context.project']
    assert 'actual reply' not in calls[-1][1]['content']


def test_actual_confirmation_and_answer_transition_are_observed():
    first,_=run([question()])
    second,_=run([question('reply')],['q'],first.state)
    assert second.outputs['report']['changed_visible_receipts']==['q']
    assert second.outputs['report']['records'][0]['confirmed_in_current_canonical_page']


def test_partial_and_truncated_inputs_keep_their_evidence_limits():
    first,_=run([question('before')])
    second,_=run([question('visible prefix',truncated=True)],state=first.state,partial=True,next_offset=32)
    assert second.outputs['report']['partial'] and second.outputs['report']['records'][0]['answer_truncated']
    assert second.state['versions']==first.state['versions']
    filtered,_=run([question()],kinds=['other-kind'])
    assert filtered.outputs['report']['records']==[]


@pytest.mark.parametrize('foreign',['service','question'])
def test_other_tasks_are_rejected_before_publishing(foreign):
    with pytest.raises(ValueError,match='SCOPE_CHANGED'):
        run([question() if foreign=='service' else {**question(),'task_id':'foreign'}],task='foreign' if foreign=='service' else 't')


def test_missing_projection_dependency_never_appears_active(tmp_path_factory):
    from backend.core.storage import StorageRuntime
    from backend.core.mechanism_runtime.registry import Registry
    from backend.core.mechanism_runtime.components import install
    from backend.core.mechanism_runtime.repository import Repository
    from backend.core.mechanism_runtime.coordinator import Coordinator
    registry=Registry();install(registry);component['install'](registry)
    work=tmp_path_factory/'work';work.mkdir()
    with StorageRuntime(work,state_home=tmp_path_factory/'state') as storage:
        repo=Repository(storage)
        compiler=Coordinator(registry,repo,capabilities=('decision.read','context.project'))
        program={'schema_version':2,'id':'unit-receipts','intent':'SDK dependency initialization fixture','source_refs':['unit:fixture'],
            'elements':[{'id':'event','use':'runtime.event','parameters':{'boundaries':['restored']}},
                {'id':'audit','use':'question.receipt_state_audit'}],
            'bindings':[{'kind':'trigger','from':{'node':'event','port':'go'},'to':{'node':'audit','port':'go'}}]}
        definition,ref=repo.publish(compiler.preview(program)['compiled'],{'unit:fixture':{'kind':'unit_fixture'}},expected_head=None)
        repo.create_instance('task','unit',definition,ref,policy='pinned',sources=definition['sources'])
        owner=Coordinator(registry,repo,capabilities=('decision.read',))
        owner.request('task','unit',ref,expected_revision=1,sources=definition['sources'])
        with pytest.raises(ValueError,match='ACTIVATION_SERVICE_MISSING'):owner.settle('task','unit')
        instance=repo.instance('task','unit')[0]
        assert instance['phase']=='blocked' and instance['epoch']==0 and not instance['runs'] and not instance['states']
