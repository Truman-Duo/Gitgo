"""Post-v18 SDK fixtures; not human labels or real API acceptance."""
import runpy,time
from pathlib import Path
import pytest
from backend.core.mechanism_runtime.runner import Context
observe=runpy.run_path(str(Path(__file__).resolve().parents[1]/'dev-artifacts/postfreeze-confirmed-component.py'))['observe']
def invoke(records,*,task='task',next_offset=None,kinds=None):
    projected=[]
    ctx=Context('project','task','instance',1,'event','restored','node',{},
        {'decision.read':lambda c,p:{'task_id':task,'confirmed':records,'next_offset':next_offset},
         'context.project':lambda c,p:projected.append(p) or {}},lambda:False,time.monotonic()+5)
    return observe(ctx,{'max_pages':1,'include_kinds':kinds or []},{'go':True}),projected
def test_filter_and_exact_value_version_do_not_answer_or_grant():
    record={'kind':'clarification','decision_id':'one','value':'A'}
    first,_=invoke([record],kinds=['clarification'])
    second,_=invoke([{**record,'value':'B'}],kinds=['clarification'])
    assert first.outputs['report']['counts']=={'clarification':1}
    assert first.outputs['report']['records'][0]['version']!=second.outputs['report']['records'][0]['version']
    assert not second.outputs['report']['authority_granted']
def test_bounded_page_is_partial_and_empty_report_is_explicit():
    result,projected=invoke([],next_offset=32)
    assert result.outputs['report']['partial'] and result.outputs['report']['counts']=={} and len(projected)==1
def test_foreign_scope_rejected_before_projecting():
    with pytest.raises(ValueError,match='SCOPE_CHANGED'):invoke([],task='foreign')

def test_new_registered_element_initialization_missing_service_never_becomes_ready(tmp_path_factory):
    from backend.core.storage import StorageRuntime
    from backend.core.mechanism_runtime.registry import Registry
    from backend.core.mechanism_runtime.components import install
    from backend.core.mechanism_runtime.repository import Repository
    from backend.core.mechanism_runtime.coordinator import Coordinator
    registry=Registry();install(registry)
    runpy.run_path(str(Path(__file__).resolve().parents[1]/'dev-artifacts/postfreeze-confirmed-component.py'))['install'](registry)
    work=tmp_path_factory/'work';work.mkdir()
    with StorageRuntime(work,state_home=tmp_path_factory/'state') as storage:
        repo=Repository(storage);compiler=Coordinator(registry,repo,capabilities=('decision.read','context.project'))
        program={'schema_version':2,'id':'unit-new-component','intent':'Synthetic initialization failure',
            'source_refs':['unit:fixture'],'elements':[{'id':'event','use':'runtime.event','parameters':{'boundaries':['restored']}},
                {'id':'observe','use':'decision.confirmed_kinds'}],
            'bindings':[{'kind':'trigger','from':{'node':'event','port':'go'},'to':{'node':'observe','port':'go'}}]}
        definition,ref=repo.publish(compiler.preview(program)['compiled'],{'unit:fixture':{'kind':'unit_fixture'}},expected_head=None)
        repo.create_instance('task','unit',definition,ref,policy='pinned',sources=definition['sources'])
        owner=Coordinator(registry,repo,capabilities=('decision.read',))
        owner.request('task','unit',ref,expected_revision=1,sources=definition['sources'])
        with pytest.raises(ValueError,match='ACTIVATION_SERVICE_MISSING'):owner.settle('task','unit')
        result=repo.instance('task','unit')[0]
        assert result['phase']=='blocked' and result['epoch']==0 and not result['runs'] and not result['states']
