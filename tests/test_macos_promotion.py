"""Formal entry: actual sealed refs, SQLite state and protected native file writes."""
import json
from pathlib import Path
import pytest
from tests.test_macos_worktree_result import ready,staged,case,create,repository,commit_fixture
from tests.test_macos_worktree_upstream import wire_queries
from backend.core.loop.worktree import WorktreeError


@pytest.fixture
def publication(ready,monkeypatch,tmp_path_factory):
    from backend.core.knowledge.manager import LessonManager
    manager,lease,*_=ready
    LessonManager.bind_storage(manager.repo_root,manager.storage)
    manager.publish_index(lease);manager.seal(lease)
    queries=wire_queries(manager,monkeypatch,tmp_path_factory/'queries')
    return manager,lease,queries


def run(publication,**options):
    manager,lease,*_=publication
    return manager.promote(snapshot_commit=lease.snapshot_commit,ordered_commits=[lease.result_commit],promotion_id='formal',**options)


def record(manager):
    ref=manager.storage.get_state_ref('agent_promotion',manager._ref_id('formal'))
    return json.loads(manager.storage.read_blob(ref)) if ref else None


def test_formal_linear_publication_and_retry_do_not_move_head(publication,monkeypatch):
    manager,lease,queries=publication;root=manager.repo_root
    saved={p:p.read_bytes() for p in (root/'.git/HEAD',root/'.git/index',root/'.git/config')}
    def forbidden(*a,**k):raise AssertionError('unprotected repository command')
    for name in ('_git_text','_git_bytes','_git_input','_add_detached'):monkeypatch.setattr(manager,name,forbidden)
    result=run(publication)
    assert result['promoted'] and result['changed_files']==['a.bin'] and result['effect_state']=='committed'
    assert (root/'a.bin').read_bytes()==(Path(lease.path)/'a.bin').read_bytes()
    assert record(manager)['state']=='committed' and record(manager)['native_result_refs'][lease.result_commit]['ref']==lease.macos_binding['result']['ref']
    again=run(publication)
    assert again['promoted'] and again['applied_commits']==[] and again['already_promoted']==[lease.result_commit]
    assert all(p.read_bytes()==v for p,v in saved.items()) and len(queries)==1
    assert not list(root.rglob('.gitgo-promotion-*.tmp'))


@pytest.mark.parametrize('fault',['prepared','created','applied','ack'])
def test_formal_reconciliation_does_not_replay_completed_write(publication,monkeypatch,fault):
    from backend.core import macos_promotion_write as writer
    manager,lease,_=publication;store=manager.storage;original=store.commit_state_revision;replace=writer.publish_file
    class Death(BaseException):pass
    def save(ns,key,ref,**kw):
        value=json.loads(store.read_blob(ref)) if ns=='agent_promotion' else {}
        if fault=='ack' and value.get('state')=='committed':raise OSError('ack unavailable')
        result=original(ns,key,ref,**kw)
        if ns=='agent_promotion' and value.get('state')=='prepared':
            temps=value.get('native_write',{}).get('temporaries',[])
            if fault=='prepared' or fault=='created' and temps and temps[0]['identity']:raise Death()
        return result
    def crash(*a,**kw):
        replace(*a,**kw)
        if fault=='applied':raise Death()
    monkeypatch.setattr(store,'commit_state_revision',save);monkeypatch.setattr(writer,'publish_file',crash)
    with pytest.raises(WorktreeError if fault=='ack' else Death):run(publication)
    assert record(manager)['state']=='prepared'
    monkeypatch.setattr(store,'commit_state_revision',original);calls=[]
    def observing(*a,**kw):calls.append(1);return replace(*a,**kw)
    monkeypatch.setattr(writer,'publish_file',observing)
    result=run(publication)
    assert result['promoted'] and record(manager)['state']=='committed'
    assert len(calls)==(0 if fault in ('applied','ack') else 1)
    assert result['reconciled_effect']==(fault in ('applied','ack'))
    assert not list(manager.repo_root.rglob('.gitgo-promotion-*.tmp'))


