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
from tests.registered_test_fixture import run_registered_test
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


@pytest.mark.parametrize("during_checkpoint", [True, False])
def test_cancel_while_parked_child_thread_is_retiring_finalizes_budget_and_checkpoint(tmp_path_factory, during_checkpoint):
    manager = AgentProcessManager()
    root = _process(manager, task_id="root")
    child = _process(manager, parent_id=root.process_id, task_id="child")
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / "state")
    manager.session_store = store
    reached_boundary = threading.Event()
    finish_closeout = threading.Event()

    def park():
        child.status = ProcessStatus.AWAITING_USER
        return {"status": "awaiting_user"}

    def pause():
        reached_boundary.set()
        assert finish_closeout.wait(5)

    def checkpoint():
        if during_checkpoint and child.status == ProcessStatus.AWAITING_USER:
            pause()
        store.save_process_checkpoint(child, lifecycle_finalized=True)

    if not during_checkpoint:
        manager.on_process_idle = lambda process: pause() if process is child else None
    manager.start(child.process_id, park, on_terminal=checkpoint)
    thread = manager._threads[child.process_id]
    try:
        assert reached_boundary.wait(3)
        assert thread.is_alive()
        receipt = manager.kill(child.process_id)
        assert receipt["status"] == ("cancelling" if during_checkpoint else "cancelled")
        assert any(entry["operation"] == "agent_tree_cancel_requested"
                   and entry["correlation_id"] == child.process_id
                   for entry in store._storage.load_history_records())
        finish_closeout.set()
        thread.join(3)
        assert not thread.is_alive()
        assert child.status is ProcessStatus.CANCELLED and child.lifecycle_finalized
        durable = store.load_process_state(child.process_id)
        assert durable["process"]["status"] == "cancelled"
        assert durable["runtime_state"]["lifecycle_finalized"] is True
        assert durable["runtime_state"]["task_budget"]["process_leases"][child.process_id]["state"] == "released"
    finally:
        finish_closeout.set()
        thread.join(5)
        store.close()


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


