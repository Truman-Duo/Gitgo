"""Explicit opt-in ordinary-user publication fault probe; never a tool entry."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parent))
if not getattr(sys,'frozen',False):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def exercise(manager,empty,commit,mode,*,hard_exit=True):
    from backend.core import macos_promotion_write as write
    from backend.core.knowledge.manager import LessonManager
    LessonManager.bind_storage(manager.repo_root,manager.storage)
    key=manager._ref_id('promotion-proof-'+mode);store=manager.storage
    head=store.get_state_ref('agent_promotion',key)
    if head:raise RuntimeError('fault proof already exists')
    count=0
    def prepare(versions,*,native_write):
        nonlocal head,count
        record=dict(state='prepared',snapshot_commit=empty,before_commit=empty,result_commit=commit,
            paths=['data.bin'],applied_commits=[commit],file_versions=versions,native_write=native_write,
            previous=dict(state='committed',snapshot_commit=empty,result_commit=empty,paths=[],applied_commits=[]))
        ref=store.put_blob(json.dumps(record,sort_keys=True).encode(),media_type='application/json')
        if not store.commit_state_revision('agent_promotion',key,ref,expected_ref=head,revision_key=uuid.uuid4().hex):
            raise RuntimeError('fault proof CAS failed')
        head=ref;count+=1
        if mode=='created' and count==2:die()
    class Interrupted(BaseException):pass
    def die():
        if hard_exit:os._exit(86)
        raise Interrupted()
    original=write.publish_file
    def replace(*a,**kw):
        original(*a,**kw)
        if mode=='applied':die()
    write.publish_file=replace
    try:
        try:
            write.commit(manager,['data.bin'],empty,commit,
                {'_mutation_lock_root':str(store.paths.state_home/'mutation-locks'),
                 '_lesson_context':{'state_home':str(store.paths.state_home)}},prepare)
        except Interrupted:return 86
        raise RuntimeError('publication fault did not trigger')
    finally:write.publish_file=original


def exercise_formal(manager,base,commit,mode,*,hard_exit=True):
    from backend.core import macos_promotion_write as write
    from backend.core.knowledge.manager import LessonManager
    LessonManager.bind_storage(manager.repo_root,manager.storage)
    store=manager.storage;save=store.commit_state_revision;publish=write.publish_file
    class Interrupted(BaseException):pass
    def die():
        if hard_exit:os._exit(86)
        raise Interrupted()
    def saving(ns,key,ref,**kw):
        result=save(ns,key,ref,**kw)
        if ns=='agent_promotion':
            row=json.loads(store.read_blob(ref));temps=row.get('native_write',{}).get('temporaries',[])
            if mode=='flow-created' and row.get('state')=='prepared' and temps and temps[0]['identity']:die()
        return result
    def publishing(*a,**kw):
        publish(*a,**kw)
        if mode=='flow-applied':die()
    store.commit_state_revision=saving;write.publish_file=publishing
    try:
        try:manager.promote(snapshot_commit=base,ordered_commits=[commit],promotion_id=mode)
        except Interrupted:return 86
        raise RuntimeError('formal publication fault did not trigger')
    finally:store.commit_state_revision=save;write.publish_file=publish


def exercise_product_review(worktrees, lease):
    """Exercise the registered review and publish tools for a sealed fixture."""
    from backend.core.knowledge.manager import LessonManager
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.models import ProcessStatus, RingLevel
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.loop.runtime import AgentRuntimeFactory, RuntimeSpec
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry

    project = worktrees.repo_root
    store = worktrees.storage
    manager = AgentProcessManager(worktree_manager=worktrees)
    LessonManager.bind_storage(project, store)
    supervisor = manager.fork(
        None, "supervisor", ToolRegistry(["review_child_outcome", "promote_agent_changes"]),
        8, RingLevel.RING_0, workspace_path=str(project),
        task_id="product-review", actor_kind="supervisor", task_kind="supervisor",
        capability_profile_id="supervisor.control",
    )
    child = AgentRuntimeFactory.create(
        RuntimeSpec(
            "executor", RingLevel.RING_3, ToolRegistry([]), 4,
            parent_id=supervisor.process_id, task_id=lease.task_id, task_kind="action",
            capability_profile_id="development.workspace", storage=store,
        ),
        process_id=lease.process_id,
    )
    child.status = ProcessStatus.COMPLETED
    child.worktree = lease.to_dict()
    # The fixture's write happened while preparing its verified sealed result.
    child.tool_receipts.append({
        "receipt_id": "sealed-fixture-write", "effect": "workspace_write",
        "succeeded": True, "committed": True,
    })
    manager.register_restored(child)
    catalog = build_internal_tools(supervisor, {}, str(project), None, None, None)
    ctx = ExecutionContext(supervisor, supervisor.session, str(project), storage=store,
                           cancellation=supervisor.cancellation_event)
    ctx.artifacts["tool_catalog"] = catalog
    pipeline = ToolPipeline()

    def call(name, args, label):
        return pipeline.execute({"name": name, "args": args}, catalog[name], ctx,
                                f"product-review-{label}")

    def review(verdict):
        result = call("review_child_outcome", {
            "process_id": child.process_id, "verdict": verdict,
            "summary": "Review of the sealed local acceptance fixture",
        }, verdict)
        assert result.data["accepted"], result.formatted

    target = project / "data.bin"
    args = {"process_ids": [child.process_id]}
    assert not os.path.lexists(target)
    baseline = b"gitgo protected object\0\xff\r\n"
    output = b"published result fixture"
    with target.open("xb") as stream:
        stream.write(baseline)
    try:
        target.chmod(0o644)
        for label in ("unreviewed", "changes_required"):
            if label == "changes_required":
                review(label)
            denied = call("promote_agent_changes", args, label)
            assert "lacks supervisor approval" in denied.formatted, denied.formatted
            assert not denied.receipt.get("native_invocations") and target.read_bytes() == baseline
            assert not child.worktree["promoted"]
            assert not store.list_state_refs("agent_promotion", prefix="")

        review("approved")
        result = call("promote_agent_changes", args, "publish")
        assert not result.is_error and result.data["promoted"], result.formatted
        assert result.receipt["effect_state"] == "committed" and target.read_bytes() == output
        assert child.worktree["promoted"] and store.load_worktree_state(child.process_id)["promoted"]
        published = result.receipt["native_invocations"]
        assert len(set(published)) == len(published) == 6
        repeated = call("promote_agent_changes", args, "repeat")
        assert not repeated.is_error and repeated.data["already_promoted"] == [lease.result_commit]
        assert repeated.data["changed_files"] == [] and target.read_bytes() == output
        verified = repeated.receipt["native_invocations"]
        assert len(set(verified)) == len(verified) == 2 and set(published).isdisjoint(verified)
        assert not list(project.glob(".gitgo-promotion-*.tmp"))
        return dict(unreviewed_blocked=True, changes_required_blocked=True, approved=True,
                    published=published, repeat_verified=verified, result_marked_promoted=True)
    finally:
        target.unlink()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--empty',required=True);p.add_argument('--commit',required=True)
    p.add_argument('--mode',choices=('created','applied','flow-created','flow-applied'),required=True);p.add_argument('--uid',type=int,required=True)
    a=p.parse_args()
    if os.getuid()!=a.uid or os.geteuid()!=a.uid or a.uid==0:raise RuntimeError('ordinary Host only')
    from backend.core.macos_broker_client import broker_session
    from backend.core.loop.worktree import AgentWorktreeManager
    from backend.core.storage import get_storage
    try:
        with broker_session(a.config) as binding:
            action=exercise_formal if a.mode.startswith('flow-') else exercise
            action(AgentWorktreeManager(binding['project'],get_storage(binding['project'])),a.empty,a.commit,a.mode)
    except OSError as exc:
        # The frozen Host maps raw OSErrors to a generic installation refusal.
        # Keep the actual failure visible for this operator-only probe.
        raise RuntimeError(f"{a.mode}: publication probe failed: {exc}") from exc

if __name__=='__main__':main()
