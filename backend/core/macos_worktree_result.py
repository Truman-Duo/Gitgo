"""Publish an explicitly staged, privacy-checked result to a new internal ref.

This Host lifecycle API does not stage unselected files, move HEAD, seal the
product task, or promote a user branch. Lost outcomes are reconciled, not replayed.
"""
from contextlib import ExitStack
import hashlib
import os
import re
import time
import uuid

from backend.core import macos_worktree as life, macos_worktree_index as index
from backend.core import macos_broker_client as broker
from backend.core.macos_git_checkout import object_oid


def scan_files(manager, files):
    # Space after '+' prevents file bytes beginning '++' from masquerading as
    # diff headers. Paths were already validated to exclude control characters.
    for name, data, _ in files:
        patch=('+++ b/'+name+'\n@@ -0,0 +1 @@\n'+
               '\n'.join('+ '+line for line in data.decode('utf8',errors='replace').split('\n'))+'\n')
        manager._scan_business_patch(patch.encode('utf8'))


def capture(manager, lease, work, entries):
    from backend.core.sandbox_read import ReadRoot
    files=[];used=0
    with ReadRoot(lease.path) as root:
        if (root.identity.volume,root.identity.file_id)!=tuple(lease.macos_binding['work_identity'][:2]):life.fail('worktree replaced')
        for name,oid,mode in entries:
            data,current=index.selected_file(root,work,name);used+=len(data)
            if used>4*1024*1024:life.fail('result image exceeds budget')
            if object_oid('blob',data)!=oid or mode!=current:life.fail('working file differs from staged index: '+name)
            files.append((name,data,mode))
    scan_files(manager,files)
    return files


def file_identity(fd,name):
    # read_file applies private owner/type/link/bounds checks before identity.
    life.read_file(fd,name)
    info=os.stat(name,dir_fd=fd,follow_symlinks=False)
    return [info.st_dev,info.st_ino,info.st_uid]


def directories(manager, stack, *, create=False):
    git=life.wire.open_directory(manager.repo_root/'.git');stack.callback(os.close,git);life.owned(git)
    parent=git
    identities=[]
    for name in ('refs','gitgo','results'):
        if create:
            try:os.mkdir(name,0o700,dir_fd=parent);os.fsync(parent)
            except FileExistsError:pass
        child=os.open(name,life.FLAGS,dir_fd=parent);stack.callback(os.close,child)
        identities.append(life.owned(child));parent=child
    return git,parent,identities


def no_existing_ref(git, refs, name):
    try:os.stat(name,dir_fd=refs,follow_symlinks=False)
    except FileNotFoundError:pass
    else:life.fail('result ref already exists; never overwrite it')
    try:packed=life.read_file(git,'packed-refs',1048576)
    except FileNotFoundError:return
    ref=('refs/gitgo/results/'+name).encode()
    if any(line.endswith(b' '+ref) for line in packed.splitlines()):life.fail('result ref already packed')


def unlock(fd,name,identity):
    try:actual=file_identity(fd,name)
    except FileNotFoundError:return
    if not identity or actual!=identity:life.fail('unrecorded or replaced publication lock retained: '+name)
    os.unlink(name,dir_fd=fd);os.fsync(fd)


def reconcile(manager,lease):
    """Resolve only ref/locks whose identities were recorded; never run Git."""
    from backend.core.storage.native_invocations import pending_native_invocations
    if pending_native_invocations(manager.storage,include_active=True):life.fail('native recovery debt blocks result reconciliation')
    b=lease.macos_binding;intent=b.get('result_update')
    if not intent:return lease
    with life.context(manager,lease) as (_,__,pins,guard),ExitStack() as stack:
        git=life.wire.open_directory(manager.repo_root/'.git');stack.callback(os.close,git)
        if life.owned(git)!=b['git']:life.fail('Git directory replaced')
        try:
            _,refs,identities=directories(manager,stack)
            if identities!=intent['directories']:life.fail('result ref directories replaced')
        except FileNotFoundError:refs=None
        name=intent['id'];ref='refs/gitgo/results/'+name
        if re.fullmatch('[0-9a-f]{32}',name) is None:life.fail('invalid result publication name')
        observed=None
        try:
            if refs is None:raise FileNotFoundError(name)
            raw=life.read_file(refs,name)
            if (raw!=(intent['commit']+'\n').encode() or
                    file_identity(refs,name)!=intent.get('ref_lock_identity')):life.fail('result ref has an unrecorded object')
            observed=ref
        except FileNotFoundError:
            try:packed=packed_reference(manager,lease,dict(commit=intent['commit'],ref=ref),allow_missing=True)
            except FileNotFoundError:packed=False
            if packed:
                if not intent.get('ref_lock_identity'):life.fail('packed result has no recorded publication authority')
                observed=ref
        guard()
        if refs is not None:unlock(refs,name+'.lock',intent.get('ref_lock_identity'))
        unlock(git,'packed-refs.lock',intent.get('packed_lock_identity'))
        # Keep an observed immutable ref as evidence, without claiming success
        # or automatically continuing the worker whose result was lost.
        lease.state='failed'
        life.save(manager,lease,'published',result_update={},result_interrupted=dict(
            commit=intent['commit'],ref=observed,automatic_resume=False))
    return life.retire(manager,lease,recovery=True)


