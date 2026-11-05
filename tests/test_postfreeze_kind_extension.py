"""Developer SDK component fixtures; no human holdout or real API score."""
import json,runpy,time
from pathlib import Path
import pytest
from backend.core.mechanism_runtime.runner import Context
summarize=runpy.run_path(str(Path(__file__).resolve().parent/'fixtures/mechanism_extensions/postfreeze-kinds-component.py'))['summarize']
def invoke(items,parameters,*,partial=False,task='task'):
    projected=[]
    ctx=Context('project','task','instance',1,'event','restored','node',{},
        {'decision.read':lambda c,p:{'task_id':task,'questions':{'items':items,'partial':partial}},
         'context.project':lambda c,p:projected.append(p) or {}},lambda:False,time.monotonic()+5)
    return summarize(ctx,parameters,{'go':True}),projected
def test_counts_and_configuration_do_not_fabricate_authority():
    items=[{'task_id':'task','kind':'clarification','status':'answered'},
        {'task_id':'task','kind':'clarification','status':'awaiting_user'},
        {'task_id':'task','kind':'permission','status':'awaiting_user'}]
    result,projected=invoke(items,{'include_kinds':['clarification'],'emit_empty':True})
    assert result.outputs['report']['counts']=={'clarification:answered':1,'clarification:awaiting_user':1}
    assert not result.outputs['report']['authority_granted'] and len(projected)==1
def test_partial_empty_is_visible_and_configurable():
    result,projected=invoke([],{'include_kinds':[],'emit_empty':True},partial=True)
    assert result.outputs['report']['partial'] and 'Question kind observation:' in projected[0]['content']
    _,projected=invoke([],{'include_kinds':[],'emit_empty':False})
    assert not projected
def test_scope_failure_does_not_project():
    with pytest.raises(ValueError,match='SCOPE_CHANGED'):
        invoke([],{'include_kinds':[],'emit_empty':True},task='unrelated')


def test_actual_registered_component_cannot_activate_without_required_service(tmp_path_factory):
    from backend.core.storage import StorageRuntime
    from backend.core.mechanism_runtime.registry import Registry
    from backend.core.mechanism_runtime.components import install
    from backend.core.mechanism_runtime.repository import Repository
    from backend.core.mechanism_runtime.coordinator import Coordinator
    registry=Registry();install(registry)
    runpy.run_path(str(Path(__file__).resolve().parent/'fixtures/mechanism_extensions/postfreeze-kinds-component.py'))['install'](registry)
    work=tmp_path_factory/'work';work.mkdir()
    with StorageRuntime(work,state_home=tmp_path_factory/'state') as storage:
        repo=Repository(storage)
        compiling=Coordinator(registry,repo,capabilities=('decision.read','context.project'))
        raw={'schema_version':2,'id':'fixture','intent':'Synthetic registration failure fixture',
            'source_refs':['unit:fixture'],'elements':[{'id':'event','use':'runtime.event','parameters':{'boundaries':['restored']}},
                {'id':'summary','use':'decision.kind_summary'}],
            'bindings':[{'kind':'trigger','from':{'node':'event','port':'go'},'to':{'node':'summary','port':'go'}}]}
        definition,reference=repo.publish(compiling.preview(raw)['compiled'],{'unit:fixture':{'kind':'unit_fixture'}},expected_head=None)
        repo.create_instance('task','fixture',definition,reference,policy='pinned',sources=definition['sources'])
        failing=Coordinator(registry,repo,capabilities=('decision.read',))
        failing.request('task','fixture',reference,expected_revision=1,sources=definition['sources'])
        with pytest.raises(ValueError,match='ACTIVATION_SERVICE_MISSING'):failing.settle('task','fixture')
        value=repo.instance('task','fixture')[0]
        assert value['phase']=='blocked' and value['epoch']==0 and not value['runs'] and not value['states']
