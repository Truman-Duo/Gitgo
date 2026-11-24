"""Acknowledge an explicitly published result in the existing worktree row.

No implicit staging, HEAD move, new ref, or automatic promotion.
Validated runtime evidence is merged before the final durable acknowledgement.
Interrupted acknowledgement is resolved by reloading the authoritative row.
"""
from contextlib import ExitStack
import copy
import hashlib
import os
import stat

from backend.core import macos_worktree as life,macos_worktree_index as index,macos_worktree_result as result
from backend.core import macos_broker_client as broker
from backend.core.macos_git_checkout import canonical_index


def inventory(lease,work):
    from backend.core.sandbox_read import ReadRoot
    business=[];metadata={};used=0;count=0;volume=os.fstat(work).st_dev
    with ReadRoot(lease.path) as root:
        if (root.identity.volume,root.identity.file_id)!=tuple(lease.macos_binding['work_identity'][:2]):life.fail('seal worktree replaced')
        def visit(fd,prefix='',depth=0):
            nonlocal used,count
            if depth>64:life.fail('seal directory depth exceeded')
            before=os.fstat(fd);names=sorted(os.listdir(fd))
            for name in names:
                count+=1
                if count>512:life.fail('seal inventory exceeds entry budget')
                relative=prefix+name
                if not prefix and name=='.git':continue # check_files owns this exact pointer
                if name.casefold() in ('.git','.gitgo') and relative!='.gitgo':life.fail('unexpected metadata alias in worktree')
                info=os.stat(name,dir_fd=fd,follow_symlinks=False)
                if info.st_dev!=volume:life.fail('seal cannot cross filesystems')
                if stat.S_ISDIR(info.st_mode):
                    child=os.open(name,life.FLAGS,dir_fd=fd)
                    try:
                        if life.owned(child)!=[info.st_dev,info.st_ino,info.st_uid]:life.fail('seal directory replaced')
                        visit(child,relative+'/',depth+1)
                    finally:os.close(child)
                else:
                    data,mode=index.selected_file(root,work,relative);used+=len(data)
                    if used>8*1024*1024:life.fail('seal inventory exceeds byte budget')
                    if relative.startswith('.gitgo/'):
                        metadata[relative[7:]]=hashlib.sha256(data).hexdigest()
                    else:business.append((relative,data,mode))
            after=os.fstat(fd)
            if names!=sorted(os.listdir(fd)) or (before.st_mtime_ns,before.st_ctime_ns)!=(after.st_mtime_ns,after.st_ctime_ns):
                life.fail('worktree directory changed during seal inventory')
        visit(work)
    index.validate_entries([(n,index.object_oid('blob',d),m) for n,d,m in business])
    if sum(len(d) for _,d,_ in business)>4*1024*1024:life.fail('seal business image exceeds budget')
    return tuple(sorted(business)),metadata


