"""Regression boundaries for the Agent lifecycle closeout, with real Git/SQLite."""
import json
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.budget import TaskTreeBudget, TaskBudgetExceeded
from backend.core.loop.budget_runtime import reserve_provider_call
from backend.core.loop.llm import StreamCancelledError
from backend.core.loop.test_manifest import run_registered_test
from backend.core.loop.worktree import AgentWorktreeManager, WorktreeError
from backend.core.daemon.task_observations import TaskObservations
from backend.core.storage import StorageRuntime
from .test_agent_dag_worktree import _repo, _process, _git
from .test_task_contract_recovery import _supervisor


def test_registered_test_imports_real_project_package_without_test_path_hacks(tmp_path_factory):
    ws=tmp_path_factory
    (ws/'sample_pkg').mkdir(); (ws/'tests').mkdir()
    (ws/'sample_pkg/__init__.py').write_text('VALUE=42',encoding='utf-8')
    (ws/'tests/test_sample.py').write_text('from sample_pkg import VALUE\ndef test_value(): assert VALUE == 42\n',encoding='utf-8')
    result=run_registered_test(ws,{'test_id':'local-package','target':'tests/test_sample.py','seeds':[1]})
    assert result['passed'],result
    assert 'sys.path' not in (ws/'tests/test_sample.py').read_text(encoding='utf-8')


@pytest.mark.parametrize('reason',['dependency_failure','cancel_waiting','resource_cancel','target_failure'])
def test_all_scheduler_terminal_paths_persist_after_budget_release(tmp_path_factory,reason):
    manager=AgentProcessManager()
    root=_process(manager,task_id='root')
    first=_process(manager,parent_id=root.process_id,task_id='upstream')
    child=_process(manager,parent_id=root.process_id,task_id='child',depends_on=[first.process_id] if reason in {'dependency_failure','cancel_waiting'} else [])
    store=SessionStore(str(tmp_path_factory),state_home=tmp_path_factory/'state')
    q=queue.Queue(); trace=SimpleNamespace(append=lambda event,detail=None:event)
    observe=TaskObservations(root,manager,store,trace,q)
    def terminal():
        assert child.budget_lease['state']=='released'
        observe.persist({'event':'agent_terminal','process_id':child.process_id,'status':child.status.value})
    called=threading.Event()
    def target():
        called.set();raise RuntimeError('intentional target failure')
    if reason=='dependency_failure': first.status=ProcessStatus.FAILED
    if reason in {'cancel_waiting','resource_cancel'}: child.cancellation_event.set()
    try:
        manager.start(child.process_id,target,on_terminal=terminal)
        manager._threads[child.process_id].join(3)
        assert not manager._threads[child.process_id].is_alive()
        assert child.lifecycle_finalized
        durable=store.load_process_state(child.process_id)
        assert durable['process']['status']==child.status.value
        assert durable['runtime_state']['task_budget']['process_leases'][child.process_id]['state']=='released'
        assert durable['runtime_state']['lifecycle_finalized'] is True
        assert q.get_nowait()['event']=='agent_terminal'
        assert called.is_set() is (reason=='target_failure')
    finally:store.close()


def test_checkpoint_failure_does_not_publish_durable_finality(tmp_path_factory):
    manager=AgentProcessManager();root=_process(manager);child=_process(manager,parent_id=root.process_id)
    def terminal():raise RuntimeError('state unavailable')
    manager.start(child.process_id,lambda:(_ for _ in ()).throw(RuntimeError('target failed')),on_terminal=terminal)
    manager._threads[child.process_id].join(3)
    assert child.status is ProcessStatus.FAILED and not child.lifecycle_finalized
    assert child.result['code']=='SESSION_CHECKPOINT_FAILED'
    assert child.result['metadata']['durable_terminal_barrier'] is False
    assert child.budget_lease['state']=='released'


def _blocked_tree():
    manager=AgentProcessManager()
    root=manager.fork(None,'supervisor',ToolRegistry([]),20,RingLevel.RING_0,actor_kind='supervisor',
        task_budget_limits={'max_provider_calls':10,'max_output_tokens':10000,'verification_reserve_ratio':0.2,
                            'root_coordination_calls':0,'max_seconds':20})
    child=manager.fork(root.process_id,'executor',ToolRegistry([]),10,RingLevel.RING_3,
                       child_budget_request={'provider_calls':3,'output_tokens':1000})
    for _ in range(5):root.task_budget.begin_provider_call(root.process_id)
    return manager,root,child


