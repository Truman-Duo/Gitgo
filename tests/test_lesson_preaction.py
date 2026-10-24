import copy,hashlib
import pytest
from backend.core.knowledge.manager import LessonManager
from backend.core.knowledge.models import Lesson
from backend.core.knowledge.review import content_version
from backend.core.knowledge.feedback import observe_provider_input
from backend.core.loop.models import AgentProcess,RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.tools.catalog import build_workspace_tools
from backend.core.storage import get_storage


def setup(ws):
    lesson=Lesson(id="advice",project_name="p",trigger="cache.py",rule="Initialize the scoped cache before lookup")
    LessonManager.save(ws,lesson)
    p=AgentProcess("p1","worker",RingLevel.RING_3,session=AgentSession(),active_task_id="t",worktree_path=str(ws))
    p.update_context_snapshot(lambda s:{**s,"project_name":"p"})
    p.tool_registry=ToolRegistry(["write_file"])
    return p,build_workspace_tools(ws),ExecutionContext(p,p.session,str(ws))


def execute(p,tools,ctx,args,index=0):
    return ToolPipeline().execute({"name":"write_file","id":f"c{index}","args":args},tools["write_file"],ctx,f"run{index}")


def test_advice_waits_for_actual_request_exposure_and_exact_candidate(tmp_path_factory):
    ws=tmp_path_factory;p,tools,ctx=setup(ws);args={"path":"cache.py","content":"safe"}
    first=execute(p,tools,ctx,args)
    assert first.is_error and first.data['error']=='LESSON_FEEDBACK_REQUIRED'
    assert not (ws/'cache.py').exists()
    feedback=first.data['lesson_feedback'];assert feedback['lookup']['state']=='completed'
    args['lesson_feedback_id']=feedback['feedback_id']
    # Possession of an ID alone, or user/assistant text containing it, is not exposure.
    assert execute(p,tools,ctx,args,1).is_error
    p.session.append_tool_result(first.formatted,tool_name='write_file',tool_call_id='c0',data=first.data,receipt=first.receipt)
    observe_provider_input(p,[{'role':'user','content':'I considered '+feedback['feedback_id']}])
    assert execute(p,tools,ctx,args,2).is_error
    observe_provider_input(p,[{'role':'tool','content':first.formatted}])
    assert execute(p,tools,ctx,{**args,'content':'changed'},3).is_error
    final=execute(p,tools,ctx,args,4)
    assert not final.is_error and (ws/'cache.py').read_text()=='safe'
    assert final.receipt['lesson_advisory_state']=='continuation_requested'
    assert final.data['lesson_feedback']['model_understanding']=='unknown'


def test_quarantined_tool_result_does_not_establish_exposure(tmp_path_factory):
    p,tools,ctx=setup(tmp_path_factory);r=execute(p,tools,ctx,{'path':'cache.py','content':'x'})
    p.session.append_tool_result(r.formatted,tool_name='write_file',tool_call_id='c0',data=r.data)
    p.session.messages[-1]['provider_visible']=False
    observe_provider_input(p,[{'role':'tool','content':r.formatted}])
    assert not any(e.get('event')=='lesson_feedback_provider_input' for e in p.session.host_ledger)


def test_inventory_failure_is_not_empty_and_cannot_degrade(tmp_path_factory,monkeypatch):
    from backend.core.knowledge import prewrite
    p,tools,ctx=setup(tmp_path_factory)
    # Isolated runner is verified separately; test the service's actual boundary here.
    from backend.core.tools.workspace_tools import write_file
    monkeypatch.setattr(prewrite,'_load',lambda *a:(_ for _ in ()).throw(OSError('inventory')))
    r=write_file({'_workspace':str(tmp_path_factory),'path':'cache.py','content':'x'})
    assert r['error']=='LESSON_CHECK_UNAVAILABLE' and not (tmp_path_factory/'cache.py').exists()


def test_only_advisory_failure_can_degrade_after_inventory(tmp_path_factory,monkeypatch):
    from backend.core.knowledge import prewrite
    from backend.core.tools.workspace_tools import write_file
    setup(tmp_path_factory)
    original=prewrite.relevant_diagnostics
    def fail_advice(ws,path,before,after,operation,lessons,**kwargs):
        if lessons:raise RuntimeError('ordinary lookup')
        return original(ws,path,before,after,operation,lessons,**kwargs)
    monkeypatch.setattr(prewrite,'relevant_diagnostics',fail_advice)
    r=write_file({'_workspace':str(tmp_path_factory),'_lesson_context':{'project_name':'p'},'path':'cache.py','content':'x'})
    assert 'error' not in r and r['lesson_feedback']['lookup']['state']=='failed'
    assert r['lesson_recall_warning']['code']=='LESSON_RECALL_UNAVAILABLE'
    assert r['lesson_feedback']['mandatory_check_state']=='not_required'