def test_wait_agents_yields_for_main_instruction_with_worker_still_running(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    child = _process(manager, parent_id=root.process_id)
    root.register_child_contract(child.process_id, {'target_files': ['app.txt']})
    child.status = ProcessStatus.RUNNING
    result = queue.Queue()
    thread = threading.Thread(target=lambda: result.put(tools['wait_agents']({
        'process_ids': [child.process_id], 'timeout': 10})), daemon=True)
    thread.start()
    root.mailbox.enqueue_governance_update('Take note of a routine status update')
    with pytest.raises(queue.Empty):
        result.get(timeout=0.05)
    root.mailbox.enqueue_instruction('Preserve the original API in the current change')
    manager.notify_state_changed(root.process_id)
    response = result.get(timeout=2)
    thread.join(2)
    assert response['interruption']['reason'] == 'instruction_pending'
    assert not response['all_terminal']
    assert child.status is ProcessStatus.RUNNING
    assert root.mailbox.has_pending_instruction()


def test_wait_agents_reports_delivered_feedback_once_for_active_owned_work(tmp_path_factory):
    from backend.core.loop.mechanism_catalog import digest
    from backend.core.loop.mechanism_lifecycle import next_authority_sequence
    from backend.core.loop.task_contract import get_task_contract
    from backend.core.loop.task_maintenance import add_change, state_for

    root, tools = _supervisor(tmp_path_factory)
    child = _process(root._manager, parent_id=root.process_id)
    finished = _process(root._manager, parent_id=root.process_id)
    root.register_child_contract(child.process_id, {'target_files': ['app.txt']})
    root.register_child_contract(finished.process_id, {'target_files': ['old.txt']})
    child.status = ProcessStatus.RUNNING
    finished.status = ProcessStatus.COMPLETED
    finished.lifecycle_finalized = True

    def receive(content):
        message = root.mailbox.enqueue_instruction(content)
        source_ref = 'user:' + digest(content)
        root.session.host_ledger.append({'event': 'task_user_instruction_received',
            'task_id': root.active_task_id, 'process_id': root.process_id, 'source_ref': source_ref,
            'content': content, 'authority_sequence': next_authority_sequence(root.session)})
        return add_change(root, content, identifier='feedback-' + message.message_id,
            source_ref=source_ref, expected_task_id=root.active_task_id,
            expected_contract_revision=get_task_contract(root).get('revision', 0), user_adopted=False)

    def wait(process):
        return tools['wait_agents']({'process_ids': [process.process_id], 'timeout': .01})

    first = receive('Preserve the user-facing compatibility behavior')
    assert wait(child)['interruption']['reason'] == 'instruction_pending'
    root.mailbox.drain(task_id=root.active_task_id, step=1)
    assert wait(finished)['interruption'] is None
    interrupted = wait(child)
    notice = interrupted['interruption']
    assert notice and notice['reason'] == 'user_change_pending'
    assert notice['change_ids'] == [first['id']]
    assert notice['affected_process_ids'] == [child.process_id]
    assert not interrupted['all_terminal'] and child.status is ProcessStatus.RUNNING
    assert state_for(root)['changes'][first['id']]['status'] == 'pending'
    assert wait(child)['interruption'] is None

    newer = receive('Also retain the existing test')
    root.mailbox.drain(task_id=root.active_task_id, step=2)
    assert wait(child)['interruption']['change_ids'] == [newer['id']]


@pytest.mark.parametrize(("sealed", "required"), [(False, True), (True, True), (False, False)])
def test_returned_assignment_preserves_draft_and_requires_owner_delivery(tmp_path_factory, sealed, required):
    from backend.core.loop.completion_protocol import HostCompletionEvaluator
    from backend.core.loop.continuation_contract import ContinuationAdmissionError, compile_continuation
    from backend.core.loop.coordination import publish_coordination_event

    repo = _repo(tmp_path_factory)
    root, tools = _supervisor(repo)
    manager = root._manager
    store = SessionStore(str(repo), state_home=tmp_path_factory / 'state')
    manager.session_store = store
    manager.worktree_manager = AgentWorktreeManager(repo, store._storage)
    child = _process(manager, parent_id=root.process_id, task_id='unfinished')
    child.capability_profile_id = 'development.workspace'
    root.register_child_contract(child.process_id, {
        'target_files': ['app.txt'], 'acceptance_criteria': ['Keep the existing format'],
        'required_for_parent_completion': required,
    })
    try:
        path = Path(manager.materialize_process_worktree(child))
        (path / 'app.txt').write_text('tested draft\n', encoding='utf-8')
        event = publish_coordination_event(manager, child, kind='coordination_request',
            summary='Please take this back', affected_interfaces=['app.txt:format'])
        args = {'event_id': event['coordination_event_id'], 'disposition': 'take_remaining_work',
                'note': 'I will finish this assignment'}
        resolve = tools['resolve_coordination_event'].execute
        assert resolve(args)['error'] == 'ASSIGNMENT_HANDOFF_CLOSEOUT_REQUIRED'
        child.status = ProcessStatus.COMPLETED if sealed else ProcessStatus.CANCELLED
        child.result = {'status': child.status.value}
        if sealed:
            manager.seal_process_worktree(child)
        child.lifecycle_finalized = True
        child.budget_lease = child.task_budget.release_process(child.process_id, status=child.status.value)
        store.save_process_checkpoint(child)
        receipt = {'task_id': root.active_task_id, 'receipt_id': 'owner-write', 'succeeded': True,
                   'committed': True, 'effect': 'workspace_write', 'files': ['app.txt']}
        root.tool_receipts.append(receipt)
        with patch.object(store, 'save_process_checkpoint', side_effect=OSError('checkpoint not written')):
            assert resolve(args)['error'] == 'ASSIGNMENT_HANDOFF_CHECKPOINT_FAILED'
        assert not root.delegated_contracts[child.process_id].get('returned_assignment')
        result = resolve(args)
        assert result['accepted'], result
        returned = result['returned_assignment']
        assert resolve(args)['returned_assignment'] == returned
        assert returned['owner_process_id'] == root.process_id
        assert returned['acceptance_criteria'] == ['Keep the existing format']
        assert returned['source_status'] == child.status.value
        durable = store.load_process_state(root.process_id)['runtime_state']['delegated_contracts'][child.process_id]
        assert durable['returned_assignment'] == returned
        assert durable['required_for_parent_completion'] is required
        artifact = tools['read_child_artifact']({'process_id': child.process_id, 'path': 'app.txt'})
        assert artifact['authority'] == 'retained_draft' and artifact['content'] == 'tested draft\n'
        assert (repo / 'app.txt').read_text(encoding='utf-8') == 'base\n'
        with pytest.raises(ContinuationAdmissionError, match='ASSIGNMENT_HANDOFF_STALE'):
            compile_continuation(child, durable, root, {})
        blocked = HostCompletionEvaluator.evaluate(root, 'Work complete')
        assert not blocked.allowed and any('committed workspace effect' in reason for reason in blocked.reasons)
        (repo / 'app.txt').write_text(artifact['content'], encoding='utf-8')
        root.tool_receipts.append({**receipt, 'receipt_id': 'owner-adopted-draft'})
        assert HostCompletionEvaluator.evaluate(root, 'Reviewed and delivered').allowed
    finally:
        _cleanup(store, manager, child)


@pytest.mark.parametrize("decision", ["adopt", "reclaim", "reassign"])
def test_versioned_handoff_waits_for_accepted_result(tmp_path_factory, decision):
    from backend.core.loop.coordination import publish_coordination_event
    from backend.core.loop.interface_contract import capture_declared_contract
    from backend.core.loop.validation_evidence import version_summary, workspace_version

    repo = _repo(tmp_path_factory)
    _git(repo, 'config', '--local', 'core.autocrlf', 'false')
    (repo / 'app.txt').write_bytes(b'base\n')
    _git(repo, 'add', '--renormalize', 'app.txt')
    _git(repo, 'commit', '--amend', '--no-edit', '--no-gpg-sign')
    owner, tools = _supervisor(repo)
    manager = owner._manager
    store = manager.session_store = SessionStore(str(repo), state_home=tmp_path_factory / 'state')
    manager.worktree_manager = AgentWorktreeManager(repo, store._storage)
    source = _process(manager, parent_id=owner.process_id, task_id='original')
    source.capability_profile_id = 'development.workspace'
    interface = 'app.txt:draft'
    frozen = capture_declared_contract(repo, [
        {'node_id': 'source', 'output_interfaces': [interface]},
        {'node_id': 'consumer', 'input_interfaces': [interface]},
    ])
    source.replace_context_snapshot({'task_contract': {
        'output_interfaces': [interface], 'interface_contract': frozen}})
    owner.register_child_contract(source.process_id, {
        'target_files': ['app.txt'], 'acceptance_criteria': ['Preserve layout'],
        'output_interfaces': [interface], 'interface_contract': frozen,
    })
    consumer = _process(manager, parent_id=owner.process_id, task_id='downstream', depends_on=[source.process_id])
    consumer.status = ProcessStatus.WAITING
    children = [source, consumer]
    waited = queue.Queue()
    waiting = threading.Thread(target=lambda: waited.put(manager._wait_for_dependencies(consumer)), daemon=True)

    def terminal(child, status):
        child.status = status
        child.result = {'status': status.value}
        child.lifecycle_finalized = True
        child.budget_lease = child.task_budget.release_process(child.process_id, status=status.value)
        if status is ProcessStatus.COMPLETED:
            manager.seal_process_worktree(child)

    def offer():
        return publish_coordination_event(manager, source, kind='coordination_request',
                                          summary='Review this state', offer_draft=True)['draft']

    def resolve(draft, disposition):
        return tools['resolve_coordination_event']({
            'event_id': draft['id'], 'disposition': disposition, 'note': 'Review and finish scoped work'})

    def read(child, draft):
        return tools['read_child_artifact']({'process_id': child.process_id, 'path': 'app.txt', 'draft_id': draft['id']})

    def approve(child, draft):
        args = {'process_id': child.process_id, 'verdict': 'approved', 'summary': 'Verified delivery',
                'result_commit': child.worktree['result_commit'], 'draft_id': draft['id']}
        assert tools['review_child_outcome']({**args, 'result_commit': '0' * 40})['error'] == (
            'ASSIGNMENT_HANDOFF_RESULT_VERSION_REQUIRED')
        result = tools['review_child_outcome'](args)
        assert result['accepted'], result

    try:
        path = Path(manager.materialize_process_worktree(source))
        source.status = ProcessStatus.RUNNING
        (path / 'app.txt').write_bytes(b'first draft\n')
        witness = version_summary(workspace_version(path, input_files=['app.txt']))
        source.tool_receipts.extend([
            {'task_id': source.active_task_id, 'receipt_id': 'draft-write', 'succeeded': True,
             'committed': True, 'effect': 'workspace_write', 'files': ['app.txt']},
            {'task_id': source.active_task_id, 'receipt_id': 'draft-test', 'test_id': 'layout',
             'test_passed': True, 'succeeded': True, 'committed': True, 'validation_version': witness},
        ])
        head = _git(path, 'rev-parse', 'HEAD')
        first = offer()
        assert first['test_evidence'][0]['evidence'] == 'matching_file_inputs'
        (path / 'app.txt').write_bytes(b'second draft\n')
        second = offer()
        assert second['test_evidence'][0]['evidence'] == 'historical'
        assert [first['version'], second['version']] == [1, 2]
        assert read(source, first)['content'] == 'first draft\n'
        assert read(source, second)['content'] == 'second draft\n'
        assert read(source, first)['current_draft_id'] == second['id']
        assert second['remaining_acceptance']['output_interfaces'] == [interface]
        assert read(consumer, second)['error'] == 'ASSIGNMENT_HANDOFF_DRAFT_NOT_FOUND'
        assert resolve(first, 'accept_draft')['error'] == 'ASSIGNMENT_HANDOFF_VERSION_CHANGED'
        assert resolve(second, 'take_remaining_work')['error'] == 'ASSIGNMENT_HANDOFF_CLOSEOUT_REQUIRED'
        assert _git(path, 'rev-parse', 'HEAD') == head
        assert _git(path, 'diff', '--cached') == ''
        waiting.start()
        if decision == 'adopt':
            assert resolve(second, 'accept_draft')['accepted']
            terminal(source, ProcessStatus.COMPLETED)
            with pytest.raises(queue.Empty):
                waited.get(timeout=0.05)
            entered, release = threading.Event(), threading.Event()
            checkpoint = store.save_process_checkpoint
            def hold_checkpoint(process):
                entered.set()
                assert release.wait(10)
                return checkpoint(process)
            approval_result = queue.Queue()
            def approve_in_thread():
                try:
                    approve(source, second)
                    approval_result.put(True)
                except BaseException as exc:
                    approval_result.put(exc)
            with patch.object(store, 'save_process_checkpoint', side_effect=hold_checkpoint):
                approval = threading.Thread(target=approve_in_thread, daemon=True)
                approval.start()
                try:
                    assert entered.wait(3)
                    manager.notify_state_changed()
                    with pytest.raises(queue.Empty):
                        waited.get(timeout=0.6)
                finally:
                    release.set()
                    approval.join(12)
            assert approval_result.get(timeout=2) is True
            assert resolve(second, 'take_remaining_work')['error'] == 'ASSIGNMENT_HANDOFF_ALREADY_DELIVERED'
        elif decision == 'reclaim':
            terminal(source, ProcessStatus.CANCELLED)
            assert resolve(second, 'take_remaining_work')['accepted']
            assert resolve(second, 'complete_returned_work')['error'] == 'ASSIGNMENT_HANDOFF_DELIVERY_PENDING'
            (repo / 'app.txt').write_bytes(b'owner delivery\n')
            owner.tool_receipts.append({
                'task_id': owner.active_task_id, 'receipt_id': 'returned-write', 'succeeded': True,
                'committed': True, 'effect': 'workspace_write', 'files': ['app.txt'],
            })
            assert resolve(second, 'complete_returned_work')['error'] == 'ASSIGNMENT_HANDOFF_INTERFACE_REVIEW_REQUIRED'
            (repo / 'app.txt').write_bytes(b'owner delivery draft\n')
            result = resolve(second, 'complete_returned_work')
            assert result['accepted'] and result['accepted_result']['draft_id'] == second['id'], result
        else:
            terminal(source, ProcessStatus.CANCELLED)
            (path / 'app.txt').write_bytes(b'private later edit\n')
            with patch.object(manager, 'start'):
                response = tools['delegate_task']({'draft_id': second['id'],
                    'task_description': 'Finish the current assignment', 'target_files': [], 'acceptance_criteria': []})
            assert response.get('delegated'), response
            successor = manager.get(response['process_id'])
            children.append(successor)
            successor_path = Path(manager.materialize_process_worktree(successor))
            assert successor.session is not source.session
            assert owner.delegated_contracts[successor.process_id]['output_interfaces'] == [interface]
            assert (successor_path / 'app.txt').read_bytes() == b'second draft\n'
            successor.tool_receipts.append({
                'task_id': successor.active_task_id, 'receipt_id': 'successor-write', 'succeeded': True,
                'committed': True, 'effect': 'workspace_write', 'files': ['app.txt'],
            })
            terminal(successor, ProcessStatus.COMPLETED)
            with pytest.raises(queue.Empty):
                waited.get(timeout=0.05)
            approve(successor, second)
        assert waited.get(timeout=3) == ''
        consumer_path = Path(manager.materialize_process_worktree(consumer))
        assert (consumer_path / 'app.txt').read_bytes() == (
            b'owner delivery draft\n' if decision == 'reclaim' else b'second draft\n')
        (consumer_path / 'result.txt').write_bytes(b'verified\n')
        terminal(consumer, ProcessStatus.COMPLETED)
        owner.child_reviews[consumer.process_id] = {'verdict': 'approved'}
        if decision == 'reclaim':
            dependent = _process(manager, parent_id=owner.process_id, task_id='dependent',
                                 depends_on=[consumer.process_id])
            children.append(dependent)
            dependent_path = Path(manager.materialize_process_worktree(dependent))
            assert (dependent_path / 'app.txt').read_bytes() == b'owner delivery draft\n'
            assert (dependent_path / 'result.txt').read_bytes() == b'verified\n'
            (repo / 'app.txt').write_bytes(b'delivery changed during downstream work\n')
            with pytest.raises(WorktreeError, match='ASSIGNMENT_HANDOFF_BASIS_CHANGED'):
                manager.promote_process_results(owner, [consumer.process_id])
            (repo / 'app.txt').write_bytes(b'owner delivery draft\n')
        assert manager.promote_process_results(owner, [consumer.process_id])['promoted']
        assert (repo / 'result.txt').read_bytes() == b'verified\n'
        durable = store.load_process_state(owner.process_id)
        assert durable
    finally:
        consumer.cancellation_event.set()
        manager.notify_state_changed()
        if waiting.ident:
            waiting.join(3)
        _cleanup(store, manager, *children)


@pytest.mark.parametrize(("outcome", "draft_timing"), [
    ("deliver", "pending_related"),
    ("deliver", "before_feedback"), ("outside_scope", "before_feedback"), ("retry", "before_feedback"),
    ("deliver", "after_feedback"), ("deliver", "after_adoption"),
    ("deliver", "automatic_closeout"), ("deliver", "cancel_closeout"), ("deliver", "preserve_on_reassignment"),
])
def test_user_withdrawal_reassigns_only_retained_snapshot_and_obligations(tmp_path_factory, outcome, draft_timing):
    from hashlib import sha256
    from backend.core.loop.assignment_handoff import AssignmentHandoffError, latest_draft, recorded_scope_change
    from backend.core.loop.coordination import publish_coordination_event
    from backend.core.loop.mechanism_catalog import digest
    from backend.core.loop.mechanism_lifecycle import next_authority_sequence
    from backend.core.loop.prompt_compiler import PromptCompiler
    from backend.core.loop.task_contract import get_task_contract
    from backend.core.loop.task_maintenance import add_change, guard, state_for
    from backend.core.loop.tool_pipeline import _validate_args
    from backend.core.loop.worktree import WorktreeScopeError

    repo = _repo(tmp_path_factory)
    _git(repo, 'config', '--local', 'core.autocrlf', 'false')
    (repo / 'app.txt').write_bytes(b'base\n')
    _git(repo, 'add', '--renormalize', 'app.txt')
    _git(repo, 'commit', '--amend', '--no-edit', '--no-gpg-sign')
    owner, tools = _supervisor(repo)
    manager = owner._manager
    store = manager.session_store = SessionStore(str(repo), state_home=tmp_path_factory / 'state')
    manager.worktree_manager = AgentWorktreeManager(repo, store._storage)
    keep, remove = 'Complete the application', 'Write the legacy notes in legacy.txt'
    app = {'kind': 'workspace_file', 'path': 'app.txt'}
    legacy = {'kind': 'workspace_file', 'path': 'legacy.txt'}
    initial = tools['declare_task_contract']({'goal': 'Deliver the application and legacy notes',
        'execution_mode': 'delegate', 'deliverables': [app, legacy], 'acceptance_criteria': [keep, remove]})
    assert initial['accepted'], initial
    source = _process(manager, parent_id=owner.process_id, task_id='original')
    source.capability_profile_id = 'development.workspace'
    source.task_description = 'Implement both files including the retired legacy instruction'
    source.required_test_ids = ['app-proof', 'legacy-proof']
    owner.register_child_contract(source.process_id, {
        'target_files': ['app.txt', 'legacy.txt'], 'acceptance_criteria': [keep, remove],
        'required_test_ids': ['app-proof', 'legacy-proof'],
        'allowed_tools': ['run_test', 'read_file', 'write_file'],
        **({'required_tool_calls': [
            {'tool_name': name, 'min_calls': 1} for name in ('edit_file', 'write_file', 'run_test')
        ]} if draft_timing == "pending_related" else {}),
    })
    children = [source]
    try:
        path = Path(manager.materialize_process_worktree(source))
        source.status = ProcessStatus.RUNNING
        (path / 'app.txt').write_bytes(b'accepted source draft\n')
        (path / 'legacy.txt').write_bytes(b'historical content\n')
        if draft_timing == "pending_related":
            for tool_name, filename in (('edit_file', 'app.txt'), ('write_file', 'legacy.txt')):
                source.tool_receipts.append({
                    'task_id': source.active_task_id, 'receipt_id': tool_name,
                    'tool_name': tool_name, 'effect': 'workspace_write', 'file_service_result': True,
                    'succeeded': True, 'committed': True, 'files': [filename],
                    'after_sha256': sha256((path / filename).read_bytes()).hexdigest(),
                })
        def offer():
            return publish_coordination_event(manager, source, kind='coordination_request',
                summary='Both files are drafted', offer_draft=True)['draft']

        draft = offer() if draft_timing in {"before_feedback", "pending_related"} else None
        content = 'Withdraw the legacy notes and their verification. Keep the application.'
        source_ref = 'user:' + digest(content)
        revision = get_task_contract(owner)['revision']
        change = add_change(owner, content, identifier='feedback-fixture', source_ref=source_ref,
            expected_task_id=owner.active_task_id, expected_contract_revision=revision, store=store, user_adopted=False)
        scope = {**({'draft_id': draft['id']} if draft else {'source_process_id': source.process_id}),
                 'retire_target_files': ['legacy.txt'],
                 'retire_acceptance_criteria': [remove], 'retire_test_ids': ['legacy-proof']}
        revise = {'operation': 'revise', 'goal': 'Deliver the application', 'deliverables': [app],
                  'acceptance_criteria': [keep], 'maintenance': {
                      'plan': {'summary': 'Reassign the application', 'steps': [
                          {'id': 'app', 'goal': keep, 'verification': 'Check the application result'}]},
                      'related_review': {'summary': 'The notes have no remaining consumers'},
                      'resolve_changes': [{'id': change['id'], 'reason': 'The user withdrew the legacy notes',
                                           'assignment_scope': scope}]}}
        error = tools['declare_task_contract'](revise)
        assert error['error'] == 'ASSIGNMENT_HANDOFF_SCOPE_CHANGE_INVALID', error
        assert get_task_contract(owner)['revision'] == revision
        # The native ingress records this source before admitting its change.
        owner.session.host_ledger.append({'event': 'task_user_instruction_received', 'task_id': owner.active_task_id,
            'process_id': owner.process_id, 'source_ref': source_ref, 'content': content,
            'authority_sequence': next_authority_sequence(owner.session)})
        authorized = add_change(owner, content, identifier='feedback-authorized', source_ref=source_ref,
            expected_task_id=owner.active_task_id, expected_contract_revision=revision, store=store, user_adopted=False)
        if draft_timing in {'cancel_closeout', 'pending_related'}:
            prompt, _ = PromptCompiler.compile(process=owner, tools=tools, workspace_path=str(repo))
            assert '"issued_scope"' in prompt and remove in prompt
            assert 'Pending feedback IDs: ["feedback-authorized"]' in prompt
        if draft_timing == "after_feedback":
            draft = offer()
            scope['draft_id'] = draft['id']
        revise['maintenance']['resolve_changes'] = [
            {'id': change['id'], 'reason': 'Retain the original receipt without attributing authority to it'},
            {'id': authorized['id'], 'reason': 'Apply the user withdrawal', 'assignment_scope': scope,
             'contract_fields': ['deliverables', 'acceptance_criteria']}]
        revise['maintenance']['resolve_changes'][0]['contract_fields'] = ['goal']
        rejected = tools['declare_task_contract']({**revise, 'deliverables': [app, legacy]})
        assert rejected['error'] == 'ASSIGNMENT_HANDOFF_SCOPE_CHANGE_INVALID', rejected
        pending_scope = revise['maintenance']['resolve_changes'][1].pop('assignment_scope')
        rejected = tools['declare_task_contract'](revise)
        assert rejected['error'] == 'ASSIGNMENT_HANDOFF_SCOPE_REQUIRED', rejected
        assert get_task_contract(owner)['revision'] == revision
        revise['maintenance']['resolve_changes'][1]['assignment_scope'] = pending_scope
        criteria = pending_scope.pop('retire_acceptance_criteria')
        rejected = tools['declare_task_contract'](revise)
        assert rejected['error'] == 'ASSIGNMENT_HANDOFF_CRITERIA_UNRESOLVED', rejected
        assert remove in rejected['message']
        assert get_task_contract(owner)['revision'] == revision
        pending_scope['retire_acceptance_criteria'] = criteria
        if draft_timing == 'cancel_closeout':
            args_to_close = {**revise, 'maintenance': {**revise['maintenance'], 'resolve_changes': [
                {**revise['maintenance']['resolve_changes'][1], 'contract_fields': [
                    'deliverables', 'acceptance_criteria', 'plan', 'related_review'],
                 'assignment_scope': {**pending_scope, 'close_source': True}},
            ]}}

            def finish_cancelled():
                assert source.cancellation_event.wait(10)
                source.status = ProcessStatus.CANCELLED
                return {'status': 'cancelled', 'code': 'TASK_CANCELLED'}

            manager.start(source.process_id, finish_cancelled)
            with patch.object(manager, 'kill', wraps=manager.kill) as cancel:
                committed = tools['declare_task_contract'](args_to_close)
            assert committed['accepted'], committed
            cancel.assert_called_once_with(source.process_id, reason='assignment_handoff')
            assert committed['maintenance_status']['handoff_closeouts'][0]['source_process_id'] == source.process_id
            manager._threads[source.process_id].join(timeout=15)
            assert not manager._threads[source.process_id].is_alive()
            draft = latest_draft(owner, source.process_id)
            assert draft and source.status == ProcessStatus.CANCELLED
        else:
            if draft_timing == "pending_related":
                from backend.core.loop.assignment_handoff import resolve_withdrawal_selection, withdrawal_context
                _, issued = withdrawal_context(owner, state_for(owner), [source.process_id], owner.delegated_contracts)
                scope_ref = issued[source.process_id]["assignment_ref"]
                selected = {"assignment_ref": scope_ref, "retire_criterion_numbers": [2],
                            "retire_target_files": ["legacy.txt"], "retire_test_ids": ["legacy-proof"]}
                revise["maintenance"]["resolve_changes"][1]["assignment_scope"] = {
                    **selected, "assignment_ref": "assignment-stale"}
                assert tools['declare_task_contract'](revise)["error"] == "ASSIGNMENT_HANDOFF_VERSION_CHANGED"
                assert get_task_contract(owner)["revision"] == revision
                revise["maintenance"]["resolve_changes"][1]["assignment_scope"] = {
                    **selected, "retire_acceptance_criteria": [keep]}
                assert tools['declare_task_contract'](revise)["error"] == "ASSIGNMENT_HANDOFF_SCOPE_CHANGE_INVALID"
                redundant = {**selected, "retire_acceptance_criteria": [remove]}
                assert resolve_withdrawal_selection(owner, selected) == resolve_withdrawal_selection(owner, redundant)
                revise["maintenance"]["resolve_changes"][1]["assignment_scope"] = redundant
                revise.pop("acceptance_criteria")
                revise.pop("deliverables")
                revise["maintenance"].pop("related_review")
            committed = tools['declare_task_contract'](revise)
        assert committed['accepted'], committed
        record = recorded_scope_change(owner, source.process_id)
        assert record['remaining']['target_files'] == ['app.txt']
        assert get_task_contract(owner)['acceptance_criteria'] == [keep]
        assert state_for(owner)['changes'][authorized['id']]['status'] == 'adopted'
        if draft_timing == "after_adoption":
            assert not record['draft_id']
            assert guard(source, 'write_file')[0] == 'ASSIGNMENT_HANDOFF_DRAFT_PENDING'
            original_lease = source.worktree
            source.worktree = {**original_lease, 'snapshot_commit': 'different-commit'}
            try:
                with pytest.raises(AssignmentHandoffError, match='changed before binding'):
                    offer()
            finally:
                source.worktree = original_lease
            with patch.object(store, 'save_process_checkpoint', side_effect=OSError('failed checkpoint')):
                with pytest.raises(AssignmentHandoffError, match='failed checkpoint'):
                    offer()
            assert recorded_scope_change(owner, source.process_id) == record
            draft = offer()
            assert recorded_scope_change(owner, source.process_id)['draft_id'] == draft['id']
            with pytest.raises(AssignmentHandoffError, match='Close out the old scope'):
                offer()
        if not draft:
            # Related-work review after an adopted withdrawal carries no new
            # business authority and must leave its captured basis usable.
            review_only = tools['declare_task_contract']({'operation': 'revise', 'related_review': {'summary': 'No remaining consumers'}})
            assert review_only['accepted'], review_only
            assert guard(source, 'write_file')[0] == 'ASSIGNMENT_HANDOFF_DRAFT_PENDING'
            selection = {'supersedes_process_id': source.process_id}
        else:
            assert guard(source, 'write_file')[0] == 'ASSIGNMENT_HANDOFF_SCOPE_RETIRED'
            selection = {'draft_id': draft['id']}
        args = {**selection, 'task_description': keep, 'target_files': ['app.txt'], 'acceptance_criteria': []}
        if draft_timing != 'cancel_closeout':
            assert tools['delegate_task'](args)['error'] == 'ASSIGNMENT_HANDOFF_CLOSEOUT_REQUIRED'
        if draft_timing == 'automatic_closeout':
            def finish():
                source.status = ProcessStatus.FAILED
                return {'status': 'failed', 'code': 'TASK_TREE_CHILD_PROVIDER_ESCROW_EXHAUSTED'}

            manager.start(source.process_id, finish)
            manager._threads[source.process_id].join(timeout=15)
            assert not manager._threads[source.process_id].is_alive()
            draft = latest_draft(owner, source.process_id)
            assert draft and recorded_scope_change(owner, source.process_id)['draft_id'] == draft['id']
        elif draft_timing != 'cancel_closeout':
            source.status = ProcessStatus.CANCELLED
            source.result = {'status': 'cancelled'}
            source.lifecycle_finalized = True
            source.budget_lease = source.task_budget.release_process(source.process_id, status='cancelled')
        if draft:
            (path / 'app.txt').write_bytes(b'later local edit\n')
        original_continuation = {'continue_process_id': source.process_id, 'continuation_mode': 'remaining',
                                 'task_description': keep, 'target_files': ['app.txt', 'legacy.txt']}
        assert tools['delegate_task'](original_continuation)['error'] == 'ASSIGNMENT_HANDOFF_SCOPE_RETIRED'
        assert tools['delegate_task']({**args, 'acceptance_criteria': [remove]})['error'] == 'ASSIGNMENT_HANDOFF_SCOPE_RETIRED'
        if draft_timing == 'cancel_closeout':
            waited = tools['wait_agents']({'process_ids': [source.process_id], 'timeout': 1})
            assert waited['agents'][source.process_id]['handoff']['draft_id'] == draft['id']
            assert waited['agents'][source.process_id]['handoff']['phase'] == 'ready_to_reassign'
            unavailable = tools['read_child_artifact']({'process_id': source.process_id, 'path': 'app.txt'})
            assert unavailable['error_info']['details']['handoff']['draft_id'] == draft['id']
            prompt, _ = PromptCompiler.compile(process=owner, tools=tools, workspace_path=str(repo))
            assert '"phase":"ready_to_reassign"' in prompt and draft['id'] in prompt
            delegate = tools['delegate_task']
            with pytest.raises(ValueError, match='target_files'):
                _validate_args(delegate.prepare_args({'task_description': keep}), delegate.parameters)
            args = delegate.prepare_args({'draft_id': draft['id'], 'task_description': keep})
            _validate_args(args, delegate.parameters)
        if draft_timing == "pending_related":
            before = list(owner.child_ids)
            assert state_for(owner)["review_requirements"]["related"]
            blocked = tools['delegate_task'](args)
            assert blocked['error'] == 'TASK_MAINTENANCE_REQUIRED', blocked
            assert list(owner.child_ids) == before
            reconciled = tools['declare_task_contract']({
                'operation': 'revise', 'related_review': {'summary': 'No remaining consumers'},
            })
            assert reconciled['accepted'], reconciled
        with patch.object(manager, 'start'):
            result = tools['delegate_task'](args)
        assert result.get('delegated'), (result.get('error'), result.get('message'))
        draft = latest_draft(owner, source.process_id)
        successor = manager.get(result['process_id'])
        children.append(successor)
        successor_path = Path(manager.materialize_process_worktree(successor))
        assert (successor_path / 'app.txt').read_bytes() == b'accepted source draft\n'
        assert not (successor_path / 'legacy.txt').exists()
        assert successor.required_test_ids == ['app-proof']
        assert remove not in successor.task_description and 'retired legacy instruction' not in successor.task_description
        assert owner.delegated_contracts[successor.process_id]['acceptance_criteria'] == [keep]
        if draft_timing == "pending_related":
            remaining_calls = owner.delegated_contracts[successor.process_id]['required_tool_calls']
            assert [row['tool_name'] for row in remaining_calls] == ['run_test']
            assert {row['path'] for row in draft['write_evidence']} == {'app.txt', 'legacy.txt'}
        archived = tools['read_child_artifact']({'process_id': source.process_id, 'path': 'legacy.txt', 'draft_id': draft['id']})
        assert archived['content'] == 'historical content\n'
        persisted = store.load_process_state(owner.process_id)['runtime_state']['delegated_contracts']
        assert persisted[source.process_id]['superseded_by'] == successor.process_id
        assert persisted[successor.process_id]['continuation']['scope_change_id'] == authorized['id']
        expected = b'accepted source draft\n'
        if outcome != "deliver":
            (successor_path / 'legacy.txt').write_bytes(b'cannot resurrect the withdrawal\n')
            with pytest.raises(WorktreeScopeError, match='legacy.txt'):
                manager.seal_process_worktree(successor)
            assert successor.worktree['state'] == 'scope_blocked'
        if outcome == "retry":
            first_successor = successor
            first_successor.status = ProcessStatus.FAILED
            first_successor.result = {'status': 'failed', 'code': WorktreeScopeError.code}
            first_successor.lifecycle_finalized = True
            first_successor.budget_lease = first_successor.task_budget.release_process(first_successor.process_id, status='failed')
            expected = b'useful progress from the scoped execution\n'
            (successor_path / 'app.txt').write_bytes(expected)
            retry = {key: value for key, value in args.items() if key != 'draft_id'}
            retry.update(continue_process_id=first_successor.process_id, continuation_mode='remaining')
            assert tools['delegate_task']({**retry, 'target_files': ['app.txt', 'legacy.txt']})['error'] == (
                'ASSIGNMENT_HANDOFF_SCOPE_RETIRED')
            with patch.object(manager, 'start'):
                resumed = tools['delegate_task'](retry)
            assert resumed['delegated'], resumed
            successor = manager.get(resumed['process_id'])
            children.append(successor)
            resumed_path = Path(manager.materialize_process_worktree(successor, continue_process_id=first_successor.process_id))
            assert (resumed_path / 'app.txt').read_bytes() == expected
            assert not (resumed_path / 'legacy.txt').exists()
            assert successor.session is first_successor.session
            assert resumed['continuation']['scope_change_id'] == authorized['id']
            assert resumed['continuation']['draft_id'] == draft['id']
        if outcome != "outside_scope":
            successor.tool_receipts.append({
                'task_id': successor.active_task_id, 'receipt_id': 'scoped-delivery', 'succeeded': True,
                'committed': True, 'effect': 'workspace_write', 'files': ['app.txt'],
            })
            manager.seal_process_worktree(successor)
            successor.status = ProcessStatus.COMPLETED
            successor.result = {'status': 'completed'}
            successor.lifecycle_finalized = True
            successor.budget_lease = successor.task_budget.release_process(successor.process_id, status='completed')
            if draft_timing in {'cancel_closeout', 'pending_related'}:
                waited = tools['wait_agents']({'process_ids': [successor.process_id], 'timeout': 1})
                assert waited['agents'][successor.process_id]['handoff_review_target'] == {
                    'draft_id': draft['id'], 'result_commit': successor.worktree['result_commit'],
                    'changed_files': ['app.txt'], 'changed_files_total': 1}
                selection_args = waited['agents'][successor.process_id]['delivery_call']['arguments']
                assert set(selection_args) == {'result_ref'}
                read = tools['read_child_artifact']({**selection_args, 'path': 'app.txt'})
                assert read['sha256'] == sha256(expected).hexdigest()
                assert tools['read_child_artifact']({
                    'result_ref': 'result-stale', 'path': 'app.txt'})['error'] == 'ASSIGNMENT_HANDOFF_VERSION_CHANGED'
                missing = tools['read_child_artifact']({'process_id': successor.process_id, 'path': 'legacy.txt'})
                assert missing['error'] == 'CHILD_ARTIFACT_PATH_UNAVAILABLE'
            assert tools['promote_agent_changes']({'process_ids': [source.process_id]})['error'] == (
                'ASSIGNMENT_HANDOFF_SCOPE_RETIRED')
            version = {'draft_id': draft['id'], 'result_commit': successor.worktree['result_commit'],
                       'summary': 'The application is the retained deliverable'}
            promotion_args = {'process_ids': [successor.process_id]}
            if draft_timing == "pending_related":
                from backend.core.loop.execution_context import ExecutionContext
                from backend.core.loop.tool_pipeline import ToolPipeline
                tool = tools['promote_agent_changes']
                compact_review = {key: value for key, value in version.items() if key != 'draft_id'}
                invalid = tool({**promotion_args, 'review': {**compact_review, 'result_commit': '0' * 40}})
                assert invalid["error"] == "ASSIGNMENT_HANDOFF_RESULT_VERSION_REQUIRED", invalid
                invalid = tool({**promotion_args, 'review': {**version, 'draft_id': 'other-draft'}})
                assert invalid["error"] == "ASSIGNMENT_HANDOFF_RESULT_VERSION_REQUIRED", invalid
                assert successor.process_id not in owner.child_reviews
                combined = {**selection_args, 'review': {'summary': version['summary']},
                            'completion_result': 'Delivered the application.'}
                stale = tool({**combined, 'result_ref': 'result-stale'})
                assert stale['error'] == 'ASSIGNMENT_HANDOFF_VERSION_CHANGED'
                assert stale['effect_state'] == 'not_committed'
                context = ExecutionContext(process=owner, session=owner.session,
                    workspace_path=str(repo), storage=store._storage, artifacts={'tool_catalog': tools})
                from backend.core.loop.tool_surface_runtime import ToolSurfaceRuntime
                surface = ToolSurfaceRuntime(owner, SimpleNamespace(_executors={}), object(), str(repo), None)
                surface.ctx = context
                assert not surface.rebuild()
                surface.synchronize()
                tool = surface.tools[tool.name]
                owner.steps_used = 1
                missing_review = ToolPipeline().execute(
                    {'id': 'call-without-review', 'name': tool.name, 'args': promotion_args},
                    tool, context, 'without-review',
                )
                assert missing_review.is_error and missing_review.diagnostics['code'] == 'CHILD_REVIEW_REQUIRED'
                assert missing_review.receipt['effect_state'] == 'not_committed'
                assert missing_review.data['error_info']['details']['process_id'] == successor.process_id
                assert missing_review.data['error_info']['next_actions'][0]['action'] == 'review_child_outcome'
                from backend.core.loop.task_maintenance import observe_result
                plan = state_for(owner)
                assert plan and guard(owner, tool.name) is None
                observe_result(owner, missing_review)
                assert state_for(owner) == plan and guard(owner, tool.name) is None
                assert successor.process_id not in owner.child_reviews
                assert (repo / 'app.txt').read_bytes() == b'base\n'
                with patch.object(manager, 'promote_process_results', side_effect=RuntimeError('promotion interrupted')):
                    failed = tool(combined)
                assert failed['error'] == 'promotion interrupted'
                assert 'completion_result' not in failed and owner.completion_claim is None
                assert failed['review']['result_commit'] == version['result_commit']
                assert (repo / 'app.txt').read_bytes() == b'base\n'

                call = {'id': 'call-combined', 'name': tool.name, 'args': combined}
                result = ToolPipeline().execute(call, tool, context, 'combined')
                assert not result.is_error, result.formatted
                assert result.receipt['committed']
                promotion = result.data
                assert promotion['completion_result'] == combined['completion_result']
                assert promotion['review']['result_commit'] == version['result_commit']
                from backend.core.loop.tool_outcome_runtime import ToolOutcomeRuntime
                from backend.core.loop.loop_guard import LoopGuard
                from backend.core.loop.delivery_runtime import DeliveryRuntime
                from backend.core.loop.engineering_workflow import EngineeringWorkflow
                from backend.core.loop.loop_bookkeeping import _apply_pending_instructions
                from backend.core.loop.tool_batch_runtime import ToolBatchRuntime
                events = []
                noop = lambda *_args, **_kwargs: None
                loop_guard = LoopGuard()
                context.artifacts["engineering_workflow"] = EngineeringWorkflow(owner)
                ToolBatchRuntime(owner, owner.session, lambda: surface.tools, loop_guard,
                    context, str(repo), time.time(), noop).commit_results([call], [result], rolled_back=False)
                _apply_pending_instructions(owner, owner.session)
                delivery = DeliveryRuntime(owner, owner.session, loop_guard, None, emit=events.append,
                    apply_mailbox=noop, pause_blocked=noop, capture_recovery=noop, increment_nudge=noop,
                    nudge_count=lambda *_: 0)
                outcome_runtime = ToolOutcomeRuntime(owner, owner.session, delivery, events.append, noop, noop)
                completed = outcome_runtime.evaluate([result], '', 1, {'action': 'keep', 'usage_ratio': 0}, [])
                assert owner.completion_claim.result == combined['completion_result']
                assert completed.result and completed.result['status'] == 'completed', events
            else:
                review_selection = selection_args if draft_timing == "cancel_closeout" else {
                    'process_id': successor.process_id, **version}
                approved = tools['review_child_outcome']({
                    'verdict': 'approved', 'summary': version['summary'], **review_selection})
                assert approved['accepted'], approved
                assert approved['review']['result_commit'] == version['result_commit']
                promotion = tools['promote_agent_changes'](promotion_args)
            assert promotion['promoted'], promotion
            assert (repo / 'app.txt').read_bytes() == expected
        assert not (repo / 'legacy.txt').exists()
    finally:
        _cleanup(store, manager, *children)


def test_wait_reports_supervisor_prerequisite_for_already_started_successor_once(tmp_path_factory):
    owner, tools = _supervisor(tmp_path_factory)
    child = _process(owner._manager, parent_id=owner.process_id, task_id='retained')
    child.task_kind = 'action'
    child.status = ProcessStatus.RUNNING
    owner.register_child_contract(child.process_id, {'continuation': {'retained_obligations': True}})
    context, _ = owner.read_context_snapshot()
    context['task_maintenance'] = {'task_id': owner.active_task_id, 'revision': 2,
        'phase': 'needs_review', 'review_requirements': {'related': True}}
    owner.replace_context_snapshot(context)
    wait = {'process_ids': [child.process_id], 'timeout': 0.01}
    result = tools['wait_agents'](wait)
    interruption = result['interruption']
    assert interruption['reason'] == 'supervisor_maintenance_required'
    assert interruption['source_process_ids'] == [child.process_id]
    assert interruption['owner_process_id'] == owner.process_id
    assert 'related review' in interruption['message']
    assert tools['wait_agents'](wait)['interruption'] is None


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
    from backend.core.loop.execution_contract import HOST_COMPUTE
    follow=AgentTool(name='inspect_other',description='read',parameters={'type':'object'},
        execute=lambda args:called.append('read') or {'observed':True},read_only=True,effect=ToolEffect.READ,execution_contract=HOST_COMPUTE)
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
