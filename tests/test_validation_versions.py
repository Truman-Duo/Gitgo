"""Version-evidence regressions through production tools and lifecycle boundaries.

Regression tests require invalidation; no semantic model responses are trusted as authority.
No providers or real user projects are used. Artifacts and reports live outside .gitgo.
"""
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tests.test_alpha3_native_source import isolated_python, source_runtime, source_native_runner

from backend.core.loop.execution_contract import data_broker
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.completion_protocol import CompletionClaim, HostCompletionEvaluator, HostCompletionEvidence
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.event_bus import EventBus
from backend.core.loop.executor import _build_internal_tools
from backend.core.loop.models import ProcessStatus
from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
from tests.registered_test_fixture import run_registered_test
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry
from backend.core.storage import close_owned_storage
from backend.core.tools.catalog import build_workspace_tools
from tests.test_task_contract_recovery import _supervisor


@pytest.fixture
def case(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory
    project = tmp_path / 'test-project'
    project.mkdir()
    close_owned_storage()
    monkeypatch.setenv('GITGO_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('GITGO_CONFIG_PATH', str(tmp_path / 'config.json'))
    p, _ = _supervisor(project)
    p.task_kind = 'action'
    p.tool_registry = ToolRegistry(['write_file', 'run_test', 'read_file', 'request_review'])
    catalog = build_workspace_tools(project)
    catalog['run_test'] = AgentTool(name='run_test', description='Run the product registered test implementation',
        parameters={'type':'object','properties':{'test_id':{'type':'string'},'target':{'type':'string'}},
                    'required':['test_id','target']},
        execute=lambda a: run_registered_test(project, a), read_only=False, effect=ToolEffect.PROCESS,
        execution_contract=data_broker('test.validation_registered_test'))
    ctx = ExecutionContext(process=p, session=p.session, workspace_path=str(project),
        event_bus=EventBus(), cancellation=p.cancellation_event)
    def execute(name, args, *, expected_error=False):
        result = ToolPipeline().execute({'name':name,'args':args}, catalog[name], ctx,
            'audit-'+str(len(p.tool_receipts)), 0)
        assert result.is_error is expected_error, (name, result.error, result.diagnostics)
        p.tool_receipts.append(result.receipt)
        p.session.append_tool_result(result.formatted, tool_name=name, data=result.data, receipt=result.receipt)
        return result
    yield project, p, catalog, ctx, execute
    close_owned_storage()

def write(execute, project, name, content):
    args={'path':name,'content':content}
    target=project/name
    if target.exists():
        args.update(create_only=False,expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    return execute('write_file',args)

def claim(p):
    p.completion_claim=CompletionClaim.from_args({'result':'The task is complete','verification':[]},step=p.steps_used)
    return HostCompletionEvaluator.evaluate(p,'TASK_COMPLETE',[])

def test_registered_test_then_edit_invalidates(case):
    project,p,_,_,execute=case
    from backend.core.loop.task_contract import validate_contract_proposal, publish_contract
    proposal=validate_contract_proposal({'goal':'Implement and verify solver',
        'execution_mode':'self_execute','acceptance_criteria':['unit:solver passes']},str(project))
    assert 'engineering_workflow' not in proposal
    publish_contract(p,proposal)
    write(execute,project,'solver.py','VALUE = 1\n')
    write(execute,project,'test_solver.py','from solver import VALUE\ndef test_value():\n    assert VALUE == 1\n')
    p.required_test_ids=['unit:solver']
    args={'test_id':'unit:solver','target':'test_solver.py'}
    green=execute('run_test',args)
    assert green.data['passed'] is True,green.data
    assert 'files' not in green.receipt['validation_version']
    assert 'files' not in green.data['validation_version']
    assert green.data['validation_version']['file_count'] == 2
    assert claim(p).allowed
    write(execute,project,'solver.py','VALUE = 2000\n')
    stale=claim(p)
    evidence=HostCompletionEvidence.collect(p)
    assert not stale.allowed and not evidence.factual_ready
    red=execute('run_test',args,expected_error=True)
    assert red.data['passed'] is False,red.data
    assert not claim(p).allowed

@pytest.mark.parametrize("during_review", [False, True])
def test_independent_review_then_edit_invalidates(case, during_review):
    project,p,catalog,_,execute=case
    write(execute,project,'app.txt','reviewed version\n')
    p.review_required=True
    assert not claim(p).allowed
    manager=p._manager
    tools=_build_internal_tools(p,{},str(project),object(),SimpleNamespace(_executors=catalog))
    captured=[]
    with patch.object(manager,'start',side_effect=lambda pid,target,**kw: captured.append(target)):
        request=tools['request_review']({'process_id':p.process_id,'focus':'Review app.txt'})
    reviewer=manager.get(request['reviewer_process_id'])
    def finish_review(**kw):
        if during_review:
            write(execute,project,"app.txt","changed during review\n")
        child_tools=_build_internal_tools(reviewer,{},str(project),object(),SimpleNamespace(_executors=catalog))
        result=child_tools['complete_review']({'verdict':'approved','summary':'The current file is correct',
            'findings':[],'evidence':[]})
        assert result['accepted']
        reviewer.status=ProcessStatus.COMPLETED
        reviewer.result={'status':'completed'}
        return reviewer.result
    with patch('backend.core.loop.protocol_tools.child_execution._run_agent_step',side_effect=finish_review):
        captured[0]()
    if during_review:
        assert reviewer.process_id not in p.review_approvals
        assert reviewer.review_claim["applicability"] == "needs_recheck"
        assert not claim(p).allowed
        return
    assert reviewer.process_id in p.review_approvals
    assert claim(p).allowed
    write(execute,project,'app.txt','changed after independent review\n')
    stale=claim(p)
    assert not stale.allowed

def test_permission_rejects_changed_args_and_wrong_task(case):
    project,p,catalog,ctx,execute=case
    from backend.core.loop.agent_tool import ApprovalMode
    calls=[]
    tool=AgentTool(name='audit_sensitive',description='Harmless in-memory approval fixture',
        parameters={'type':'object','properties':{'value':{'type':'string'}},'required':['value']},
        execute=lambda a: calls.append(a['value']) or {'ok':True},
        read_only=False,effect=ToolEffect.PROCESS,approval=ApprovalMode.ASK,approval_per_invocation=True,
        execution_contract=data_broker('test.validation_approval'))
    p.tool_registry.add_scoped('audit_sensitive')
    args={'value':'original'}
    create_permission_request(p,{'tool_name':tool.name,'arguments':args,'purpose':'observe the exact grant boundary',
        'resource':'capability://audit_sensitive'},{tool.name:tool},str(project))
    grant_from_decision(p,p.pending_decision,'allow_once')
    p.pending_decision=None
    def invoke(a):
        return ToolPipeline().execute({'name':tool.name,'args':a},tool,ctx,'permission-probe',0)
    changed=invoke({'value':'different'})
    assert changed.is_error and not calls
    old=p.active_task_id
    p.active_task_id='another-task'
    wrong=invoke(args)
    assert wrong.is_error and not calls
    p.active_task_id=old
    first=invoke(args)
    assert not first.is_error and calls==['original']
    spent=invoke(args)
    assert spent.is_error and calls==['original']

@pytest.mark.parametrize("pin", [False, True])
def test_promotion_rechecks_explicit_read_dependency(case, pin, source_native_runner):
    from tests.test_agent_delivery_continuation import setup, delegate, cleanup
    from tests.test_agent_dag_worktree import _git
    project,_,_,_,_=case
    repo,storage,root,manager,tools=setup(project)
    try:
        (repo/'settings.txt').write_text('old configuration\n',encoding='utf-8')
        _git(repo,'add','settings.txt'); _git(repo,'commit','-m','initial configuration')
        with patch.object(manager,'start'):
            response=delegate(tools)
        child=manager.get(response['process_id'])
        child_root=Path(manager.materialize_process_worktree(child))
        catalog=build_workspace_tools(child_root)
        ctx=ExecutionContext(process=child,session=child.session,workspace_path=str(child_root),
            event_bus=EventBus(),cancellation=child.cancellation_event)
        def invoke(name,args):
            result=ToolPipeline().execute({'name':name,'args':args},catalog[name],ctx,'child-audit-'+str(len(child.tool_receipts)),0)
            assert not result.is_error,(result.error,result.diagnostics)
            child.tool_receipts.append(result.receipt)
            child.session.append_tool_result(result.formatted,tool_name=name,data=result.data,receipt=result.receipt)
            return result
        with source_native_runner(child_root):
            read=invoke('read_file',{'path':'settings.txt','bind_dependency':pin})
        assert read.data['content'].startswith('old configuration')
        invoke('write_file',{'path':'app.txt','content':'generated using old configuration\n','create_only':False,
            'expected_sha256':hashlib.sha256((child_root/'app.txt').read_bytes()).hexdigest()})
        child.status=ProcessStatus.COMPLETED
        child.result={'status':'completed','response':'Generated from settings.txt'}
        manager.seal_process_worktree(child)
        review=tools['review_child_outcome']({'process_id':child.process_id,'verdict':'approved',
            'summary':'Output matches the configuration read by the child'})
        assert review['accepted'],review
        (repo/'settings.txt').write_text('new incompatible configuration\n',encoding='utf-8')
        from backend.core.loop.worktree import WorktreeError
        if pin:
            assert storage.load_worktree_state(child.process_id)['input_dependencies'][0]['path'] == 'settings.txt'
            with pytest.raises(WorktreeError,match='VALIDATION_INPUT_CHANGED'):
                manager.promote_process_results(root,[child.process_id])
            assert (repo/'app.txt').read_text() == 'base\n'
        else:
            assert manager.promote_process_results(root,[child.process_id])['promoted']
        assert (repo/'settings.txt').read_text().startswith('new incompatible')
    finally:
        cleanup(manager,storage)



def test_input_scope_detects_external_edits_additions_and_excludes_runtime(tmp_path_factory):
    from backend.core.loop.validation_evidence import workspace_version, assess_version
    root=tmp_path_factory
    (root/'source.py').write_text('original')
    before=workspace_version(root)
    (root/'.gitgo').mkdir(); (root/'.gitgo'/'runtime.json').write_text('mutable runtime')
    assert assess_version(before,root)['state']=='current'
    scoped=workspace_version(root,input_files=['source.py'])
    (root/'unrelated.txt').write_text('unrelated')
    assert assess_version(scoped,root)['state']=='current'
    assert assess_version(before,root)['state']=='needs_recheck'
    old_stat=(root/'source.py').stat()
    (root/'source.py').write_text('changed!')
    os.utime(root/'source.py',ns=(old_stat.st_atime_ns,old_stat.st_mtime_ns))
    assert assess_version(scoped,root)['state']=='needs_recheck'
    (root/'source.py').unlink()
    assert assess_version(scoped,root)['state']=='needs_recheck'


def test_unknown_and_oversized_scopes_never_claim_current(tmp_path_factory,monkeypatch):
    from backend.core.loop import validation_evidence as versions
    root=tmp_path_factory
    (root/'a.py').write_text('x')
    monkeypatch.setattr(versions,'MAX_BYTES',0)
    failed=versions.workspace_version(root)
    assert failed['state']=='unavailable'
    assert versions.assess_version(failed,root)['state']=='needs_recheck'
    assert versions.workspace_version('')['state']=='unavailable'
    assert versions.assess_version(None,root)['state']=='needs_recheck'


def test_seed_results_do_not_merge_across_input_versions(tmp_path_factory):
    from backend.core.loop.validation_evidence import workspace_version
    from backend.core.loop.test_manifest import TestManifest,TestRecord,SeedResult
    root=tmp_path_factory; source=root/'code.py';source.write_text('one')
    manifest=TestManifest(root)
    manifest.register(TestRecord('unit','code.py',[1],[SeedResult(1,0,0,'passed')],validation_version=workspace_version(root)))
    source.write_text('two')
    assert not TestManifest.load(root).evaluate_required(['unit'])[0]
    manifest.register(TestRecord('unit','code.py',[2],[SeedResult(2,0,0,'passed')],validation_version=workspace_version(root)))
    assert TestManifest.load(root).records['unit'].seeds==[2]
    assert TestManifest.load(root).evaluate_required(['unit'])[0]


def test_old_bare_review_ids_and_wrong_task_binding_are_not_approval(case):
    from backend.core.loop.validation_evidence import review_target_version,has_current_review
    root,p,_,_,_=case
    p.review_approvals=['historical-reviewer']
    assert not has_current_review(p)
    p.runtime_preferences['review_evidence']={'historical-reviewer':review_target_version(p)}
    assert has_current_review(p)
    p.active_task_id='different-task'
    assert not has_current_review(p)


def test_required_test_cannot_borrow_another_target_run(case):
    root,p,_,_,execute=case
    write(execute,root,'test_one.py','def test_one(): assert True\n')
    write(execute,root,'test_two.py','def test_two(): assert True\n')
    p.required_test_ids=['required']
    execute('run_test',{'test_id':'required','target':'test_one.py'})
    assert claim(p).allowed
    # A separate caller runs another target under the same manifest ID. Its
    # record is genuine but is not this process's invocation evidence.
    assert run_registered_test(root,{'test_id':'required','target':'test_two.py'})['passed']
    assert not claim(p).allowed


def test_pinned_dag_input_is_checked_against_integrated_predecessor(case, source_native_runner):
    from tests.test_agent_delivery_continuation import setup,delegate,cleanup
    from tests.test_agent_dag_worktree import _git
    project,_,_,_,_=case
    repo,storage,root,manager,tools=setup(project)
    try:
        (repo/'settings.txt').write_text('original\n',encoding='utf-8')
        _git(repo,'add','settings.txt');_git(repo,'commit','-m','initial input')
        def start(**args):
            with patch.object(manager,'start'):
                request=delegate(tools,**args)
            child=manager.get(request['process_id'])
            folder=Path(manager.materialize_process_worktree(child))
            catalog=build_workspace_tools(folder)
            context=ExecutionContext(process=child,session=child.session,workspace_path=str(folder),
                event_bus=EventBus(),cancellation=child.cancellation_event)
            def invoke(name,args):
                result=ToolPipeline().execute({'name':name,'args':args},catalog[name],context,'dag-'+str(len(child.tool_receipts)),0)
                assert not result.is_error,(result.error,result.diagnostics)
                child.tool_receipts.append(result.receipt)
                return result
            return child,folder,invoke
        def seal(child):
            child.status=ProcessStatus.COMPLETED;child.result={'status':'completed'}
            manager.seal_process_worktree(child)
            assert tools['review_child_outcome']({'process_id':child.process_id,'verdict':'approved','summary':'Checked output'})['accepted']
        upstream,upfolder,upcall=start(target_files=['settings.txt'])
        upcall('write_file',{'path':'settings.txt','content':'new upstream\n','create_only':False,
            'expected_sha256':hashlib.sha256((upfolder/'settings.txt').read_bytes()).hexdigest()})
        seal(upstream)
        child,folder,invoke=start(depends_on=[upstream.process_id],target_files=['app.txt'])
        with source_native_runner(folder):
            assert invoke('read_file',{'path':'settings.txt','bind_dependency':True}).data['content']=='new upstream'
        invoke('write_file',{'path':'app.txt','content':'generated from new upstream\n','create_only':False,
            'expected_sha256':hashlib.sha256((folder/'app.txt').read_bytes()).hexdigest()})
        seal(child)
        assert (repo/'settings.txt').read_text().startswith('original')
        assert manager.promote_process_results(root,[child.process_id])['promoted']
        assert (repo/'settings.txt').read_text().startswith('new upstream')
        assert (repo/'app.txt').read_text().startswith('generated from new upstream')
    finally:
        cleanup(manager,storage)


def test_review_binding_survives_checkpoint_and_still_rechecks_files(case):
    from backend.core.loop.validation_evidence import review_target_version,has_current_review
    from backend.core.loop.manager import SessionStore
    root,p,_,_,execute=case
    write(execute,root,'app.txt','reviewed\n')
    p.review_approvals=['bound-review']
    p.runtime_preferences['review_evidence']={'bound-review':review_target_version(p)}
    store=SessionStore(str(root),state_home=root/'.gitgo'/'state')
    try:
        store.save_process_checkpoint(p)
        saved=store.load_process_state(p.process_id)['runtime_state']
        p.review_approvals=saved['review_approvals']
        p.runtime_preferences=saved['runtime_preferences']
        assert has_current_review(p)
        (root/'app.txt').write_text('external change\n')
        assert not has_current_review(p)
    finally:
        store.close()


def test_later_failed_test_supersedes_green_without_source_changes(case):
    from backend.core.loop.validation_evidence import test_receipt_assessment as assess_test
    root,p,_,_,execute=case
    write(execute,root,'test_sample.py','def test_ok(): assert True\n')
    green=execute('run_test',{'test_id':'unit','target':'test_sample.py'})
    assert assess_test(p,green.receipt)['state']=='current'
    # Unit fixture for a later failing invocation. Content identity alone must
    # not turn this previous success back into the current test observation.
    p.tool_receipts.append({**green.receipt,'receipt_id':'later-failed','succeeded':False,'test_passed':False})
    assert assess_test(p,green.receipt)['reason']=='superseded_test_run'