@pytest.mark.parametrize('action',['refund','cancel','fully_spent','deadline'])
def test_root_waits_for_escrow_without_billing_retry_or_stealing_it(action):
    manager,root,child=_blocked_tree();release=threading.Event();started=threading.Event();out={}
    def work():
        started.set();release.wait(3)
        for _ in range(3 if action=='fully_spent' else 1):child.task_budget.begin_provider_call(child.process_id)
        child.status=ProcessStatus.COMPLETED
        return {'status':'completed'}
    manager.start(child.process_id,work);assert started.wait(1)
    def reserve():
        try:out['index']=reserve_provider_call(root)
        except Exception as exc:out['error']=exc
    thread=threading.Thread(target=reserve);thread.start()
    time.sleep(0.06)
    assert thread.is_alive() and root.task_budget.snapshot()['used']['provider_calls']==5
    try:
        if action=='cancel':root.cancellation_event.set()
        elif action=='deadline':
            root.task_budget.deadline_monotonic=time.monotonic()-1
        else:release.set()
        thread.join(2)
        assert not thread.is_alive()
        if action=='refund':assert out['index']==7
        elif action=='cancel':assert isinstance(out['error'],StreamCancelledError)
        else:assert isinstance(out['error'],TaskBudgetExceeded)
    finally:
        release.set();manager._threads[child.process_id].join(3);thread.join(1)


def test_child_admission_preserves_finite_root_coordination_floor():
    b=TaskTreeBudget.create('t',{'max_provider_calls':10,'verification_reserve_ratio':0.2,'max_output_tokens':10000})
    b.register_process('root',parent_id=None,actor_kind='supervisor',max_steps=20)
    with pytest.raises(TaskBudgetExceeded):
        b.register_process('too-much',parent_id='root',actor_kind='worker',max_steps=5,request={'provider_calls':7})
    assert b.snapshot()['used']['provider_calls']==0
    assert b.decision_card('root')['provider_calls']['work_available_for_delegation']==6
    b.register_process('bounded',parent_id='root',actor_kind='worker',max_steps=5,request={'provider_calls':6})
    b.begin_provider_call('root');b.begin_provider_call('root')
    assert b.snapshot()['used']['provider_calls']==2
    restored=TaskTreeBudget.from_snapshot(b.snapshot())
    assert restored.limits.root_coordination_calls==2


def test_dag_budget_failure_preflights_before_starting_any_node(tmp_path_factory):
    root,tools=_supervisor(tmp_path_factory);manager=root._manager
    root.task_budget=TaskTreeBudget.create(root.active_task_id,{'max_provider_calls':10,'verification_reserve_ratio':0.2})
    root.task_budget.register_process(root.process_id,parent_id=None,actor_kind='supervisor',max_steps=10)
    nodes=[{'node_id':str(i),'task_description':'inspect one file','target_files':['file.txt'],
            'acceptance_criteria':['report actual facts'],'depends_on':[],'task_kind':'answer',
            'capability_profile_id':'governance.observe','budget_request':{'provider_calls':4,'output_tokens':512}}
           for i in range(2)]
    with patch.object(manager,'start') as start:
        response=tools['delegate_task_dag']({'nodes':nodes})
    assert not response['delegated'] and response['execution_state']=='not_started'
    start.assert_not_called();assert not root.child_ids


def _promotion_tree(tmp):
    repo=_repo(tmp);storage=StorageRuntime(repo,state_home=tmp/'state')
    manager=AgentProcessManager(worktree_manager=AgentWorktreeManager(repo,storage))
    root=_process(manager,task_id='root')
    a=_process(manager,parent_id=root.process_id,task_id='a')
    path=Path(manager.materialize_process_worktree(a));(path/'app.txt').write_text('upstream\n',encoding='utf-8')
    a.status=ProcessStatus.COMPLETED;manager.seal_process_worktree(a)
    b=_process(manager,parent_id=root.process_id,task_id='b',depends_on=[a.process_id])
    path=Path(manager.materialize_process_worktree(b));(path/'result.txt').write_text('consumer\n',encoding='utf-8')
    b.status=ProcessStatus.COMPLETED;manager.seal_process_worktree(b)
    for child in [a,b]:root.child_reviews[child.process_id]={'verdict':'approved'}
    return repo,storage,manager,root,a,b


def _cleanup(storage,manager,*children):
    for child in children:manager.dispose_process_worktree(child)
    storage.close()


