"""Host-owned linked-worktree lifecycle; no unrestricted Git subprocess.

Authority is the existing project grant plus the durable WorktreeLease row.
Paths alone (including a task-written .git file) never establish ownership.
Publication changes a private staging directory into the final name with an
exclusive rename. Interrupted creation is retired, never replayed or resumed.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from backend.core import macos_broker_client as broker, macos_broker_protocol as wire
from backend.core.macos_git_write import rename_exclusive
from backend.core.loop.worktree import WorktreeError
from backend.core.macos_file_authority import FileAuthorityError, require_no_extended_acl

FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def fail(message):
    raise WorktreeError('MACOS_WORKTREE_UNCONFIRMED: ' + message)


def exclusive_acl(fd):
    # This backend conservatively rejects all nonempty ACLs, including deny
    # entries; it never edits a project's permissions to make a write eligible.
    try:require_no_extended_acl(fd)
    except FileAuthorityError as exc:
        raise WorktreeError('MACOS_WORKTREE_UNCONFIRMED: exclusive Host authority requires an ACL-free object') from exc


def owned(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        fail('directory is not exclusively controlled by this Host user')
    exclusive_acl(fd)
    return wire.directory_identity(fd)


def read_file(fd, name, limit=262144):
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    try:
        info = os.fstat(child)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > limit):
            fail('unsafe metadata file: ' + name)
        exclusive_acl(child)
        data = bytearray()
        while len(data) <= limit:
            part = os.read(child, min(65536, limit + 1 - len(data)))
            if not part:break
            data.extend(part)
        if len(data) > limit:fail('metadata exceeds limit')
        return bytes(data)
    finally:os.close(child)


def write_file(fd, name, data, mode=0o600):
    child = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=fd)
    try:
        remaining = memoryview(data)
        while remaining:
            count = os.write(child, remaining)
            if not count:raise OSError('short worktree write')
            remaining = remaining[count:]
        os.fchmod(child, mode);os.fsync(child)
    finally:os.close(child)


def root_head(manager):
    """Read only fixed HEAD/ref files through the authorized native root."""
    from backend.core.sandbox_read import ReadRoot
    if not broker.active():fail('authorized project broker required')
    broker.project_binding(manager.repo_root)
    with ReadRoot(manager.repo_root / '.git') as root:
        def read(name, limit):
            with root.open(name) as opened:return opened.read_bytes(max_bytes=limit)
        head = read('HEAD', 1024)
        if re.fullmatch(b'[0-9a-f]{40}\n', head):value = head[:-1].decode()
        else:
            match = re.fullmatch(rb'ref: (refs/heads/[A-Za-z0-9_./-]+)\n', head)
            if not match or b'..' in match[1] or any(not p or p.startswith('.') or p.endswith(('.', '.lock')) for p in match[1].decode().split('/')):
                fail('unsupported HEAD reference')
            ref = match[1].decode()
            try:raw = read(ref, 128)
            except FileNotFoundError:
                packed = read('packed-refs', 1048576)
                matches = [line.split(b' ', 1)[0] for line in packed.splitlines()
                           if line.endswith(b' ' + ref.encode()) and not line.startswith((b'#', b'^'))]
                if len(matches) != 1:fail('HEAD ref missing or ambiguous')
                raw = matches[0] + b'\n'
            if re.fullmatch(b'[0-9a-f]{40}\n', raw) is None:fail('invalid HEAD object')
            value = raw[:-1].decode()
        if read('HEAD', 1024) != head:fail('HEAD changed during capture')
    broker.project_binding(manager.repo_root)
    return value


def target_name(manager, lease):
    manager._safe_id(lease.process_id)
    name = hashlib.sha256(lease.process_id.encode()).hexdigest()[:20]
    if (Path(lease.root_workspace) != manager.repo_root or
            Path(lease.path) != manager.worktrees_root / name or lease.worktree_id != lease.process_id):
        fail('lease path does not match its managed project/process')
    return name


def stored(manager, lease):
    value = manager.storage.load_worktree_state(lease.process_id)
    if (not value or any(value.get(k) != getattr(lease, k) for k in
            ('worktree_id', 'path', 'root_workspace', 'state', 'macos_binding'))):
        fail('stale or unregistered worktree lease')


@contextmanager
def parent_lock(manager):
    """Serialize reservation and later transitions using the same pinned lock."""
    with ExitStack() as stack:
        parent=wire.open_directory(manager.worktrees_root);stack.callback(os.close,parent);owned(parent)
        lock=os.open('.gitgo-lifecycle.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        stack.callback(os.close,lock)
        info=os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077:
            fail('unsafe lifecycle lock')
        exclusive_acl(lock)
        try:fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:fail('another Host owns this lifecycle transition')
        yield parent


@contextmanager
def context(manager, lease, *, initial=False):
    """Pin both parents and serialize Host lifecycle changes across processes."""
    target_name(manager, lease)
    if not broker.active():fail('authorized project broker required')
    grant = broker.project_binding(manager.repo_root)
    with ExitStack() as stack:
        def opening(path):
            fd = wire.open_directory(path);stack.callback(os.close, fd);owned(fd);return fd
        parent = stack.enter_context(parent_lock(manager))
        stored(manager, lease)
        git = opening(manager.repo_root / '.git')
        if initial:
            try:os.mkdir('worktrees', 0o700, dir_fd=git);os.fsync(git)
            except FileExistsError:pass
        meta_parent = opening(manager.repo_root / '.git/worktrees')
        pins = dict(project=grant['identity'],parent=owned(parent),git=owned(git),metadata_parent=owned(meta_parent))
        if not initial and any(lease.macos_binding.get(k) != value for k, value in pins.items()):
            fail('project or worktree parent directory replaced')
        def guard():
            if broker.project_binding(manager.repo_root)['identity'] != pins['project']:fail('project replaced')
            for path, key in ((manager.worktrees_root,'parent'),(manager.repo_root/'.git','git'),
                              (manager.repo_root/'.git/worktrees','metadata_parent')):
                fd = wire.open_directory(path)
                try:
                    if owned(fd) != pins[key]:fail('worktree parent replaced')
                finally:os.close(fd)
        guard()
        yield parent, meta_parent, pins, guard


def save(manager, lease, phase, **fields):
    lease.macos_binding = {**lease.macos_binding, **fields, 'phase':phase}
    manager._persist(lease)


def open_bound(parent, name, identity):
    fd = os.open(name, FLAGS, dir_fd=parent)
    try:
        if owned(fd) != identity:fail('worktree directory object replaced: ' + name)
        return fd
    except BaseException:os.close(fd);raise


def metadata(manager, lease):
    binding = lease.macos_binding
    return {
        'HEAD':(binding['commit'] + '\n').encode(),
        'commondir':b'../..\n',
        'gitdir':(lease.path + '/.git\n').encode(),
        'locked':b'Gitgo-owned lifecycle; retire through the recorded lease\n',
    }


def check_files(manager, lease, work, meta):
    binding = lease.macos_binding
    expected = ('gitdir: ' + str(manager.repo_root / '.git/worktrees' / binding['metadata']) + '\n').encode()
    if read_file(work, '.git') != expected:fail('worktree metadata pointer changed')
    for name, raw in metadata(manager, lease).items():
        if read_file(meta, name) != raw:fail('linked-worktree metadata changed: ' + name)
    if hashlib.sha256(read_file(meta, 'index')).hexdigest() != binding['index_sha256']:
        fail('worktree index changed outside the protected lifecycle')


def publish(manager, lease, commit, *, continuation_commit=""):
    """Materialize a broker-verified commit; record identities before publish."""
    from backend.core.sandbox_read import ReadRoot
    from backend.core.loop.native_invocation_scope import current_native_cancellation
    from backend.core.macos_git_checkout import validate_commit, canonical_index
    validate_commit(commit)
    checkout_commit=continuation_commit or commit
    validate_commit(checkout_commit)
    if continuation_commit and (lease.continuation_commit!=continuation_commit or not lease.resumed_from_process_id):
        fail('continuation image lacks its durable source binding')
    stored(manager, lease)
    if lease.macos_binding:fail('existing worktree transaction must be recovered first')
    cancellation = current_native_cancellation(manager.repo_root)
    with ReadRoot(manager.repo_root / '.git/objects') as root:
        image = broker.checkout_git_commit(manager.repo_root, root, checkout_commit, cancellation=cancellation)
    if image.commit != checkout_commit or image.index != canonical_index(image.files):fail('invalid prepared checkout')
    with context(manager, lease, initial=True) as (parent, meta_parent, pins, guard):
        def checked():
            guard()
            if cancellation is not None and cancellation.is_set():fail('creation cancelled')
        name = target_name(manager, lease)
        try:os.stat(name,dir_fd=parent,follow_symlinks=False)
        except FileNotFoundError:pass
        else:fail('target already exists; never adopt or overwrite it')
        nonce = uuid.uuid4().hex
        binding = dict(version=1,**pins,commit=commit,checkout_commit=checkout_commit,stage='.pending-'+nonce,
                       trash='.retiring-'+nonce,metadata='gitgo-'+nonce,
                       meta_trash='.retiring-'+nonce,index_sha256=hashlib.sha256(image.index).hexdigest())
        lease.macos_binding = binding
        save(manager, lease, 'reserved')
        checked()
        os.mkdir(binding['stage'],0o700,dir_fd=parent);os.fsync(parent)
        with ExitStack() as stack:
            work = os.open(binding['stage'],FLAGS,dir_fd=parent);stack.callback(os.close,work)
            save(manager,lease,'building',work_identity=owned(work))
            os.mkdir(binding['metadata'],0o700,dir_fd=meta_parent);os.fsync(meta_parent)
            meta = os.open(binding['metadata'],FLAGS,dir_fd=meta_parent);stack.callback(os.close,meta)
            save(manager,lease,'building',meta_identity=owned(meta))
            for directory in sorted(image.directories,key=lambda x:(x.count('/'),x)):
                # Ancestors were created by this Host in the private directory.
                fd = os.dup(work)
                try:
                    parts=directory.split('/')
                    for part in parts[:-1]:
                        child=os.open(part,FLAGS,dir_fd=fd);os.close(fd);fd=child
                    os.mkdir(parts[-1],0o700,dir_fd=fd);os.fsync(fd)
                finally:os.close(fd)
            for path,raw,mode in image.files:
                checked();fd=os.dup(work)
                try:
                    parts=path.split('/')
                    for part in parts[:-1]:
                        child=os.open(part,FLAGS,dir_fd=fd);os.close(fd);fd=child
                    write_file(fd,parts[-1],raw,mode);os.fsync(fd)
                finally:os.close(fd)
            for field,raw in metadata(manager,lease).items():write_file(meta,field,raw)
            write_file(meta,'index',image.index);os.fsync(meta)
            write_file(work,'.git',('gitdir: '+str(manager.repo_root/'.git/worktrees'/binding['metadata'])+'\n').encode())
            os.fsync(work);check_files(manager,lease,work,meta)
            save(manager,lease,'prepared')
            checked()
            rename_exclusive(parent,binding['stage'],name);os.fsync(parent)
            checked()
            fd=open_bound(parent,name,lease.macos_binding['work_identity']);os.close(fd)
            save(manager,lease,'published')


def verify(manager, lease):
    with context(manager,lease) as (parent,meta_parent,pins,guard):
        binding=lease.macos_binding
        if binding.get('runtime_update'):fail('interrupted runtime merge requires explicit reconciliation')
        if binding.get('phase')!='published':fail('worktree publication is incomplete')
        if binding.get('result_update'):fail('interrupted result publication requires recovery')
        from backend.core.macos_worktree_result import verify as verify_result
        verify_result(manager,lease)
        if binding.get('index_update'):fail('interrupted index staging requires recovery')
        with ExitStack() as stack:
            work=open_bound(parent,target_name(manager,lease),binding['work_identity']);stack.callback(os.close,work)
            meta=open_bound(meta_parent,binding['metadata'],binding['meta_identity']);stack.callback(os.close,meta)
            check_files(manager,lease,work,meta);guard()
        if lease.state=='sealed':
            from backend.core.macos_worktree_seal import verify_fields
            verify_fields(lease)
        return dict(valid=True,head=binding['commit'],directory_identity=binding['work_identity'])


def erase(fd, volume, budget, depth=0):
    """Unlink only inside the pinned retired directory; never follow links."""
    if depth>64:fail('cleanup directory depth exceeds budget')
    for name in os.listdir(fd):
        budget[0]-=1
        if budget[0]<0:fail('cleanup entry budget exceeded; retain retirement record')
        info=os.stat(name,dir_fd=fd,follow_symlinks=False)
        if info.st_uid!=os.getuid() or info.st_dev!=volume:fail('foreign cleanup member')
        if stat.S_ISDIR(info.st_mode):
            child=os.open(name,FLAGS,dir_fd=fd)
            try:
                if wire.directory_identity(child)!=[info.st_dev,info.st_ino,info.st_uid]:fail('cleanup child replaced')
                erase(child,volume,budget,depth+1)
            finally:os.close(child)
            os.rmdir(name,dir_fd=fd)
        else:os.unlink(name,dir_fd=fd)
    os.fsync(fd)


def retire_one(parent, names, trash, identity, guard):
    found=[]
    for name in [*names,trash]:
        try:info=os.stat(name,dir_fd=parent,follow_symlinks=False)
        except FileNotFoundError:continue
        if not identity or not stat.S_ISDIR(info.st_mode) or [info.st_dev,info.st_ino,info.st_uid]!=identity:
            fail('unrecorded or replaced directory; retain evidence: '+name)
        found.append(name)
    if len(found)>1:fail('ambiguous worktree directory locations')
    if not found:return
    name=found[0];guard()
    if name!=trash:rename_exclusive(parent,name,trash);os.fsync(parent)
    fd=open_bound(parent,trash,identity)
    try:erase(fd,identity[0],[10000])
    finally:os.close(fd)
    guard()
    info=os.stat(trash,dir_fd=parent,follow_symlinks=False)
    if [info.st_dev,info.st_ino,info.st_uid]!=identity:fail('retirement directory replaced')
    os.rmdir(trash,dir_fd=parent);os.fsync(parent)


def retire(manager, lease, *, recovery=False):
    """Idempotently retire only recorded directory objects, not guessed paths."""
    from backend.core.storage.native_invocations import pending_native_invocations
    if lease.macos_binding.get('runtime_update'):fail('runtime merge must be reconciled before retirement')
    if lease.macos_binding.get('result_update'):fail('result publication must be reconciled before retirement')
    if pending_native_invocations(manager.storage,include_active=True):fail('native execution/recovery debt must be resolved before retirement')
    with context(manager,lease) as (parent,meta_parent,pins,guard):
        b=lease.macos_binding
        if b.get('version')!=1 or b.get('phase') not in ('reserved','building','prepared','published','removing','removed'):
            fail('unknown worktree lifecycle version/phase')
        # Names are durable Host data, but validate them again before mutation.
        nonce=b.get('metadata','').removeprefix('gitgo-')
        if (re.fullmatch('[0-9a-f]{32}',nonce) is None or b['stage']!='.pending-'+nonce or
                b['trash']!='.retiring-'+nonce or b['meta_trash']!='.retiring-'+nonce):fail('invalid retirement names')
        if b['phase']=='removed':
            for fd,names in ((parent,[target_name(manager,lease),b['stage'],b['trash']]),
                             (meta_parent,[b['metadata'],b['meta_trash']])):
                for name in names:
                    try:os.stat(name,dir_fd=fd,follow_symlinks=False)
                    except FileNotFoundError:continue
                    fail('removed lease has a new object at its old pathname')
            return lease
        result=b.get('retire_result', 'failed' if recovery else 'disposed')
        if result not in ('failed','disposed'):fail('invalid retirement intent')
        save(manager,lease,'removing',retire_result=result)
        retire_one(parent,[target_name(manager,lease),b['stage']],b['trash'],b.get('work_identity'),guard)
        retire_one(meta_parent,[b['metadata']],b['meta_trash'],b.get('meta_identity'),guard)
        lease.state=result
        if result=='failed':lease.error='Interrupted worktree creation retired; task was not resumed'
        save(manager,lease,'removed')
    return lease


def recover(manager, lease):
    if lease.macos_binding.get('runtime_update'):
        from backend.core.macos_worktree_runtime import merge
        return merge(manager,lease,observe=True)
    if lease.macos_binding.get('result_update'):
        from backend.core.macos_worktree_result import reconcile
        return reconcile(manager,lease)
    if lease.macos_binding.get('index_update') or lease.state in ('creating','failed') or lease.macos_binding.get('phase') in ('removing','removed'):
        return retire(manager,lease,recovery=lease.state!='disposed')
    verify(manager,lease)
    return lease


def copy_runtime_inputs(manager, lease, context_refs):
    """Read Host metadata through native owners; write only pinned fresh paths."""
    import json
    from backend.core.sandbox_read import ReadRoot
    inputs={}
    with ReadRoot(manager.repo_root) as root:
        def read(relative):
            try:
                with root.open('.gitgo/'+relative) as opened:return opened.read_bytes(max_bytes=4194304)
            except FileNotFoundError:return None
        for name in manager._RUNTIME_INPUT_NAMES:
            raw=read(name)
            if raw is not None:inputs[name]=raw;lease.runtime_offsets[name]=len(raw)
        for value in (context_refs or {}).values():
            ref=str((value or {}).get('pinned') or (value or {}).get('latest') or '')
            if not ref:continue
            match=re.fullmatch(r'context:([A-Za-z0-9_/-]+)@(latest|(?:sha256:)?[0-9a-f]{64})',ref)
            if not match or any(p in ('','.','..') for p in match[1].split('/')):fail('invalid pinned context reference')
            logical,selector=match.groups()
            reference=read('context_objects/refs/'+logical+'.json')
            digest=(json.loads(reference)['digest'] if selector=='latest' and reference is not None else selector.removeprefix('sha256:'))
            if type(digest) is not str or re.fullmatch('[0-9a-f]{64}',digest) is None:fail('context digest unavailable')
            blob=read('context_objects/blobs/'+digest+'.json')
            if blob is not None:inputs['context_objects/blobs/'+digest+'.json']=blob
            if reference is not None:inputs['context_objects/refs/'+logical+'.json']=reference
    if sum(map(len,inputs.values()))>4194304 or len(inputs)>128:fail('runtime metadata exceeds budget')
    if not inputs:
        with context(manager,lease):save(manager,lease,'published',runtime_input_digests={})
        return
    with context(manager,lease) as (parent,meta_parent,pins,guard):
        work=open_bound(parent,target_name(manager,lease),lease.macos_binding['work_identity'])
        try:
            os.mkdir('.gitgo',0o700,dir_fd=work);os.fsync(work)
            metadata_fd=os.open('.gitgo',FLAGS,dir_fd=work)
            try:
                for name,raw in inputs.items():
                    guard();fd=os.dup(metadata_fd)
                    try:
                        parts=name.split('/')
                        for part in parts[:-1]:
                            try:os.mkdir(part,0o700,dir_fd=fd);os.fsync(fd)
                            except FileExistsError:pass
                            child=os.open(part,FLAGS,dir_fd=fd);os.close(fd);fd=child
                        write_file(fd,parts[-1],raw);os.fsync(fd)
                    finally:os.close(fd)
                os.fsync(metadata_fd)
            finally:os.close(metadata_fd)
            guard()
            save(manager,lease,'published',runtime_input_digests={name:hashlib.sha256(raw).hexdigest() for name,raw in inputs.items()})
        finally:os.close(work)


def create(manager, *, process_id, task_id, snapshot_commit='', upstream_commits=(), mode='write',
           review_commit='', context_refs=None, resume_from=None):
    from backend.core.loop.worktree import WorktreeLease
    from backend.core.macos_git_checkout import validate_commit
    if mode not in ('write','review'):fail('invalid worktree mode')
    if not broker.active():fail('authorized project broker required')
    broker.project_binding(manager.repo_root)
    # Dirty-root snapshots and nonlinear patch application remain unsupported.
    # Explicit continuation uses a verified immutable business checkpoint.
    commit=review_commit or snapshot_commit
    if not commit:fail('a sealed project snapshot is required to create a worktree')
    validate_commit(commit)
    from itertools import islice
    upstream=list(islice(iter(upstream_commits),9))
    if len(upstream)>8:fail('select at most eight distinct upstream commits')
    for oid in upstream:validate_commit(oid)
    if len(set(upstream))!=len(upstream):fail('duplicate upstream commit')
    if upstream and (mode!='write' or review_commit):fail('review cannot materialize upstream commits')
    if resume_from is not None and (mode!='write' or review_commit):fail('review cannot resume unpublished work')
    process_id=manager._safe_id(process_id)
    original_commit=commit
    if resume_from is not None:
        from backend.core.macos_worktree_resume import validate_source
        validate_source(manager,resume_from,process_id=process_id,snapshot_commit=original_commit,
            upstream_commits=upstream,base_commit=upstream[-1] if upstream else commit)
    if upstream:
        from backend.core.sandbox_read import ReadRoot
        from backend.core.loop.native_invocation_scope import current_native_cancellation, _SCOPE
        from backend.core.storage.native_invocations import pending_native_invocations
        if _SCOPE.get() is not None or pending_native_invocations(manager.storage,include_active=True):fail('upstream admission requires a clean Host lifecycle boundary')
        cancellation=current_native_cancellation(manager.repo_root)
        with ReadRoot(manager.repo_root/'.git/objects') as root:
            for oid in upstream:
                kind=broker.query_git_objects(manager.repo_root,root,['cat-file','-t',oid],cancellation=cancellation)
                if kind.returncode or kind.stdout!='commit\n':fail('upstream must name a commit')
                related=broker.query_git_objects(manager.repo_root,root,['merge-base','--is-ancestor',commit,oid],cancellation=cancellation)
                if related.returncode or related.stdout:fail('nonlinear upstream requires protected patch merging')
                commit=oid
    continuation=''
    if resume_from is not None:
        # Reject an occupied destination before freezing the retained source.
        # Reservation below repeats this check to close a concurrent creator race.
        with parent_lock(manager):
            occupied=manager.storage.load_worktree_state(process_id)
            if occupied and (occupied.get('state')!='disposed' or
                    occupied.get('macos_binding') and occupied['macos_binding'].get('phase')!='removed'):
                fail('existing process lease must be recovered or disposed first')
        from backend.core.macos_worktree_resume import checkpoint
        continuation=checkpoint(manager,resume_from,process_id=process_id,snapshot_commit=original_commit,
            upstream_commits=upstream,base_commit=commit)
    with parent_lock(manager):
        previous=manager.storage.load_worktree_state(process_id)
        if previous and (previous.get('state')!='disposed' or
                previous.get('macos_binding') and previous['macos_binding'].get('phase')!='removed'):
            fail('existing process lease must be recovered or disposed first')
        lease=WorktreeLease(worktree_id=process_id,process_id=process_id,task_id=task_id,
            path=str(manager.worktrees_root/hashlib.sha256(process_id.encode()).hexdigest()[:20]),
            root_workspace=str(manager.repo_root),mode=mode,upstream_commits=upstream,continuation_commit=continuation,
            resumed_from_process_id=resume_from.process_id if resume_from is not None else '')
        manager._persist(lease)
    try:
        lease.base_commit=root_head(manager);manager._persist(lease)
        if continuation:publish(manager,lease,commit,continuation_commit=continuation)
        else:publish(manager,lease,commit)
        copy_runtime_inputs(manager,lease,context_refs)
        verify(manager,lease)
        lease.snapshot_commit=original_commit
        if review_commit:lease.result_commit=commit
        lease.state='review_ready' if review_commit else 'leased'
        manager._persist(lease)
        return lease
    except BaseException as exc:
        # Never assume an interrupted durable write succeeded. Recovery always
        # reloads the actual row and does not adopt unrecorded directory objects.
        try:
            value=manager.storage.load_worktree_state(process_id)
            saved=WorktreeLease.from_dict(value)
            if saved.macos_binding:
                saved.state='failed';saved.error=str(exc);manager._persist(saved)
                retire(manager,saved,recovery=True)
            else:
                saved.state='failed';saved.error=str(exc);manager._persist(saved)
        except Exception as cleanup:
            if hasattr(exc,'add_note'):exc.add_note('Worktree recovery retained: '+str(cleanup))
        raise


def resolve_authority(path):
    """Resolve only an active durable lease, never follow its .git as a grant."""
    from backend.core.storage import get_storage
    from backend.core.loop.worktree import AgentWorktreeManager,WorktreeLease
    binding=broker._SESSION.get()
    if binding is None:fail('authorized project broker required')
    project=Path(binding['project']);path=Path(path).absolute()
    broker.project_binding(project)
    if path==project:return project,None,None
    store=get_storage(project)
    records=[r for r in store.list_worktree_states(active_only=True) if r.get('path')==str(path)]
    if len(records)!=1:fail('path is not a registered worktree of this project')
    lease=WorktreeLease.from_dict(records[0])
    if not lease.macos_binding or lease.state not in ('leased','review_ready','sealed'):
        fail('worktree is not available for execution')
    from backend.core.loop.native_invocation_scope import _SCOPE
    scope=_SCOPE.get()
    if scope is not None and getattr(scope['ctx'].process,'process_id',None)!=lease.process_id:
        fail('task does not own this worktree lease')
    manager=AgentWorktreeManager(project,store)
    verify(manager,lease)
    return project,manager,lease
