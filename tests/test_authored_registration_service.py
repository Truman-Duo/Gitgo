"""Registration self-tests have private disposable effects and no file publication."""
import hashlib
from pathlib import Path
from tests.test_file_feedback_contract import context
from backend.core.loop.executor import _build_internal_tools
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.process_tool_runner import ProcessToolRunner, SubprocessResult
from backend.core.loop.permission_broker import create_permission_request, grant_from_decision


def setup(ws):
    code="def run(args):\n    from pathlib import Path\n    Path('self-test.txt').write_text('discard')\n    return {'ok': True}\n"
    (ws/'helper.py').write_text(code,encoding='utf8')
    payload={'name':'registered_emit','description':'Test private registration','authority_mode':'privileged',
        'purpose':'Registration self-test fixture','source_path':'helper.py','source_sha256':hashlib.sha256(code.encode()).hexdigest(),
        'effect':'workspace_write','resources':['capability://registered_emit'],
        'parameters':{'type':'object','properties':{}},'tests':[{'input':{},'expected':{'ok':True}}]}
    ctx=context(ws,['author_tool'])
    tool=_build_internal_tools(ctx.process,{},str(ws))['author_tool']
    create_permission_request(ctx.process,{'purpose':'Explicit synthetic registration test','tool_name':'author_tool','arguments':payload,'resource':'capability://author_tool'},{'author_tool':tool},str(ws))
    assert grant_from_decision(ctx.process,ctx.process.pending_decision,'allow_once')
    ctx.process.pending_decision=None
    return ctx,tool,payload


def test_registration_self_tests_never_write_formal_project(tmp_path_factory,monkeypatch):
    ws=tmp_path_factory
    calls=[]
    def native(self,name,args,**kwargs):
        stage=Path(args['_workspace'])
        assert stage!=ws and not stage.is_relative_to(ws)
        calls.append(stage)
        (stage/'self-test.txt').write_text('discard')
        return SubprocessResult(success=True,data={'ok':True},exit_code=0,started=True,launch_state='started',cleanup={'complete':True,'mode':'protocol-fixture'})
    monkeypatch.setattr(ProcessToolRunner,'run',native)
    monkeypatch.setattr('backend.core.sandbox_access.WorkspaceAccess.prepare',lambda self:self)
    monkeypatch.setattr('backend.core.sandbox_access.WorkspaceAccess.close',lambda self:None)
    ctx,tool,payload=setup(ws)
    result=ToolPipeline().execute({'name':'author_tool','args':payload},tool,ctx,'registration')
    assert not result.is_error,result.formatted
    assert result.data['registered'] and len(calls)==1
    assert not (ws/'self-test.txt').exists() and not calls[0].exists()
    assert result.receipt['runtime_cleanup']['complete']


from tests.test_alpha3_native_source import source_runtime
from tests.test_native_sandbox import isolated_python
from tests.native_source_runtime import workspace_native_launcher


def test_real_registration_discards_files_and_retires_native_access(tmp_path_factory,workspace_native_launcher):
    ws=tmp_path_factory
    ctx,tool,payload=setup(ws)
    result=ToolPipeline().execute({'name':tool.name,'args':payload},tool,ctx,'registration-real')
    assert not result.is_error,result.formatted
    assert result.data['registered'] and result.data['tests_passed']==1
    assert not (ws/'self-test.txt').exists()
    facts=result.data['registration_producers']
    assert len(facts)==1 and facts[0]['launch_state']=='started' and facts[0]['access_retired']
    assert result.receipt['runtime_cleanup']['complete']
    from backend.core.loop.service_runtime import service_runtime
    assert service_runtime() is None