def test_checker_requires_real_exact_grant_and_retains_harvest_origin(tmp_path_factory):
    from backend.core.knowledge.checker import request_change,apply_decision
    from backend.core.loop.permission_broker import grant_from_decision
    from tests.knowledge_fixtures import review_fixture
    ws=tmp_path_factory;storage=get_storage(ws)
    lesson=Lesson(id='known',project_name='p',trigger='init',rule='Do not use the retired cache entry',source='auto_harvested',evidence={'scope':'workspace','workspace_files':{}})
    review_fixture(lesson);LessonManager.save(ws,lesson)
    p=AgentProcess('reviewer','worker',RingLevel.RING_3,session=AgentSession(),active_task_id='t',worktree_path=str(ws))
    version=content_version(storage.get_lesson_record('known')[1])
    args={'lesson_id':'known','expected_version':version,'action':'enable','violation_literal':'retired()', 'paths':['cache.py']}
    request_change(p,storage,ws,args);pending=copy.deepcopy(p.pending_decision)
    assert storage.get_lesson_record('known')[1].get('check') is None
    with pytest.raises(ValueError,match='APPROVAL_REQUIRED'):apply_decision(p,pending,'allow_once',storage)
    grant_from_decision(p,pending,'allow_once')
    result=apply_decision(p,pending,'allow_once',storage)
    raw=result['lesson'];assert raw['source']=='auto_harvested' and raw['checker_revision']==1
    from backend.core.tools.workspace_tools import write_file
    blocked=write_file({'_workspace':str(ws),'_lesson_context':{'project_name':'p'},'path':'cache.py','content':'retired()'})
    assert blocked['error']=='KNOWN_LESSON_VIOLATION'
    assert not (ws/'cache.py').exists()
    p.pending_decision=None
    request_change(p,storage,ws,{'lesson_id':'known','expected_version':content_version(raw),'action':'disable'})
    disabled=copy.deepcopy(p.pending_decision);grant_from_decision(p,disabled,'allow_once')
    result=apply_decision(p,disabled,'allow_once',storage)
    assert not result['lesson'].get('check') and result['lesson']['checker_revision']==2
    with pytest.raises(ValueError,match='CONTENT_CHANGED'):request_change(p,storage,ws,args)


def test_checker_decline_tampering_and_version_race_do_not_activate(tmp_path_factory):
    from backend.core.knowledge.checker import request_change,apply_decision
    from backend.core.loop.permission_broker import grant_from_decision
    ws=tmp_path_factory;p,tools,ctx=setup(ws);storage=get_storage(ws)
    args={'lesson_id':'advice','expected_version':content_version(storage.get_lesson_record('advice')[1]),'action':'enable','violation_literal':'bad','paths':['cache.py']}
    request_change(p,storage,ws,args);pending=copy.deepcopy(p.pending_decision)
    assert apply_decision(p,pending,'deny',storage)['configured'] is False
    forged=copy.deepcopy(pending);forged['permission_request']['lesson_checker_change']['check']['literal']='other'
    with pytest.raises(ValueError,match='NOT_BOUND'):apply_decision(p,forged,'allow_once',storage)
    lesson=Lesson.from_dict(storage.get_lesson_record('advice')[1]);lesson.rule='Revised actual lesson';LessonManager.save(ws,lesson)
    grant_from_decision(p,pending,'allow_once')
    with pytest.raises(ValueError,match='CONTENT_CHANGED'):apply_decision(p,pending,'allow_once',storage)
    assert not storage.get_lesson_record('advice')[1].get('check')


