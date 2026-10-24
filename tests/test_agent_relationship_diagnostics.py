"""Regressions derived from real Agent API failures and their boundary repairs."""
from pathlib import Path
import pytest
from backend.core.dependency_graph import record_tool_observation
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import ProcessStatus
from backend.core.loop.worktree import AgentWorktreeManager
from backend.core.storage import StorageRuntime
from .test_agent_dag_worktree import _repo, _process


def _sealed_worker(tmp_path, existing_observation=False):
    repo=_repo(tmp_path)
    storage=StorageRuntime(repo,state_home=tmp_path/'state')
    manager=AgentProcessManager(worktree_manager=AgentWorktreeManager(repo,storage))
    root=_process(manager,task_id='root')
    if existing_observation:
        record_tool_observation(repo,task_id='root',tool_name='read_file',files=['app.txt'])
    child=_process(manager,parent_id=root.process_id,task_id='child')
    path=Path(manager.materialize_process_worktree(child))
    (path/'result.txt').write_text('business output\n',encoding='utf-8')
    record_tool_observation(path,task_id='child',tool_name='read_file',files=['app.txt','result.txt'])
    child.status=ProcessStatus.COMPLETED
    manager.seal_process_worktree(child)
    root.child_reviews[child.process_id]={'verdict':'approved','summary':'verified fixture output'}
    return repo,storage,manager,root,child


def test_internal_observation_is_not_a_worker_business_change(tmp_path_factory):
    repo,storage,manager,root,child=_sealed_worker(tmp_path_factory)
    try:
        assert child.worktree['changed_files']==['result.txt'], child.worktree['changed_files']
    finally:
        manager.dispose_process_worktree(child)
        storage.close()


@pytest.mark.parametrize('existing_observation', [False, True])
def test_returned_observation_cannot_block_approved_promotion(tmp_path_factory, existing_observation):
    repo,storage,manager,root,child=_sealed_worker(tmp_path_factory, existing_observation)
    try:
        assert (repo/'.gitgo/dependency_observations.jsonl').is_file()
        result=manager.promote_process_results(root,[child.process_id])
        assert result['promoted'] is True
        assert (repo/'result.txt').read_text(encoding='utf-8')=='business output\n'
    finally:
        manager.dispose_process_worktree(child)
        storage.close()




def test_throttled_optional_observation_does_not_consume_global_capacity(tmp_path_factory):
    """A rejected optional record must not reserve a physical write it never makes."""
    from backend.core.storage.models import StoragePolicy
    policy = StoragePolicy(observability_transactions_per_minute=1,
                           sqlite_transactions_per_minute=30)
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / 'state', policy=policy) as storage:
        assert storage.append_event(event_type='diagnostic', summary='first') is True
        before = len(storage._global_sqlite_budget._entries)
        assert storage.append_event(event_type='diagnostic', summary='throttled') is False
        assert len(storage._global_sqlite_budget._entries) == before


def test_rejected_optional_observations_do_not_starve_authoritative_state(tmp_path_factory):
    """Exercise real observer drops followed by a required state write, no fake clock."""
    from backend.core.storage.models import StoragePolicy
    policy = StoragePolicy()
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / 'state', policy=policy) as storage:
        ref = 'ref:diagnostic'
        for _ in range(policy.observability_transactions_per_minute):
            assert storage.append_event(event_type='diagnostic', summary='accepted') is True
        for _ in range(policy.sqlite_transactions_per_minute + 1):
            assert storage.append_event(event_type='diagnostic', summary='throttled') is False
        storage.put_state_ref('diagnostic', 'authoritative', ref)
        assert storage.get_state_ref('diagnostic', 'authoritative') == ref


def test_runtime_artifacts_stay_local_even_when_staged_but_business_rules_promote(tmp_path_factory):
    from .test_agent_dag_worktree import _git
    repo,storage,manager,root,child=_sealed_worker(tmp_path_factory)
    # Use a new execution to exercise runtime files staged by an Agent itself.
    sibling=_process(manager,parent_id=root.process_id,task_id='sibling')
    path=Path(manager.materialize_process_worktree(sibling))
    try:
        for name in ['tool_invocations/probe.json', 'context_objects/refs/probe.json',
                     'dependency_graph.v2.json', 'project_rules.md']:
            target=path/'.gitgo'/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text('{}',encoding='utf-8')
        business=path/'project_rules.md';business.write_text('user project rules',encoding='utf-8')
        _git(path,'add','-A')
        sibling.status=ProcessStatus.COMPLETED
        manager.seal_process_worktree(sibling)
        assert sibling.worktree['changed_files']==['project_rules.md']
        assert (path/'.gitgo/tool_invocations/probe.json').is_file()
        root.child_reviews[sibling.process_id]={'verdict':'approved'}
        result=manager.promote_process_results(root,[sibling.process_id])
        assert result['promoted'] is True
        assert (repo/'project_rules.md').read_text(encoding='utf-8')=='user project rules'
        assert not (repo/'.gitgo/tool_invocations/probe.json').exists()
        assert not (repo/'.gitgo/project_rules.md').exists()
    finally:
        manager.dispose_process_worktree(sibling)
        manager.dispose_process_worktree(child)
        storage.close()