def test_incremental_and_repeated_promotion_preserve_accepted_predecessors_and_index(tmp_path_factory):
    repo,store,m,root,a,b=_promotion_tree(tmp_path_factory)
    try:
        (repo/'notes.md').write_text('user unrelated\n',encoding='utf-8');_git(repo,'add','notes.md')
        index=_git(repo,'diff','--cached')
        first=m.promote_process_results(root,[a.process_id]);assert first['changed_files']==['app.txt']
        repeated=m.promote_process_results(root,[a.process_id]);assert repeated['changed_files']==[]
        second=m.promote_process_results(root,[b.process_id]);assert second['changed_files']==['result.txt']
        assert second['already_promoted']==[a.worktree['own_commit']]
        assert (repo/'app.txt').read_text(encoding='utf-8')=='upstream\n'
        assert (repo/'result.txt').read_text(encoding='utf-8')=='consumer\n'
        assert _git(repo,'diff','--cached')==index
        # A new service instance uses the same authoritative promotion head.
        fresh=AgentWorktreeManager(repo,store)
        again=fresh.promote(snapshot_commit=a.worktree['snapshot_commit'],
            ordered_commits=[a.worktree['own_commit'],b.worktree['own_commit']],promotion_id=root.active_task_id)
        assert again['promoted'] and again['changed_files']==[]
    finally:_cleanup(store,m,a,b)


@pytest.mark.parametrize('stage',['before_apply','after_apply','drift_after_apply'])
def test_interrupted_promotion_reconciles_only_recorded_versions(tmp_path_factory,stage):
    repo,store,m,root,a,b=_promotion_tree(tmp_path_factory)
    try:
        if stage=='before_apply':
            original=m.worktree_manager._git_input
            def fail_root(path,data,*args):
                if path==repo:raise WorktreeError('injected before root effect')
                return original(path,data,*args)
            with patch.object(m.worktree_manager,'_git_input',side_effect=fail_root):
                with pytest.raises(WorktreeError):m.promote_process_results(root,[a.process_id])
        else:
            original=store.commit_state_revision;count=[0]
            def fail_ack(*args,**kwargs):
                count[0]+=1
                if count[0]==2:raise RuntimeError('injected metadata acknowledgement failure')
                return original(*args,**kwargs)
            with patch.object(store,'commit_state_revision',side_effect=fail_ack):
                with pytest.raises(WorktreeError,match='METADATA_PENDING'):m.promote_process_results(root,[a.process_id])
            assert (repo/'app.txt').read_text(encoding='utf-8')=='upstream\n'
        if stage=='drift_after_apply':
            (repo/'app.txt').write_text('user later edit\n',encoding='utf-8')
            with pytest.raises(WorktreeError,match='AMBIGUOUS'):m.promote_process_results(root,[a.process_id])
            assert (repo/'app.txt').read_text(encoding='utf-8')=='user later edit\n'
        else:
            result=m.promote_process_results(root,[a.process_id])
            assert result['promoted'] and a.worktree['promoted']
            assert result['reconciled_effect'] is (stage=='after_apply')
    finally:_cleanup(store,m,a,b)


def test_accepted_promotion_drift_and_unattributed_same_content_are_rejected(tmp_path_factory):
    repo,store,m,root,a,b=_promotion_tree(tmp_path_factory)
    try:
        m.promote_process_results(root,[a.process_id])
        # Same bytes in a previously absent file are not an earlier promotion.
        (repo/'result.txt').write_text('consumer\n',encoding='utf-8')
        with pytest.raises(WorktreeError,match='workspace changed'):m.promote_process_results(root,[b.process_id])
        (repo/'result.txt').unlink();(repo/'app.txt').write_text('user edit\n',encoding='utf-8')
        with pytest.raises(WorktreeError,match='workspace changed'):m.promote_process_results(root,[b.process_id])
        assert (repo/'app.txt').read_text(encoding='utf-8')=='user edit\n'
    finally:_cleanup(store,m,a,b)