def publish(manager,lease,*,cancellation=None):
    from backend.core.loop.native_invocation_scope import _SCOPE
    from backend.core.storage.native_invocations import pending_native_invocations
    from backend.core.sandbox_read import ReadRoot
    if _SCOPE.get() is not None:life.fail('result publication is a Host lifecycle operation')
    if lease.mode!='write' or lease.state!='leased':life.fail('result publication requires an active write lease')
    if pending_native_invocations(manager.storage,include_active=True):life.fail('native recovery debt blocks result publication')
    with life.context(manager,lease) as (parent,meta_parent,pins,guard),ExitStack() as stack:
        b=lease.macos_binding
        if b.get('runtime_update') or b.get('index_update') or b.get('result_update') or b.get('result'):life.fail('result already exists or requires recovery')
        work=life.open_bound(parent,life.target_name(manager,lease),b['work_identity']);stack.callback(os.close,work)
        meta=life.open_bound(meta_parent,b['metadata'],b['meta_identity']);stack.callback(os.close,meta)
        life.check_files(manager,lease,work,meta)
        entries=index.decode(life.read_file(meta,'index'));tree=index.tree_plan(entries)[0]
        if tree!=b.get('staged_tree'):life.fail('explicit index staging required before result publication')
        ref_pins=None
        def checked():
            guard()
            if ref_pins is not None:
                with ExitStack() as checking:
                    if directories(manager,checking)[2]!=ref_pins:life.fail('result ref directory changed')
            if cancellation is not None and cancellation.is_set():life.fail('result publication cancelled')
        checked();capture(manager,lease,work,entries);checked()
        # Fresh immutable name, never replacing an old task or user ref.
        git,refs,identities=directories(manager,stack,create=True)
        ref_pins=identities
        try:os.stat('packed-refs.lock',dir_fd=git,follow_symlinks=False)
        except FileNotFoundError:pass
        else:life.fail('existing packed-refs lock retained')
        name=uuid.uuid4().hex;no_existing_ref(git,refs,name)
        author=dict(name='Gitgo Agent',email='agent@local.gitgo.invalid',date=str(int(time.time()))+' +0000')
        message='gitgo staged result'
        person=author['name']+' <'+author['email']+'> '+author['date']
        raw=('tree '+tree+'\nparent '+b['commit']+'\nauthor '+person+'\ncommitter '+person+'\n\n'+message+'\n').encode()
        commit=object_oid('commit',raw)
        intent=dict(id=name,phase='producing',commit=commit,tree=tree,directories=identities)
        life.save(manager,lease,'published',result_update=intent)
        with ReadRoot(manager.repo_root/'.git/objects') as root:
            result=broker.produce_git_objects(manager.repo_root,root,
                ['commit-tree',tree,'-p',b['commit'],'-m',message],b'',author,cancellation=cancellation)
            if result.stdout!=commit+'\n':life.fail('produced commit differs from Host intent')
            checked()
            image=broker.checkout_git_commit(manager.repo_root,root,commit,cancellation=cancellation)
        # Independently validated object image must be precisely the staged index.
        if image.commit!=commit or image.index!=index.encode(entries):life.fail('result image differs from staged index')
        scan_files(manager,image.files);checked()
        life.save(manager,lease,'published',result_update={**intent,'phase':'verified'})
        # Git's global packed-refs lock prevents a concurrent pack from hiding a
        # same-name ref. Own both exclusion files by identity before mutation.
        life.write_file(git,'packed-refs.lock',b'');os.fsync(git)
        intent={**intent,'phase':'packed-locked','packed_lock_identity':file_identity(git,'packed-refs.lock')}
        life.save(manager,lease,'published',result_update=intent)
        checked();no_existing_ref(git,refs,name)
        life.write_file(refs,name+'.lock',(commit+'\n').encode());os.fsync(refs)
        intent={**intent,'phase':'prepared','ref_lock_identity':file_identity(refs,name+'.lock')}
        life.save(manager,lease,'published',result_update=intent)
        checked();no_existing_ref(git,refs,name)
        life.rename_exclusive(refs,name+'.lock',name);os.fsync(refs)
        checked()
        if life.read_file(refs,name)!=(commit+'\n').encode():life.fail('published result ref changed')
        unlock(git,'packed-refs.lock',intent['packed_lock_identity'])
        published=dict(commit=commit,tree=tree,ref='refs/gitgo/results/'+name,
                       directories=identities,identity=intent['ref_lock_identity'],index_sha256=b['index_sha256'])
        life.save(manager,lease,'published',result_update={},result=published)
        return published