def seal(manager,lease,*,cancellation=None):
    from backend.core.loop.native_invocation_scope import _SCOPE
    from backend.core.storage.native_invocations import pending_native_invocations
    from backend.core.sandbox_read import ReadRoot
    if _SCOPE.get() is not None:life.fail('sealing is a Host lifecycle operation')
    if lease.mode!='write' or lease.state!='leased':life.fail('seal requires an active write lease')
    if pending_native_invocations(manager.storage,include_active=True):life.fail('native recovery debt blocks sealing')
    with life.context(manager,lease) as (parent,meta_parent,pins,guard),ExitStack() as stack:
        b=lease.macos_binding;published=b.get('result')
        if not published or b.get('result_update') or b.get('index_update') or b.get('runtime_update'):life.fail('seal requires an explicitly staged and published result')
        if 'runtime_input_digests' not in b:life.fail('legacy lease lacks runtime metadata baseline; retain it for recovery')
        work=life.open_bound(parent,life.target_name(manager,lease),b['work_identity']);stack.callback(os.close,work)
        meta=life.open_bound(meta_parent,b['metadata'],b['meta_identity']);stack.callback(os.close,meta)
        def checked():
            guard();life.check_files(manager,lease,work,meta);result.verify(manager,lease)
            if cancellation is not None and cancellation.is_set():life.fail('sealing cancelled')
            if pending_native_invocations(manager.storage,include_active=True):life.fail('native recovery debt blocks sealing')
        checked();files,metadata=inventory(lease,work)
        runtime_changed=metadata!=(b.get('runtime_merged') or {}).get('input_digests',b['runtime_input_digests'])
        if runtime_changed:
            from backend.core.macos_worktree_runtime import snapshot
            snapshot(manager,lease,work) # reject unknown evidence before any producer call
        if canonical_index(files)!=life.read_file(meta,'index'):life.fail('unstaged additions, deletions, content or modes block sealing')
        result.scan_files(manager,files)
        with ReadRoot(manager.repo_root/'.git/objects') as root:
            base=broker.checkout_git_commit(manager.repo_root,root,b['commit'],cancellation=cancellation);checked()
            final=broker.checkout_git_commit(manager.repo_root,root,published['commit'],cancellation=cancellation);checked()
        if base.commit!=b['commit'] or final.commit!=published['commit'] or final.files!=files or final.index!=canonical_index(files):
            life.fail('sealed candidate differs from verified immutable result')
        before={n:(d,m) for n,d,m in base.files};after={n:(d,m) for n,d,m in final.files}
        changed=sorted((n for n in before.keys()|after.keys() if before.get(n)!=after.get(n)),key=lambda n:n.encode('utf8'))
        if inventory(lease,work)!=(files,metadata):life.fail('worktree changed before seal acknowledgement')
        checked()
        if runtime_changed:
            from backend.core.macos_worktree_runtime import merge
            merge(manager,lease,cancellation=cancellation,_held_context=(parent,meta_parent,pins,guard))
            b=lease.macos_binding
            checked()
            if inventory(lease,work)!=(files,metadata):life.fail('worktree changed after runtime merge; seal was not acknowledged')
            if (b.get('runtime_merged') or {}).get('input_digests')!=metadata:life.fail('runtime merge acknowledgement is missing')
        # Business completion is the final effect: metadata_ref and lifecycle fields commit in
        # one existing authoritative SQLite transaction. Never update the live
        # in-memory lease before persistence has returned successfully.
        sealed=copy.deepcopy(lease);sealed.state='sealed';sealed.error=''
        sealed.result_commit=published['commit'];sealed.own_commit=published['commit'] if changed else ''
        sealed.changed_files=changed
        sealed.macos_binding={**b,'seal':dict(version=1,parent=b['commit'],commit=published['commit'],
            index_sha256=hashlib.sha256(final.index).hexdigest(),changed_files=changed,runtime_input_digests=metadata,
            derived_caches=(b.get('runtime_merged') or {}).get('derived_caches',{}))}
        manager._persist(sealed)
        lease.__dict__.update(sealed.__dict__)
        return lease


def verify_fields(lease):
    b=lease.macos_binding;ack=b.get('seal');published=b.get('result') or {}
    if (not ack or ack.get('version')!=1 or ack.get('parent')!=b['commit'] or
            ack.get('commit')!=published.get('commit') or lease.result_commit!=ack.get('commit') or
            ack.get('index_sha256')!=b['index_sha256'] or ack.get('index_sha256')!=published.get('index_sha256') or
            ack.get('changed_files')!=lease.changed_files or
            ack.get('runtime_input_digests')!=(b.get('runtime_merged') or {}).get('input_digests',b.get('runtime_input_digests')) or
            ack.get('derived_caches',{})!=(b.get('runtime_merged') or {}).get('derived_caches',{}) or
            lease.own_commit!=(lease.result_commit if lease.changed_files else '')):
        life.fail('sealed result fields differ from durable acknowledgement')
