"""Bounded Host publication of checked immutable bytes, never repository Git.

The existing prepared promotion revision owns temporary identities. Recovery
only retires recorded temporaries; it neither replays nor rolls back outputs.
Existing parent directories are required. Cooperating writers share the file
service locks; arbitrary external writers are not a filesystem transaction.
"""
import copy
import hashlib
import os
import stat
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from backend.core import macos_broker_client as broker, macos_broker_protocol as wire
from backend.core import macos_worktree as life, macos_worktree_index as index
from backend.core.sandbox_read import ReadRoot
from backend.core.tools.file_mutation import FileMutation, cancellation_requested, runtime_is_current
from backend.core.tools.file_mutation_batch import FileMutationBatch, checked_file_batch


def digest(value):
    return hashlib.sha256(value[0]).hexdigest() if value is not None else ''


def image(manager, commit):
    from backend.core.macos_git_checkout import validate_commit
    validate_commit(commit)
    from backend.core.loop.native_invocation_scope import current_native_cancellation
    with ReadRoot(manager.repo_root/'.git/objects') as root:
        value=broker.checkout_git_commit(manager.repo_root,root,commit,cancellation=current_native_cancellation(manager.repo_root))
    if value.commit!=commit:life.fail('promotion checkout commit differs')
    return {name:(raw,mode) for name,raw,mode in value.files}


@contextmanager
def destination(manager, paths, intent=None):
    """Open only existing no-follow parents; pin their identity until retirement."""
    index.validate_entries([(p,'0'*40,0o644) for p in paths])
    grant=broker.project_binding(manager.repo_root)
    with ExitStack() as stack:
        rootfd=wire.open_directory(manager.repo_root);stack.callback(os.close,rootfd)
        project=life.owned(rootfd)
        if project!=grant['identity']:life.fail('promotion root replaced')
        root=stack.enter_context(ReadRoot(manager.repo_root))
        if (root.identity.volume,root.identity.file_id)!=tuple(project[:2]):life.fail('promotion read root replaced')
        parents={};pins={}
        for name in paths:
            relative=Path(name).parent.as_posix()
            if relative in parents:continue
            fd=os.dup(rootfd);stack.callback(os.close,fd)
            if relative!='.':
                for part in relative.split('/'):
                    child=os.open(part,life.FLAGS,dir_fd=fd);stack.callback(os.close,child);fd=child
                    life.owned(fd)
            parents[relative]=fd;pins[relative]=life.owned(fd)
        if intent is not None and (intent.get('project')!=project or intent.get('parents')!=pins):
            life.fail('promotion destination differs from prepared identity')
        def guard():
            if broker.project_binding(manager.repo_root)['identity']!=project:life.fail('promotion grant changed')
            for relative,pin in pins.items():
                fd=wire.open_directory(manager.repo_root/relative)
                try:
                    if life.owned(fd)!=pin:life.fail('promotion parent replaced')
                finally:os.close(fd)
        def current(name):
            guard()
            try:return index.selected_file(root,rootfd,name)
            except FileNotFoundError:return None
        guard()
        yield parents,dict(version=1,project=project,parents=pins,temporaries=[]),guard,current