def packed_reference(manager,lease,result,*,allow_missing=False):
    """Read only an exact previously recorded result; never resolve expressions."""
    from backend.core.sandbox_read import ReadRoot
    with ExitStack() as stack:
        fd=life.wire.open_directory(manager.repo_root/'.git');stack.callback(os.close,fd)
        if life.owned(fd)!=lease.macos_binding['git']:life.fail('Git directory replaced')
        root=stack.enter_context(ReadRoot(manager.repo_root/'.git'))
        if (root.identity.volume,root.identity.file_id)!=tuple(lease.macos_binding['git'][:2]):life.fail('Git directory replaced')
        raw,_=index.selected_file(root,fd,'packed-refs')
    matches=[];previous=False
    for line in raw.splitlines():
        if not line or line.startswith(b'#'):continue
        if line.startswith(b'^'):
            if not previous or re.fullmatch(rb'\^[0-9a-f]{40}',line) is None:life.fail('malformed packed ref peel')
            previous=False;continue
        fields=line.split(b' ')
        if len(fields)!=2 or re.fullmatch(b'[0-9a-f]{40}',fields[0]) is None or not fields[1].startswith(b'refs/'):
            life.fail('malformed packed refs')
        previous=True
        if fields[1]==result['ref'].encode('ascii'):matches.append(fields[0].decode('ascii'))
    if allow_missing and not matches:return False
    if matches!=[result['commit']]:life.fail('packed result ref missing, changed or duplicated')
    return True


def verify(manager,lease):
    result=lease.macos_binding.get('result')
    if not result:return
    if result['index_sha256']!=lease.macos_binding['index_sha256']:life.fail('published result index changed')
    name=result['ref'].removeprefix('refs/gitgo/results/')
    if re.fullmatch('[0-9a-f]{32}',name) is None:life.fail('invalid result ref')
    with ExitStack() as stack:
        try:
            _,refs,identities=directories(manager,stack)
            if identities!=result['directories']:life.fail('published ref directories replaced')
            raw=life.read_file(refs,name)
        except FileNotFoundError:
            packed_reference(manager,lease,result)
            return
        if raw!=(result['commit']+'\n').encode() or file_identity(refs,name)!=result['identity']:
            life.fail('published immutable result ref changed')


def read_blob(manager,commit,path):
    """Binary-safe content from a verified immutable image, after UID cleanup."""
    from backend.core.macos_git_checkout import validate_commit
    from backend.core.sandbox_read import ReadRoot
    from backend.core.loop.native_invocation_scope import current_native_cancellation
    validate_commit(commit)
    if type(path) is not str:life.fail('artifact requires a literal relative path')
    index.validate_entries([(path,'0'*40,0o644)])
    broker.project_binding(manager.repo_root)
    cancellation=current_native_cancellation(manager.repo_root)
    with ReadRoot(manager.repo_root/'.git/objects') as root:
        image=broker.checkout_git_commit(manager.repo_root,root,commit,cancellation=cancellation)
    if image.commit!=commit:life.fail('artifact image belongs to another commit')
    files=[data for name,data,_ in image.files if name==path]
    if len(files)!=1:life.fail('artifact is absent from the immutable result')
    return files[0]