@pytest.mark.parametrize('kind',['unsealed','ref-changed','input-changed','workspace-changed','duplicate'])
def test_ref_and_input_failures_leave_no_prepared_write(publication,kind):
    manager,lease,_=publication;root=manager.repo_root;options={}
    if kind=='unsealed':
        lease.state='leased';manager._persist(lease)
    elif kind=='ref-changed':(root/'.git'/lease.macos_binding['result']['ref']).write_text('0'*40+'\n')
    elif kind=='input-changed':options['input_dependencies']=[{'path':'nested/run.sh','sha256':'0'*64}]
    elif kind=='workspace-changed':(root/'a.bin').write_bytes(b'foreign')
    old=(root/'a.bin').read_bytes()
    with pytest.raises((WorktreeError,ValueError)):
        if kind=='duplicate':manager.promote(snapshot_commit=lease.snapshot_commit,ordered_commits=[lease.result_commit]*2,promotion_id='formal')
        else:run(publication,**options)
    assert record(manager) is None and (root/'a.bin').read_bytes()==old
    assert not list(root.rglob('.gitgo-promotion-*.tmp'))


def test_incremental_second_result_and_packed_anchor(publication,ready):
    manager,first,_=publication;git=ready[-1];run(publication)
    second=manager.create(process_id='second',task_id='t',snapshot_commit=first.snapshot_commit,upstream_commits=[first.result_commit])
    (Path(second.path)/'new.txt').write_text('second result\n')
    manager.stage_index(second,['new.txt']);manager.publish_index(second);manager.seal(second)
    git('pack-refs','--all','--prune')
    result=manager.promote(snapshot_commit=first.snapshot_commit,ordered_commits=[first.result_commit,second.result_commit],promotion_id='formal')
    assert result['applied_commits']==[second.result_commit] and result['changed_files']==['new.txt']
    assert (manager.repo_root/'new.txt').read_text()=='second result\n'
    saved=record(manager)
    assert saved['paths']==['a.bin','new.txt'] and len(saved['file_versions'])==2
    again=manager.promote(snapshot_commit=first.snapshot_commit,ordered_commits=[first.result_commit,second.result_commit],promotion_id='formal')
    assert again['applied_commits']==[]


def test_nonlinear_sealed_result_is_rejected_without_overwriting(publication):
    manager,first,_=publication;run(publication)
    fork=manager.create(process_id='fork',task_id='t',snapshot_commit=first.snapshot_commit)
    (Path(fork.path)/'a.bin').write_bytes(b'different branch')
    manager.stage_index(fork,['a.bin']);manager.publish_index(fork);manager.seal(fork)
    old=(manager.repo_root/'a.bin').read_bytes();saved=record(manager)
    with pytest.raises(WorktreeError,match='nonlinear publication'):
        manager.promote(snapshot_commit=first.snapshot_commit,ordered_commits=[fork.result_commit],promotion_id='formal')
    assert record(manager)==saved and (manager.repo_root/'a.bin').read_bytes()==old


def test_foreign_edit_after_crash_keeps_debt_without_replay(publication,monkeypatch):
    manager,lease,_=publication;store=manager.storage;original=store.commit_state_revision
    class Death(BaseException):pass
    def stopping(ns,key,ref,**kw):
        result=original(ns,key,ref,**kw)
        if ns=='agent_promotion':raise Death()
        return result
    monkeypatch.setattr(store,'commit_state_revision',stopping)
    with pytest.raises(Death):run(publication)
    monkeypatch.setattr(store,'commit_state_revision',original)
    saved=record(manager);(manager.repo_root/'a.bin').write_bytes(b'foreign')
    with pytest.raises(WorktreeError,match='PROMOTION_EFFECT_AMBIGUOUS'):run(publication)
    assert record(manager)==saved and (manager.repo_root/'a.bin').read_bytes()==b'foreign'


def test_packaged_formal_probe_has_bounded_native_calls(ready,monkeypatch,tmp_path_factory):
    from scripts.check_macos_git_broker import promotion_flow_cases
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    manager,_,_,checks,git=ready
    queries=wire_queries(manager,monkeypatch,tmp_path_factory/'formal-queries')
    blob=git('hash-object','-w','--stdin',data=b'gitgo protected object\0\xff\r\n')
    tree=git('mktree',data=f'100644 blob {blob}\tdata.bin\n'.encode())
    base=git('-c','user.name=Test','-c','user.email=test@local.invalid','commit-tree',tree,'-m','baseline')
    lease=manager.create(process_id='flow-fixture',task_id='t',snapshot_commit=base)
    (Path(lease.path)/'data.bin').write_bytes(b'published result fixture')
    manager.stage_index(lease,['data.bin']);manager.publish_index(lease);manager.seal(lease);manager.dispose(lease)
    before=len(checks)+len(queries)
    cases=promotion_flow_cases(manager,lease)
    assert len(cases)==4 and all(c['matched'] for c in cases)
    assert len(checks)+len(queries)-before==31
    assert not (manager.repo_root/'data.bin').exists()


