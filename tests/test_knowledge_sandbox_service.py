"""Knowledge observations use trusted state, never native state credentials."""
from pathlib import Path
from types import SimpleNamespace
import os
import pytest
from backend.core.knowledge import harvest
from backend.core.tools.runner import handler_bindings


def test_native_registry_cannot_execute_legacy_harvest():
    assert 'lesson_harvest' not in handler_bindings()


def test_git_discovery_uses_existing_protected_git(monkeypatch,tmp_path_factory):
    from backend.core.loop.worktree import AgentWorktreeManager
    calls=[]; signals=[]
    monkeypatch.setattr(AgentWorktreeManager,'_run',lambda root,args,**kw: calls.append((root,args,kw)) or SimpleNamespace(returncode=0,stdout=b'abc fix: adapter\nM adapter.py\n',stderr=b''))
    monkeypatch.setattr(harvest,'capture_signal',lambda kind,detail,project,**kw: signals.append((kind,detail)))
    result=harvest._harvest_from_git_log(tmp_path_factory,'p')
    assert len(calls)==1 and calls[0][1][:2]==['git','log']
    assert signals[0][0]=='vcs_observation'
    assert result['state']=='completed'


def test_document_discovery_rejects_linked_input(monkeypatch,tmp_path_factory):
    secret=tmp_path_factory/'outside.txt';secret.write_text('synthetic outside content',encoding='utf8')
    ws=tmp_path_factory/'workspace';ws.mkdir();os.link(secret,ws/'CLAUDE.md')
    signals=[]
    monkeypatch.setattr(harvest,'capture_signal',lambda kind,detail,project,**kw: signals.append((kind,detail)))
    result=harvest._harvest_from_claude_md(ws,'p')
    assert not any(kind=='document_observation' for kind,detail in signals)
    assert any(kind=='discovery_unavailable' for kind,detail in signals)
    assert result['state']=='unavailable'


def test_harvest_tool_binds_project_facts_without_native_authority(tmp_path_factory):
    from backend.core.tools.knowledge_service import build_lesson_harvest_tool
    from backend.core.loop.execution_contract import data_broker
    tool=build_lesson_harvest_tool(tmp_path_factory,'p')
    assert tool.validate_execution_contract()==data_broker('knowledge.lesson_harvest')
    assert tool.isolated is False and not tool.read_only
    prepared=tool.prepare_args({'workspace_path':'C:/unrelated','project_name':'forged','tech_stack':'python'})
    assert prepared['workspace_path']==str(tmp_path_factory) and prepared['project_name']=='p'
    assert prepared['tech_stack']=='python'


def test_harvest_pipeline_uses_state_service_and_does_not_approve_knowledge(tmp_path_factory,monkeypatch):
    from backend.core.tools.knowledge_service import build_lesson_harvest_tool
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.history import HistoryManager
    from backend.core.knowledge.manager import LessonManager
    from tests.test_file_feedback_contract import context
    tool=build_lesson_harvest_tool(tmp_path_factory,'p')
    ctx=context(tmp_path_factory,[tool.name])
    from backend.core.loop.models import RingLevel
    ctx.process.ring_level=RingLevel.RING_0
    (tmp_path_factory/'CLAUDE.md').write_text('User prefers short answers. A task has changed.',encoding='utf8')
    def no_native(*a,**kw):raise AssertionError('State discovery cannot enter native process')
    monkeypatch.setattr(ToolPipeline,'_execute_isolated',no_native)
    result=ToolPipeline().execute({'name':tool.name,'args':{'workspace_path':'forged','project_name':'forged'}},tool,ctx,'harvest')
    assert not result.is_error,result.formatted
    assert result.data['knowledge_saved'] is False and result.data['count']==0
    assert result.data['discovery']['document']['state']=='completed'
    assert result.data['discovery']['document']['has_observation'] is True
    assert result.data['status']=='partial'
    assert result.data['discovery']['git_log']['state']=='unavailable'
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        assert any(x['signal_type']=='document_observation' for x in harvest.get_unprocessed_signals('p'))
    assert LessonManager.load_pending(tmp_path_factory,'p')==[]


def test_git_discovery_failure_is_explicit_and_never_retries_unprotected(tmp_path_factory,monkeypatch):
    from backend.core.loop.worktree import AgentWorktreeManager,WorktreeError
    calls=[];signals=[]
    def blocked(*a,**kw):calls.append(a);raise WorktreeError('GIT_BOUNDARY_UNAVAILABLE: no protected backend')
    monkeypatch.setattr(AgentWorktreeManager,'_run',blocked)
    monkeypatch.setattr(harvest,'capture_signal',lambda kind,detail,project,**kw: signals.append((kind,detail)))
    result=harvest._harvest_from_git_log(tmp_path_factory,'p')
    assert len(calls)==1 and result['state']=='unavailable'
    assert signals[0][0]=='discovery_unavailable'


def test_worker_still_cannot_harvest_without_existing_ring_authority(tmp_path_factory,monkeypatch):
    from backend.core.tools.knowledge_service import build_lesson_harvest_tool
    from backend.core.loop.tool_pipeline import ToolPipeline
    from tests.test_file_feedback_contract import context
    tool=build_lesson_harvest_tool(tmp_path_factory,'p')
    ctx=context(tmp_path_factory,[tool.name])
    called=[]
    monkeypatch.setattr('backend.core.loop.tool_wrappers.lesson_harvest',lambda args:called.append(args))
    result=ToolPipeline().execute({'name':tool.name,'args':{}},tool,ctx,'denied')
    assert result.is_error and not called and 'RING_0_REQUIRED' in result.error


@pytest.mark.skipif(os.name!='nt',reason='Windows protected Git backend')
def test_real_git_observation_and_missing_include_fail_closed(tmp_path_factory,monkeypatch):
    import subprocess
    ws=tmp_path_factory/'repo';ws.mkdir()
    def git(*args):
        r=subprocess.run(['git',*args],cwd=ws,capture_output=True,timeout=20)
        assert r.returncode==0,r.stderr
    git('init');git('config','user.name','Synthetic');git('config','user.email','synthetic@invalid.local')
    (ws/'adapter.py').write_text('pass',encoding='utf8')
    git('add','.');git('-c','commit.gpgSign=false','commit','-m','fix: synthetic adapter')
    signals=[]
    monkeypatch.setattr(harvest,'capture_signal',lambda kind,detail,project,**kw:signals.append((kind,detail)))
    report=harvest._harvest_from_git_log(ws,'p')
    assert report['state']=='completed' and signals[0][0]=='vcs_observation'
    assert 'adapter.py' in signals[0][1]['changes']
    signals.clear()
    git('config','include.path',str(tmp_path_factory/'missing.conf'))
    report=harvest._harvest_from_git_log(ws,'p')
    assert report['state']=='unavailable' and [kind for kind,detail in signals]==['discovery_unavailable']


def test_absent_source_document_differs_from_failed_source(tmp_path_factory,monkeypatch):
    signals=[]
    monkeypatch.setattr(harvest,'capture_signal',lambda *args,**kw:signals.append(args))
    assert harvest._harvest_from_claude_md(tmp_path_factory,'p')=={'state':'absent','has_observation':False}
    assert not signals