def retire(manager,intent):
    """No target effects. Unknown temporary ownership is retained for inspection."""
    if intent is None:return
    if type(intent) is not dict or intent.get('version')!=1:life.fail('invalid promotion write intent')
    rows=intent.get('temporaries')
    if type(rows) is not list or len(rows)>128:life.fail('invalid promotion temporary list')
    paths=[]
    for row in rows:
        if type(row) is not dict or set(row)!={'path','identity'}:life.fail('invalid promotion temporary')
        path=row['path']
        if type(path) is not str:life.fail('invalid promotion temporary path')
        name=Path(path).name
        import re
        if re.fullmatch(r'\.gitgo-promotion-[0-9a-f]{32}\.tmp',name) is None:life.fail('invalid promotion temporary name')
        paths.append(path)
    if len(set(paths))!=len(paths):life.fail('duplicate promotion temporary')
    # All parents (including deletion-only ones) remain bound to this record.
    parent_paths=[('probe' if p=='.' else p+'/probe') for p in intent.get('parents',{})]
    with destination(manager,parent_paths,intent) as (parents,_,guard,current):
        owned=[]
        for row in rows:
            path=Path(row['path']);fd=parents.get(path.parent.as_posix())
            if fd is None:life.fail('temporary parent absent from intent')
            try:info=os.stat(path.name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:continue
            identity=[info.st_dev,info.st_ino,info.st_uid]
            if (identity!=row['identity'] or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid()):
                life.fail('unconfirmed promotion temporary; retain recovery debt')
            owned.append((fd,path.name,identity))
        for fd,name,identity in owned:
            guard();info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if [info.st_dev,info.st_ino,info.st_uid]!=identity:life.fail('promotion temporary replaced before cleanup')
            os.unlink(name,dir_fd=fd);os.fsync(fd)


def publish_file(parent, temporary, leaf, *, absent):
    if absent:
        life.rename_exclusive(parent,temporary,leaf)
    else:
        os.replace(temporary,leaf,src_dir_fd=parent,dst_dir_fd=parent)
    os.fsync(parent)


def commit(manager, changed, base, tip, args, prepare, *, guard_paths=(),input_paths=()):
    from backend.core.loop.promotion_candidate import _raise
    changed=list(changed);paths=list(dict.fromkeys([*guard_paths,*changed]))
    if not changed or len(changed)>128 or len(set(changed))!=len(changed):life.fail('invalid promotion write members')
    index.validate_entries([(p,'0'*40,0o644) for p in paths])
    before,after=image(manager,base),image(manager,tip)
    if set(changed)!={p for p in set(before)|set(after) if before.get(p)!=after.get(p)}:
        life.fail('promotion paths differ from complete immutable delta')
    from backend.core.macos_worktree_result import scan_files
    scan_files(manager,[(name,*after[name]) for name in changed if name in after])
    candidates=[]
    for name in changed:
        old,new=before.get(name),after.get(name)
        a=old[0] if old else b'';b=new[0] if new else b''
        try:
            text_a,text_b=a.decode('utf8'),b.decode('utf8');view=b'\0' not in a+b
        except UnicodeError:text_a=text_b='';view=False
        candidates.append(FileMutation('write_file' if new else 'delete_file',manager.repo_root,manager.repo_root/name,
            digest(old),text_a if view else '',text_b if view else '',after_bytes=b,text_view_available=view))
    # Modes and absence participate in the exact feedback/approval projection.
    import json
    projection=hashlib.sha256(json.dumps([[p,digest(before.get(p)),before.get(p,(None,None))[1],
        digest(after.get(p)),after.get(p,(None,None))[1]] for p in sorted(changed)],separators=(',',':')).encode()).hexdigest()
    batch=FileMutationBatch(manager.repo_root,tuple(candidates),operation='promote_agent_changes',projection_version=projection)
    with destination(manager,paths) as (parents,intent,guard,current):
        def versions_match():return all(current(p)==before.get(p) for p in paths)
        with checked_file_batch(batch,args,check_versions=versions_match,
                guard_paths=[manager.repo_root/p for p in {*paths,*input_paths}]) as inspection:
            if inspection.result.get('error'):_raise(inspection.result)
            versions=[dict(path=p,before_sha256=digest(before.get(p)),after_sha256=digest(after.get(p))) for p in paths]
            # Reserve names before creating anything; save each created identity
            # before bytes/rename. An unacknowledged creation remains explicit debt.
            intent['temporaries']=[dict(path=str(Path(p).parent/('.gitgo-promotion-'+uuid.uuid4().hex+'.tmp')),identity=None)
                                   for p in changed if p in after]
            prepare(versions,native_write=copy.deepcopy(intent))
            temps=iter(intent['temporaries'])
            try:
                for name in changed:
                    guard()
                    if cancellation_requested() or not runtime_is_current():raise RuntimeError('promotion cancelled or stale')
                    if current(name)!=before.get(name):raise RuntimeError('promotion input changed')
                    parent=parents[Path(name).parent.as_posix()];leaf=Path(name).name
                    if name not in after:
                        os.unlink(leaf,dir_fd=parent);os.fsync(parent)
                    else:
                        row=next(temps);temporary=Path(row['path']).name
                        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
                        try:
                            info=os.fstat(fd);row['identity']=[info.st_dev,info.st_ino,info.st_uid]
                            prepare(versions,native_write=copy.deepcopy(intent))
                            data,mode=after[name];remaining=memoryview(data)
                            while remaining:
                                n=os.write(fd,remaining)
                                if not n:raise OSError('short promotion write')
                                remaining=remaining[n:]
                            os.fchmod(fd,mode);os.fsync(fd)
                        finally:os.close(fd)
                        guard()
                        if cancellation_requested() or not runtime_is_current():raise RuntimeError('promotion cancelled or stale')
                        if current(name)!=before.get(name):raise RuntimeError('promotion input changed before replacement')
                        info=os.stat(temporary,dir_fd=parent,follow_symlinks=False)
                        if [info.st_dev,info.st_ino,info.st_uid]!=row['identity']:raise RuntimeError('promotion temporary replaced')
                        publish_file(parent,temporary,leaf,absent=before.get(name) is None)
                    if current(name)!=after.get(name):raise RuntimeError('promotion output differs')
                if not all(current(p)==after.get(p) for p in paths):raise RuntimeError('promotion batch output differs')
                retire(manager,intent)
            except Exception as exc:
                cleanup_error=None
                try:retire(manager,intent)
                except Exception as cleanup:cleanup_error=str(cleanup)
                rows=[]
                for name in changed:
                    state='ambiguous'
                    try:
                        value=current(name)
                        if value==after.get(name):state='committed'
                        elif value==before.get(name):state='not_committed'
                    except Exception:pass
                    rows.append(dict(path=name,effect_state=state))
                states={r['effect_state'] for r in rows}
                effect='ambiguous' if 'ambiguous' in states else 'partial' if len(states)>1 else next(iter(states))
                _raise({**inspection.result,'error':'PROMOTION_EFFECT_AMBIGUOUS','detail':str(exc),'file_results':rows,
                        'effect_state':effect,'execution_state':'completed',**({'cleanup_error':cleanup_error} if cleanup_error else {})})
            result={**inspection.result,'file_results':[{**c.binding(),'path':c.path.relative_to(manager.repo_root).as_posix(),
                'after_sha256':c.after_sha256,'effect_state':'committed','execution_state':'completed',
                **({'recovery_ref':f'git:{base}:{c.path.relative_to(manager.repo_root).as_posix()}','recovery_kind':'git_baseline'}
                    if c.path.relative_to(manager.repo_root).as_posix() in before else {})} for c in candidates],
                'effect_state':'committed','knowledge_excerpts':[]}

            try:
                from backend.core.knowledge.excerpts import changed_excerpts,MAX_EXCERPTS
                for c in candidates:
                    if c.text_view_available and c.operation!='delete_file':
                        result['knowledge_excerpts'].extend(changed_excerpts(c.path.relative_to(manager.repo_root).as_posix(),
                            c.before,c.after,c.after_sha256)[:max(0,MAX_EXCERPTS-len(result['knowledge_excerpts']))])
            except Exception as exc:
                result['warnings']=[dict(code='SOURCE_EXCERPTS_UNAVAILABLE',error_type=type(exc).__name__,
                    message='Promotion committed; source excerpt observation failed. Preserve the completed effect.')]
            if result['lesson_feedback']['lookup']['state']=='failed':
                result['lesson_recall_warning']=dict(code='LESSON_RECALL_UNAVAILABLE',
                    message='Promotion committed with ordinary advice unavailable; mandatory checks were evaluated separately.')
            return result