def test_unknown_registration_cleanup_blocks_asset_and_followup(tmp_path_factory,monkeypatch):
    ws=tmp_path_factory
    ctx,tool,payload=setup(ws)
    calls=[]
    def unknown(pipeline,name,args,timeout,**kwargs):
        calls.append(Path(args['_workspace']))
        kwargs['runtime_metadata'].update(launch_state='started',exit_code=0,cleanup={'complete':False,'mode':'fixture_unknown'})
        return {'ok':True}
    monkeypatch.setattr(ToolPipeline,'_execute_isolated',unknown)
    monkeypatch.setattr('backend.core.sandbox_access.WorkspaceAccess.prepare',lambda self:self)
    monkeypatch.setattr('backend.core.sandbox_access.WorkspaceAccess.close',lambda self:None)
    try:
        result=ToolPipeline().execute({'name':tool.name,'args':payload},tool,ctx,'registration-unknown')
        assert result.data['error']=='SANDBOX_CLEANUP_PENDING',result.formatted
        assert not result.data['registered'] and result.receipt['effect_state']=='not_committed'
        assert not result.receipt['runtime_cleanup']['complete']
        assert ctx.process.recovery['resume_forbidden']
        assert len(calls)==1 and calls[0].exists()
        from backend.core.storage import get_storage
        assert get_storage(ws).list_custom_tools()['tools']==[]
    finally:
        # No process/ACL was created by this explicit protocol fault fixture.
        from backend.core.loop.read_approval import _RETAINED_ADMISSIONS
        for owner in _RETAINED_ADMISSIONS.pop((ctx.process.process_id,'registration-unknown',0),[]):
            owner.close()


def test_registration_detects_changed_formal_inputs_before_saving(tmp_path_factory,monkeypatch):
    ws=tmp_path_factory
    (ws/'input.txt').write_text('before')
    ctx,tool,payload=setup(ws)
    def changed(pipeline,name,args,timeout,**kwargs):
        (ws/'input.txt').write_text('external writer')
        kwargs['runtime_metadata'].update(launch_state='started',exit_code=0,cleanup={'complete':True,'mode':'fixture'})
        return {'ok':True}
    monkeypatch.setattr(ToolPipeline,'_execute_isolated',changed)
    monkeypatch.setattr('backend.core.sandbox_access.WorkspaceAccess.prepare',lambda self:self)
    monkeypatch.setattr('backend.core.sandbox_access.WorkspaceAccess.close',lambda self:None)
    result=ToolPipeline().execute({'name':tool.name,'args':payload},tool,ctx,'registration-stale')
    assert result.data['error']=='FILE_CHANGED' and not result.data['registered'],result.formatted
    assert result.receipt['effect_state']=='not_committed'


def test_registration_uses_actual_child_root_after_catalog_was_built(tmp_path_factory,monkeypatch):
    from backend.core.loop.execution_context import ExecutionContext
    base=tmp_path_factory
    main,child=base/'main',base/'child'
    main.mkdir();child.mkdir()
    (main/'helper.py').write_text("def run(args):\n    return {'origin': 'main'}\n",encoding='utf8')
    (child/'helper.py').write_text("def run(args):\n    return {'origin': 'child'}\n",encoding='utf8')
    ctx=context(child,['author_tool'])
    tool=_build_internal_tools(ctx.process,{},str(main))['author_tool']
    def run(self,name,args,**kwargs):
        assert "'child'" in args['_dynamic_spec']['_source']
        assert Path(args['_workspace'])!=child
        return SubprocessResult(success=True,data={'origin':'child'},exit_code=0,started=True,launch_state='started',cleanup={'complete':True,'mode':'fixture'})
    monkeypatch.setattr(ProcessToolRunner,'run',run)
    monkeypatch.setattr('backend.core.sandbox_access.WorkspaceAccess.prepare',lambda self:self)
    monkeypatch.setattr('backend.core.sandbox_access.WorkspaceAccess.close',lambda self:None)
    payload={'name':'child_helper','description':'Use actual child source','source_path':'helper.py','parameters':{},
        'tests':[{'input':{},'expected':{'origin':'child'}}]}
    result=ToolPipeline().execute({'name':tool.name,'args':payload},tool,ctx,'child-registration')
    assert not result.is_error,result.formatted
    assert result.data['registered']


def test_rejected_source_is_known_uncommitted_not_an_ambiguous_save(tmp_path_factory):
    ws = tmp_path_factory
    ctx, tool, payload = setup(ws)
    (ws / "helper.py").write_text("def run(args):\n    raise ValueError('rejected syntax')\n", encoding="utf8")
    payload["authority_mode"] = "pure"
    result = ToolPipeline().execute({"name": tool.name, "args": payload}, tool, ctx, "preflight")
    assert result.is_error and result.data["error"] == "CUSTOM_TOOL_INVALID"
    assert result.receipt["effect_state"] == "not_committed"
    assert not result.receipt.get("native_producers")