def test_legacy_sealed_runtime_delta_does_not_pollute_downstream_or_promotion(tmp_path_factory):
    from .test_agent_dag_worktree import _git
    repo,storage,manager,root,child=_sealed_worker(tmp_path_factory)
    path=Path(child.worktree['path'])
    downstream=None
    try:
        # Emulate the immutable result produced before this fix, including logs.
        _git(path,'reset','--mixed',child.worktree['snapshot_commit'])
        _git(path,'add','-A');_git(path,'commit','-m','legacy contaminated result')
        legacy=_git(path,'rev-parse','HEAD')
        downstream=manager.worktree_manager.create(process_id='legacy-consumer',task_id='consumer',
            snapshot_commit=child.worktree['snapshot_commit'],upstream_commits=[legacy])
        assert (Path(downstream.path)/'result.txt').read_text(encoding='utf-8')=='business output\n'
        changed=_git(Path(downstream.path),'diff','--name-only',child.worktree['snapshot_commit'],'HEAD')
        assert changed=='result.txt'
        result=manager.worktree_manager.promote(snapshot_commit=child.worktree['snapshot_commit'],
            ordered_commits=[legacy],promotion_id='legacy-promotion')
        assert result['changed_files']==['result.txt']
        assert (repo/'result.txt').read_text(encoding='utf-8')=='business output\n'
    finally:
        if downstream:manager.worktree_manager.dispose(downstream)
        manager.dispose_process_worktree(child)
        storage.close()


def test_trace_drop_does_not_charge_shared_budget_or_block_state(tmp_path_factory):
    from backend.core.storage.models import StoragePolicy
    policy=StoragePolicy(observability_transactions_per_minute=1,sqlite_transactions_per_minute=10)
    with StorageRuntime(tmp_path_factory,state_home=tmp_path_factory/'state',policy=policy) as storage:
        assert storage.append_trace_record('diagnostic',{'event':'first','seq':1}) is not None
        for i in range(50):
            assert storage.append_trace_record('diagnostic',{'event':'dropped','seq':i+2}) is None
        storage.put_state_ref('diagnostic','terminal','ref:terminal')
        assert storage.get_state_ref('diagnostic','terminal')=='ref:terminal'


def test_global_rejection_leaves_observation_budget_available():
    from backend.core.storage.support import _MinuteBudget
    shared=_MinuteBudget(1,100);optional=_MinuteBudget(10,100)
    assert shared.reserve(10)
    assert not _MinuteBudget.reserve_all((shared,10),(optional,10))
    assert optional.reserve(100)


def test_joint_budget_limits_hold_under_reversed_concurrent_requests():
    from concurrent.futures import ThreadPoolExecutor
    from backend.core.storage.support import _MinuteBudget
    shared=_MinuteBudget(5,500);a=_MinuteBudget(50,500);b=_MinuteBudget(50,500)
    def reserve(index):
        local=a if index%2 else b
        requests=[(shared,10),(local,10)]
        return _MinuteBudget.reserve_all(*(requests if index%3 else requests[::-1]))
    with ThreadPoolExecutor(8) as pool:
        results=list(pool.map(reserve,range(80)))
    assert sum(results)==5
    assert len(a._entries)+len(b._entries)==len(shared._entries)==5
    assert a._bytes+b._bytes==shared._bytes==50


def test_approved_disposed_no_change_dependency_does_not_block_promotion(tmp_path_factory):
    repo=_repo(tmp_path_factory)
    with StorageRuntime(repo,state_home=tmp_path_factory/'state') as storage:
        manager=AgentProcessManager(worktree_manager=AgentWorktreeManager(repo,storage))
        root=_process(manager,task_id='noop-root')
        noop=_process(manager,parent_id=root.process_id,task_id='noop')
        manager.materialize_process_worktree(noop)
        noop.status=ProcessStatus.COMPLETED
        manager.seal_process_worktree(noop)
        assert noop.worktree['own_commit']==''
        root.child_reviews[noop.process_id]={'verdict':'approved'}
        manager.dispose_process_worktree(noop,keep_ref=True)
        assert noop.worktree['state']=='disposed' and noop.worktree['result_commit']
        consumer=_process(manager,parent_id=root.process_id,task_id='consumer',depends_on=[noop.process_id])
        path=Path(manager.materialize_process_worktree(consumer))
        try:
            (path/'output.txt').write_text('consumer output',encoding='utf-8')
            consumer.status=ProcessStatus.COMPLETED
            manager.seal_process_worktree(consumer)
            # Cleanup must not waive the requirement for actual A approval.
            root.child_reviews.pop(noop.process_id)
            with pytest.raises(ValueError,match='lacks supervisor approval'):
                manager.promote_process_results(root,[consumer.process_id])
            root.child_reviews[noop.process_id]={'verdict':'approved'}
            root.child_reviews[consumer.process_id]={'verdict':'approved'}
            result=manager.promote_process_results(root,[consumer.process_id])
            assert result['promoted'] is True
            assert result['changed_files']==['output.txt']
            assert noop.worktree['state']=='disposed' and noop.worktree['promoted'] is True
            assert (repo/'output.txt').read_text(encoding='utf-8')=='consumer output'
        finally:
            manager.dispose_process_worktree(consumer)