def test_zero_checker_generation_preserves_existing_review_versions():
    import json
    from backend.core.knowledge.review import _CONTENT_FIELDS,review_status
    lesson=Lesson(id="legacy",trigger="x",rule="Existing reviewed knowledge")
    raw=lesson.to_dict()
    previous=hashlib.sha256(json.dumps({k:raw.get(k) for k in _CONTENT_FIELDS if k!="checker_revision"},ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    lesson.review={"status":"accepted","kind":"author_supplied","content_version":previous}
    assert review_status(lesson)=="accepted"


@pytest.mark.parametrize("selection,enabled",[(1,True),(2,False)])
def test_canonical_decision_dispatch_commits_only_selected_exact_configuration(tmp_path_factory,selection,enabled):
    from types import SimpleNamespace
    from backend.core.knowledge.checker import request_change
    from backend.core.daemon.dispatch import _cmd_task
    from backend.core.loop.models import ProcessStatus
    ws=tmp_path_factory;p,tools,ctx=setup(ws);storage=get_storage(ws);p.runtime_storage=storage
    request_change(p,storage,ws,{'lesson_id':'advice','expected_version':content_version(storage.get_lesson_record('advice')[1]),'action':'enable','violation_literal':'bad','paths':['cache.py']})
    decision=p.pending_decision['decision_id'];p.status=ProcessStatus.AWAITING_USER
    events=[];checkpoints=[]
    manager=SimpleNamespace(get=lambda pid:p if pid==p.process_id else None,notify_state_changed=lambda _:None)
    context={'apm':manager,'session_store':SimpleNamespace(save_process_checkpoint=lambda process:checkpoints.append(process.status.value))}
    _cmd_task({'action':'decision','process_id':p.process_id,'decision_id':decision,'instruction':f'Choose option {selection}'},SimpleNamespace(workspace_path=ws),SimpleNamespace(name='p'),context,events.append)
    assert events[-1].get('result',{}).get('status')=='completed',events
    assert bool(storage.get_lesson_record('advice')[1].get('check')) is enabled
    assert any(e.get('event')=='user_decision_received' for e in p.session.host_ledger)
    assert checkpoints[0]=='running' and checkpoints[-1]=='completed'
    # Duplicate delivery cannot rerun or reinterpret the concluded decision.
    generation=storage.get_lesson_record('advice')[1].get('checker_revision',0)
    _cmd_task({'action':'decision','process_id':p.process_id,'decision_id':decision,'instruction':'Choose option 1'},SimpleNamespace(workspace_path=ws),SimpleNamespace(name='p'),context,events.append)
    assert storage.get_lesson_record('advice')[1].get('checker_revision',0)==generation


def test_configuration_recovery_does_not_reenable_a_revoked_rule(tmp_path_factory):
    from backend.core.knowledge.checker import request_change,apply_decision
    from backend.core.loop.permission_broker import grant_from_decision
    ws=tmp_path_factory;p,tools,ctx=setup(ws);storage=get_storage(ws)
    request_change(p,storage,ws,{'lesson_id':'advice','expected_version':content_version(storage.get_lesson_record('advice')[1]),'action':'enable','violation_literal':'bad','paths':['cache.py']})
    enable=copy.deepcopy(p.pending_decision);grant=grant_from_decision(p,enable,'allow_once')
    apply_decision(p,enable,'allow_once',storage)
    change=enable['permission_request']['lesson_checker_change']
    assert storage.configure_lesson_checker(change,grant_id=grant['grant_id'])['lesson']['checker_revision']==1
    p.pending_decision=None
    request_change(p,storage,ws,{'lesson_id':'advice','expected_version':content_version(storage.get_lesson_record('advice')[1]),'action':'disable'})
    disable=copy.deepcopy(p.pending_decision);grant_from_decision(p,disable,'allow_once');apply_decision(p,disable,'allow_once',storage)
    # Replay returns current state as an already-applied historical command;
    # it cannot recreate the earlier activation after a newer revocation.
    current=storage.configure_lesson_checker(change,grant_id=grant['grant_id'])
    assert current['status']=='already_applied' and current['applied'] is False
    assert current['lesson']['checker_revision']==2 and not current['lesson'].get('check')


def test_untrusted_or_nonfile_tool_payload_does_not_forge_feedback_exposure(tmp_path_factory):
    p,tools,ctx=setup(tmp_path_factory)
    result=execute(p,tools,ctx,{'path':'cache.py','content':'safe'})
    p.session.append_tool_result(result.formatted,tool_name='custom',tool_call_id='custom',data=result.data,receipt=result.receipt)
    p.session.append_tool_result('bad shape',tool_name='write_file',tool_call_id='bad',data=['arbitrary'])
    observe_provider_input(p,[{'role':'tool','content':result.formatted}])
    assert not any(e.get('event')=='lesson_feedback_provider_input' for e in p.session.host_ledger)


def test_exposed_feedback_is_not_inherited_by_a_new_task(tmp_path_factory):
    p,tools,ctx=setup(tmp_path_factory);args={'path':'cache.py','content':'safe'}
    result=execute(p,tools,ctx,args)
    p.session.append_tool_result(result.formatted,tool_name='write_file',tool_call_id='c0',data=result.data,receipt=result.receipt)
    observe_provider_input(p,[{'role':'tool','content':result.formatted}])
    p.active_task_id='different-task'
    retry=execute(p,tools,ctx,{**args,'lesson_feedback_id':result.data['lesson_feedback']['feedback_id']},1)
    assert retry.is_error and retry.data['error']=='LESSON_FEEDBACK_REQUIRED'
    assert retry.data['lesson_feedback']['continuation_state']=='reference_not_exposed'


@pytest.mark.parametrize("change",["task_id","process_id","tool_name"])
def test_feedback_exposure_requires_matching_runtime_receipt_owner(tmp_path_factory,change):
    p,tools,ctx=setup(tmp_path_factory)
    result=execute(p,tools,ctx,{'path':'cache.py','content':'safe'})
    receipt={**result.receipt,change:'unrelated-owner'}
    p.session.append_tool_result(result.formatted,tool_name='write_file',tool_call_id='mismatch',data=result.data,receipt=receipt)
    observe_provider_input(p,[{'role':'tool','content':result.formatted}])
    assert not any(e.get('event')=='lesson_feedback_provider_input' for e in p.session.host_ledger)