@pytest.mark.parametrize('kind',['valid','cancelled','foreign'])
def test_pipeline_owner_and_cancellation_are_preserved(publication,kind):
    from types import SimpleNamespace
    from threading import Event
    from backend.core.loop.native_invocation_scope import native_invocation_scope
    from backend.core.sandbox import SandboxDenied
    manager,lease,_=publication;stop=Event()
    if kind=='cancelled':stop.set()
    ctx=SimpleNamespace(workspace_path=str(manager.repo_root if kind!='foreign' else manager.repo_root.parent),
        storage=manager.storage,cancellation=stop,process=None)
    with native_invocation_scope(ctx,'publication-proof',1):
        if kind=='valid':assert run(publication)['promoted']
        else:
            with pytest.raises((WorktreeError,SandboxDenied)):run(publication)
            assert record(manager) is None


def test_product_review_gates_protected_macos_publication(ready, monkeypatch, tmp_path_factory):
    from backend.core.knowledge.manager import LessonManager
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.models import ProcessStatus, RingLevel
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry

    worktrees, baseline, *_ = ready
    project = worktrees.repo_root
    store = worktrees.storage
    LessonManager.bind_storage(project, store)
    queries = wire_queries(worktrees, monkeypatch, tmp_path_factory / "product-queries")
    manager = AgentProcessManager(worktree_manager=worktrees)
    supervisor = manager.fork(
        None, "supervisor", ToolRegistry(["review_child_outcome", "promote_agent_changes"]),
        8, RingLevel.RING_0, workspace_path=str(project),
        task_id="product-promotion", actor_kind="supervisor", task_kind="supervisor",
        capability_profile_id="supervisor.control",
    )
    child = manager.fork(
        supervisor.process_id, "executor", ToolRegistry([]), 4, RingLevel.RING_3,
        workspace_path=str(project), task_id="sealed-fixture", task_kind="action",
        capability_profile_id="development.workspace",
    )
    lease = worktrees.create(process_id=child.process_id, task_id=child.active_task_id,
                             snapshot_commit=baseline.snapshot_commit)
    child.worktree = lease.to_dict()
    output = b"reviewed product output\n"
    (Path(lease.path) / "a.bin").write_bytes(output)
    worktrees.stage_index(lease, ["a.bin"])
    worktrees.publish_index(lease)
    worktrees.seal(lease)
    child.worktree = lease.to_dict()
    child.status = ProcessStatus.COMPLETED
    child.tool_receipts.append({
        "receipt_id": "fixture-write", "effect": "workspace_write",
        "succeeded": True, "committed": True,
    })

    catalog = build_internal_tools(supervisor, {}, str(project), None, None, None)
    ctx = ExecutionContext(supervisor, supervisor.session, str(project), storage=store,
                           cancellation=supervisor.cancellation_event)
    ctx.artifacts["tool_catalog"] = catalog
    pipeline = ToolPipeline()

    def call(name, args, index):
        return pipeline.execute({"name": name, "args": args}, catalog[name], ctx,
                                f"product-review-{index}")

    target = project / "a.bin"
    before = target.read_bytes()
    args = {"process_ids": [child.process_id]}
    for index, review in enumerate((None, "changes_required")):
        if review:
            result = call("review_child_outcome", {
                "process_id": child.process_id, "verdict": review, "summary": "Needs review",
            }, "rejected-review")
            assert result.data["accepted"], result.formatted
        blocked = call("promote_agent_changes", args, index)
        assert not blocked.data.get("promoted") and "lacks supervisor approval" in blocked.formatted
        assert target.read_bytes() == before and not queries
        assert not store.list_state_refs("agent_promotion", prefix="")

    approved = call("review_child_outcome", {
        "process_id": child.process_id, "verdict": "approved", "summary": "Verified fixture output",
    }, "approved-review")
    assert approved.data["accepted"], approved.formatted
    promoted = call("promote_agent_changes", args, "approved")
    assert not promoted.is_error and promoted.data["promoted"], promoted.formatted
    assert promoted.receipt["effect_state"] == "committed"
    assert target.read_bytes() == output and child.worktree["promoted"] and len(queries) == 1
    assert store.load_worktree_state(child.process_id)["promoted"]
    repeated = call("promote_agent_changes", args, "repeat")
    assert repeated.data["already_promoted"] == [lease.result_commit]
    assert repeated.data["changed_files"] == [] and target.read_bytes() == output
    assert len(queries) == 1 and not list(project.rglob(".gitgo-promotion-*.tmp"))