def test_terminal_checkpoint_must_commit_before_live_finality_is_visible(tmp_path_factory):
    manager=AgentProcessManager();root=_process(manager);child=_process(manager,parent_id=root.process_id)
    store=SessionStore(str(tmp_path_factory),state_home=tmp_path_factory/'state')
    started=threading.Event();release=threading.Event()
    original=store._storage.save_agent_checkpoint
    def blocked(snapshot):
        assert snapshot['runtime_state']['lifecycle_finalized'] is True
        assert not child.lifecycle_finalized
        started.set();assert release.wait(2)
        return original(snapshot)
    observe=TaskObservations(root,manager,store,SimpleNamespace(append=lambda event,detail=None:event),queue.Queue())
    def target():child.status=ProcessStatus.COMPLETED;return {'status':'completed'}
    try:
        with patch.object(store._storage,'save_agent_checkpoint',side_effect=blocked):
            manager.start(child.process_id,target,on_terminal=lambda:observe.persist({'event':'agent_terminal','process_id':child.process_id}))
            assert started.wait(2)
            assert not manager.wait_many([child.process_id],timeout=0)['all_terminal']
            release.set();manager._threads[child.process_id].join(3)
        assert child.lifecycle_finalized
        assert store.load_process_state(child.process_id)['runtime_state']['lifecycle_finalized']
    finally:release.set();store.close()


def test_interface_declarations_admit_the_complete_four_edge_graph(tmp_path_factory):
    ws=tmp_path_factory
    for name,func in [('api','normalize'),('upper','upper'),('lower','lower'),('bundle','both')]:
        (ws/(name+'.py')).write_text(f'def {func}(text): return text\n',encoding='utf-8')
    root,tools=_supervisor(ws);m=root._manager
    nodes=[]
    for name,ins,out in [('api',[],'api.py:normalize'),('upper',['api.py:normalize'],'upper.py:upper'),
                         ('lower',['api.py:normalize'],'lower.py:lower'),('bundle',['upper.py:upper','lower.py:lower'],'bundle.py:both')]:
        nodes.append({'node_id':name,'task_description':'validate declared interface','target_files':[name+'.py'],
                      'acceptance_criteria':['interface matches'],'depends_on':[],'input_interfaces':ins,'output_interfaces':[out]})
    with patch.object(m,'start'):
        response=tools['delegate_task_dag']({'nodes':nodes})
    assert response['delegated'],response
    edges={(p.process_id,dep) for p in m.children_of(root.process_id) for dep in p.depends_on}
    assert len(edges)==4
    assert sorted(len(p.depends_on) for p in m.children_of(root.process_id))==[0,1,1,2]



def test_cold_recovery_uses_latest_shared_budget_not_stale_root_projection(tmp_path_factory):
    from backend.core.loop.recovery import restore_incomplete_processes
    m=AgentProcessManager();root=_process(m,task_id='budget-root')
    child=_process(m,parent_id=root.process_id,task_id='budget-child')
    store=SessionStore(str(tmp_path_factory),state_home=tmp_path_factory/'state')
    try:
        store.save_process_checkpoint(root)
        old=store.load_process_state(root.process_id)['runtime_state']['task_budget']
        root.task_budget.begin_provider_call(child.process_id)
        root.task_budget.begin_provider_call(child.process_id)
        child.status=ProcessStatus.COMPLETED
        root.task_budget.release_process(child.process_id,status='completed')
        store.save_process_checkpoint(child,lifecycle_finalized=True)
        restored=AgentProcessManager()
        restore_incomplete_processes(store,restored,tmp_path_factory,include_process_ids=[root.process_id])
        budget=restored.get(root.process_id).task_budget.snapshot()
        assert budget['revision']>old['revision']
        assert budget['used']['provider_calls']==2
        assert budget['process_leases'][child.process_id]['state']=='released'
        assert restored.get(child.process_id).task_budget is restored.get(root.process_id).task_budget
        assert not restored._threads
    finally:store.close()



def test_denied_capacity_does_not_extend_deadline_like_actual_provider_work():
    with patch('backend.core.loop.budget.time.monotonic',return_value=100.0) as clock:
        b=TaskTreeBudget.create('wait',{'max_provider_calls':10,'verification_reserve_ratio':0.2,
            'root_coordination_calls':0,'max_seconds':1800,'initial_seconds':300})
        b.register_process('root',parent_id=None,actor_kind='supervisor',max_steps=20)
        b.register_process('child',parent_id='root',actor_kind='worker',max_steps=10,
                           request={'provider_calls':3})
        for _ in range(5):b.begin_provider_call('root')
        before=b.snapshot()['deadline']
        clock.return_value=120.0
        with pytest.raises(TaskBudgetExceeded):b.begin_provider_call('root')
        assert b.snapshot()['deadline']==before


