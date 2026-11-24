"""Bounded automatic staging before the existing protected result seal.

No ignore interpreter or repository Git execution is introduced. Workspaces
with business ignore rules require explicit staging until protected enumeration exists.
"""
import os
from backend.core import macos_worktree as life, macos_worktree_index as index
from backend.core import macos_worktree_seal as sealing, macos_worktree_result as result


def metadata_only_ignore(raw, *, root=True):
    """Accept only rules that cannot hide any business file in our inventory.

    This is an allowlist, not a partial wildmatch interpreter. Escapes,
    negations, business patterns and undecodable rules remain unsupported.
    """
    try:lines=raw.decode('utf8').splitlines()
    except UnicodeError:return False
    allowed={'.gitgo','.gitgo/','/.gitgo','/.gitgo/','/.gitgo/**'} if root else set()
    return all(not line or line.startswith('#') or line in allowed
               for line in (line.rstrip(' ') for line in lines))


def prepare(manager,lease,*,cancellation=None):
    from backend.core.macos_worktree_runtime import admission,snapshot
    admission(manager,lease)
    with life.context(manager,lease) as (parent,metadata_parent,pins,guard):
        b=lease.macos_binding
        if b.get('result'):return
        if b.get('runtime_update') or b.get('runtime_merged'):life.fail('runtime publication must not precede automatic staging')
        work=life.open_bound(parent,life.target_name(manager,lease),b['work_identity'])
        meta=None
        try:
            meta=life.open_bound(metadata_parent,b['metadata'],b['meta_identity'])
            guard();life.check_files(manager,lease,work,meta)
            if cancellation is not None and cancellation.is_set():life.fail('automatic staging cancelled')
            files,metadata=sealing.inventory(lease,work)
            snapshot(manager,lease,work)
            entries=index.decode(life.read_file(meta,'index'))
            # Fail closed rather than silently adding files ignored by Git.
            if any(n.rsplit('/',1)[-1]=='.gitignore' and not metadata_only_ignore(data,root=n=='.gitignore') for n,data,_ in files):
                life.fail('automatic staging with .gitignore requires protected enumeration; use explicit staging')
            git=life.wire.open_directory(manager.repo_root/'.git')
            try:
                info=os.open('info',life.FLAGS,dir_fd=git)
                try:
                    try:exclude=life.read_file(info,'exclude')
                    except FileNotFoundError:exclude=b''
                    if not metadata_only_ignore(exclude):
                        life.fail('automatic staging with info/exclude requires protected enumeration')
                finally:os.close(info)
            except FileNotFoundError:pass
            finally:os.close(git)
            result.scan_files(manager,files)
            before={n:(oid,m) for n,oid,m in entries}
            after={n:(index.object_oid('blob',data),m) for n,data,m in files}
            paths=sorted(n for n in before if n not in after)+sorted(n for n in after if before.get(n)!=after[n])
            empty_tree=not files and not entries and not b.get('staged_tree')
            # An unchanged, previously unstaged image still needs a verified
            # tree. Re-stage one existing member through the normal producer.
            if not paths and not b.get('staged_tree'):
                if files:paths=[files[0][0]]
            guard()
        finally:
            if meta is not None:os.close(meta)
            os.close(work)
    # Each step owns its existing durable transaction and lock. A crash retains
    # its journal; recovery never automatically starts the next step.
    if empty_tree:index.stage(manager,lease,[],cancellation=cancellation,_empty_tree=True)
    for at in range(0,len(paths),index.MAX_CHANGES):
        index.stage(manager,lease,paths[at:at+index.MAX_CHANGES],cancellation=cancellation)
    with life.context(manager,lease) as (parent,_,__,guard):
        work=life.open_bound(parent,life.target_name(manager,lease),lease.macos_binding['work_identity'])
        try:
            guard()
            if sealing.inventory(lease,work)!=(files,metadata):life.fail('automatic staging input changed; publication withheld')
        finally:os.close(work)
    result.publish(manager,lease,cancellation=cancellation)