@pytest.mark.parametrize('own_commit,result_commit', [('abc','abc'), ('','')])
def test_disposed_changed_or_unsealed_result_is_not_promotable(own_commit,result_commit):
    from backend.core.loop.worktree import WorktreeLease, WorktreeError
    lease=WorktreeLease(worktree_id='x',process_id='x',task_id='x',path='',root_workspace='',
        state='disposed',own_commit=own_commit,result_commit=result_commit)
    assert not lease.has_promotable_result


def test_promotion_preserves_concurrent_business_edit_and_user_index(tmp_path_factory):
    from backend.core.loop.worktree import WorktreeError
    repo=_repo(tmp_path_factory)
    with StorageRuntime(repo,state_home=tmp_path_factory/'state') as storage:
        manager=AgentProcessManager(worktree_manager=AgentWorktreeManager(repo,storage))
        root=_process(manager,task_id='conflict-root')
        child=_process(manager,parent_id=root.process_id,task_id='writer')
        path=Path(manager.materialize_process_worktree(child))
        try:
            (path/'app.txt').write_text('agent version',encoding='utf-8')
            (path/'output.txt').write_text('new output',encoding='utf-8')
            child.status=ProcessStatus.COMPLETED;manager.seal_process_worktree(child)
            root.child_reviews[child.process_id]={'verdict':'approved'}
            index_before=(repo/'.git/index').read_bytes()
            (repo/'app.txt').write_text('concurrent user version',encoding='utf-8')
            with pytest.raises(WorktreeError,match='workspace changed after DAG snapshot'):
                manager.promote_process_results(root,[child.process_id])
            assert (repo/'app.txt').read_text(encoding='utf-8')=='concurrent user version'
            assert not (repo/'output.txt').exists()
            assert (repo/'.git/index').read_bytes()==index_before
        finally:manager.dispose_process_worktree(child)


def test_business_projection_preserves_deletion_and_literal_filenames(tmp_path_factory):
    repo=_repo(tmp_path_factory)
    with StorageRuntime(repo,state_home=tmp_path_factory/'state') as storage:
        manager=AgentProcessManager(worktree_manager=AgentWorktreeManager(repo,storage))
        root=_process(manager,task_id='literal-root')
        child=_process(manager,parent_id=root.process_id,task_id='literal')
        path=Path(manager.materialize_process_worktree(child))
        try:
            (path/'app.txt').unlink()
            for name in ['note[1].txt','note with space.txt']:
                (path/name).write_text(name,encoding='utf-8')
            child.status=ProcessStatus.COMPLETED;manager.seal_process_worktree(child)
            root.child_reviews[child.process_id]={'verdict':'approved'}
            result=manager.promote_process_results(root,[child.process_id])
            assert set(result['changed_files'])=={'app.txt','note[1].txt','note with space.txt'}
            assert not (repo/'app.txt').exists()
            for name in ['note[1].txt','note with space.txt']:
                assert (repo/name).read_text(encoding='utf-8')==name
        finally:manager.dispose_process_worktree(child)

def test_read_only_upstream_is_reviewed_evidence_not_a_code_worktree(tmp_path_factory):
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tools import ToolRegistry
    repo=_repo(tmp_path_factory)
    with StorageRuntime(repo,state_home=tmp_path_factory/"state") as storage:
        manager=AgentProcessManager(worktree_manager=AgentWorktreeManager(repo,storage))
        root=_process(manager,task_id="mixed-root")
        source=manager.fork(parent_id=root.process_id,role="worker",actor_kind="worker",
            capability_profile_id="governance.observe",task_kind="answer",
            workspace_path=str(repo),tool_registry=ToolRegistry(["read_file"]),
            max_steps=4,ring_level=RingLevel.RING_3,task_id="audit")
        source.status=ProcessStatus.COMPLETED
        child=_process(manager,parent_id=root.process_id,task_id="code",depends_on=[source.process_id])
        path=Path(manager.materialize_process_worktree(child))
        try:
            (path/"output.txt").write_text("verified code",encoding="utf-8")
            child.status=ProcessStatus.COMPLETED;manager.seal_process_worktree(child)
            root.child_reviews[child.process_id]={"verdict":"approved"}
            with pytest.raises(ValueError,match="lacks supervisor approval"):
                manager.promote_process_results(root,[child.process_id])
            root.child_reviews[source.process_id]={"verdict":"approved"}
            result=manager.promote_process_results(root,[child.process_id])
            assert result["process_ids"]==[child.process_id]
            assert result["verified_dependency_ids"]==[source.process_id]
            assert source.worktree=={}
            assert (repo/"output.txt").read_text(encoding="utf-8")=="verified code"
        finally:manager.dispose_process_worktree(child)