def test_leftover_integration_checkout_cannot_block_or_be_deleted_by_retry(tmp_path_factory):
    repo,store,m,root,a,b=_promotion_tree(tmp_path_factory)
    service=m.worktree_manager
    leftover=service._target_path(f"promote-{service._ref_id(root.active_task_id)}")
    service._add_detached(leftover,a.worktree['snapshot_commit'])
    try:
        marker=leftover/'unacknowledged.txt'
        marker.write_text('preserve crash evidence\n',encoding='utf-8')
        result=m.promote_process_results(root,[a.process_id])
        assert result['promoted'] and result['changed_files']==['app.txt']
        assert marker.read_text(encoding='utf-8')=='preserve crash evidence\n'
    finally:
        service._remove_path(leftover)
        _cleanup(store,m,a,b)


@pytest.mark.parametrize('during_wait',[False,True])
def test_wait_agents_returns_owned_coordination_action_without_accepting_it(tmp_path_factory,during_wait):
    from backend.core.loop.coordination import publish_coordination_event, resolve_coordination_event
    root,tools=_supervisor(tmp_path_factory);manager=root._manager
    source=_process(manager,parent_id=root.process_id)
    child=_process(manager,parent_id=root.process_id,depends_on=[source.process_id])
    other=_process(manager,parent_id=root.process_id)
    child.status=other.status=ProcessStatus.WAITING
    for item in [source,child,other]:
        root.register_child_contract(item.process_id, {'target_files':[]})
    def publish():
        return publish_coordination_event(manager,source,kind='dependency_warning',
            summary='Owner must decide the changed boundary',affected_process_ids=[child.process_id],
            requires_supervisor_action=True,block_affected=True)
    done=threading.Event();result=[]
    if not during_wait:event=publish()
    def wait():
        result.append(tools['wait_agents']({'process_ids':[child.process_id],'timeout':10}))
        done.set()
    thread=threading.Thread(target=wait,daemon=True);thread.start()
    if during_wait:
        assert not done.wait(.05)
        event=publish()
    assert done.wait(2);thread.join(2)
    response=result[0]
    assert not response['all_terminal']
    assert response['interruption']['event_ids']==[event['coordination_event_id']]
    assert child.status==ProcessStatus.WAITING
    assert child.read_context_snapshot()[0]['coordination_blocks']==[event['coordination_event_id']]
    # This exact pending action must not interrupt a wait for unrelated work.
    unrelated=tools['wait_agents']({'process_ids':[other.process_id],'timeout':.01})
    assert unrelated['interruption'] is None
    resolve_coordination_event(manager,root,event['coordination_event_id'],disposition='acknowledge',note='Owner considered this boundary')
    child.status=ProcessStatus.COMPLETED
    completed=tools['wait_agents']({'process_ids':[child.process_id],'timeout':.01})
    assert completed['all_terminal'] and completed['interruption'] is None


@pytest.mark.parametrize('status',[ProcessStatus.WAITING,ProcessStatus.RUNNING,ProcessStatus.FAILED,ProcessStatus.CANCELLED,ProcessStatus.COMPLETED])
def test_incomplete_child_artifact_is_business_feedback_and_does_not_rollback_batch(tmp_path_factory,status):
    from unittest.mock import MagicMock
    from backend.core.loop.agent_tool import AgentTool,ToolEffect
    from backend.core.loop.tool_execution import ToolExecution
    root,tools=_supervisor(tmp_path_factory);manager=root._manager
    child=_process(manager,parent_id=root.process_id);child.status=status
    root.register_child_contract(child.process_id,{'target_files':['result.txt']})
    # A running wrapper must persist/seal even after the inner Agent says completed.
    if status==ProcessStatus.COMPLETED:manager._threads[child.process_id]=threading.current_thread()
    from backend.core.loop.execution_context import ExecutionContext
    ctx=ExecutionContext(process=root,session=root.session,workspace_path=str(tmp_path_factory))
    called=[]
    follow=AgentTool(name='inspect_other',description='read',parameters={'type':'object'},
        execute=lambda args:called.append('read') or {'observed':True},read_only=True,effect=ToolEffect.READ)
    catalog={'read_child_artifact':tools['read_child_artifact'],'inspect_other':follow}
    ctx.artifacts={'tool_catalog':catalog}
    execution=ToolExecution(execution_id='child-readiness',ctx=ctx,tool_calls=[
        {'name':'read_child_artifact','args':{'process_id':child.process_id,'path':'result.txt'}},
        {'name':'inspect_other','args':{}}])
    execution.begin();results=execution.execute_batch(catalog)
    assert not execution._rolled_back
    assert results[0].diagnostics['nature']=='business'
    assert results[0].diagnostics['error_info']['catalog_id']=='GITGO-E3118'
    assert called==['read'] and not results[1].is_error
