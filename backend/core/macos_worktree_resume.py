"""Explicit continuation from a retained checkout, never process resurrection.

Checkpoint publication uses the existing private result ref and worktree row.
Only immutable business bytes enter the fresh checkout; runtime inputs and
execution authority are acquired afresh by the normal create path.
"""
import copy
import os

from backend.core import macos_worktree as life, macos_worktree_result as result


def validate_source(manager,previous,*,process_id,snapshot_commit,upstream_commits,base_commit):
    from backend.core.loop.worktree import WorktreeLease
    if type(previous) is not WorktreeLease:life.fail('continuation requires a registered worktree lease')
    life.target_name(manager,previous)
    saved=manager.storage.load_worktree_state(previous.process_id)
    fields=('worktree_id','process_id','path','root_workspace','mode','state','snapshot_commit',
            'upstream_commits','promoted','continuation_commit','macos_binding')
    if not saved or any(saved.get(k)!=getattr(previous,k) for k in fields):
        life.fail('stale continuation source')
    if any(previous.macos_binding.get(k) for k in ('index_update','result_update','runtime_update','runtime_merged')):
        life.fail('continuation requires a settled unpublished source')
    if (previous.process_id==process_id or previous.mode!='write' or previous.state!='leased' or previous.promoted
            or previous.snapshot_commit!=snapshot_commit or previous.upstream_commits!=upstream_commits
            or previous.macos_binding.get('commit')!=base_commit):
        life.fail('continuation requires the same snapshot and dependencies and a distinct write lease')


def checkpoint(manager,previous,*,process_id,snapshot_commit,upstream_commits,base_commit):
    from backend.core.macos_worktree_runtime import admission,snapshot
    from backend.core.macos_worktree_finish import prepare
    from backend.core.macos_worktree_seal import inventory
    from backend.core.sandbox_read import ReadRoot
    from backend.core.loop.native_invocation_scope import current_native_cancellation
    validate_source(manager,previous,process_id=process_id,snapshot_commit=snapshot_commit,
        upstream_commits=upstream_commits,base_commit=base_commit)
    admission(manager,previous)
    cancellation=current_native_cancellation(manager.repo_root)
    # A durable checkpoint may be reused, but never silently refreshed after
    # publication. Unknown/lost publication is handled by existing recovery.
    prepare(manager,previous,cancellation=cancellation)
    with life.context(manager,previous) as (parent,meta_parent,_,guard):
        b=previous.macos_binding
        work=life.open_bound(parent,life.target_name(manager,previous),b['work_identity'])
        meta=None
        try:
            meta=life.open_bound(meta_parent,b['metadata'],b['meta_identity'])
            guard();life.check_files(manager,previous,work,meta);result.verify(manager,previous)
            files,metadata=inventory(previous,work);snapshot(manager,previous,work)
            result.scan_files(manager,files)
            commit=b['result']['commit']
            with ReadRoot(manager.repo_root/'.git/objects') as root:
                image=life.broker.checkout_git_commit(manager.repo_root,root,commit,cancellation=cancellation)
            if image.commit!=commit or image.files!=files or image.index!=life.read_file(meta,'index'):
                life.fail('continuation checkpoint differs from retained working files')
            guard();life.check_files(manager,previous,work,meta);result.verify(manager,previous)
            if inventory(previous,work)!=(files,metadata):life.fail('continuation source changed during capture')
            if cancellation is not None and cancellation.is_set():life.fail('continuation cancelled')
            updated=copy.deepcopy(previous);updated.continuation_commit=commit
            manager._persist(updated);previous.__dict__.update(updated.__dict__)
            return commit
        finally:
            if meta is not None:os.close(meta)
            os.close(work)
