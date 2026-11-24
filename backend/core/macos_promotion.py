"""Bounded linear publication through the existing promotion ledger/file service.

Sealed immutable result refs retain the selected commits; no user HEAD/index,
repository commands, mutable integration checkout or implicit merge is used.
"""
import copy
import re
from itertools import islice

from backend.core import macos_broker_client as broker, macos_worktree as life
from backend.core import macos_promotion_snapshot as snapshot, macos_promotion_write as writer
from backend.core.sandbox_read import ReadRoot
from backend.core.loop.worktree import WorktreeLease, WorktreeError
from backend.core.tools.file_mutation import cancellation_requested, runtime_is_current
from backend.core.tools.file_mutation_batch import file_writer_locks


def checked(manager):
    from backend.core.loop.native_invocation_scope import _SCOPE,current_native_owner,current_native_cancellation
    from backend.core.storage.native_invocations import pending_native_invocations
    if pending_native_invocations(manager.storage,include_active=True):
        life.fail('native recovery debt blocks publication')
    if _SCOPE.get() is not None:
        current_native_owner(manager.repo_root) # validate the actual Pipeline owner
        storage=getattr(_SCOPE.get()['ctx'],'storage',None)
        if storage is None or storage.paths.project_id!=manager.storage.paths.project_id or storage.paths.project_root!=manager.storage.paths.project_root:
            life.fail('promotion journal belongs to another project')
    stop=current_native_cancellation(manager.repo_root)
    if stop is not None and stop.is_set():life.fail('promotion cancelled')
    broker.project_binding(manager.repo_root)
    if cancellation_requested() or not runtime_is_current():life.fail('promotion cancelled or stale')


def anchors(manager, commits, snapshot_commit):
    from backend.core import macos_worktree_result as result,macos_worktree_seal as sealing
    rows=manager.storage.list_worktree_states();found={}
    for commit in commits:
        candidates=[WorktreeLease.from_dict(r) for r in rows if r.get('result_commit')==commit
                    and r.get('snapshot_commit')==snapshot_commit and r.get('root_workspace')==str(manager.repo_root)
                    and r.get('mode')=='write' and r.get('state') in ('sealed','disposed')]
        if not candidates:life.fail('promotion requires a durable sealed result for every commit')
        lease=sorted(candidates,key=lambda r:r.process_id)[0]
        sealing.verify_fields(lease)
        if lease.macos_binding.get('project')!=broker.project_binding(manager.repo_root)['identity']:
            life.fail('sealed result belongs to another project identity')
        result.verify(manager,lease)
        found[commit]=dict(process_id=lease.process_id,ref=lease.macos_binding['result']['ref'])
    return found


def dependencies(manager, image, outputs, rows):
    paths=[]
    if len(rows)>128:life.fail('too many publication input dependencies')
    for row in rows:
        if type(row) is not dict or type(row.get('path')) is not str or re.fullmatch('[0-9a-f]{64}',str(row.get('sha256',''))) is None:
            life.fail('publication dependency requires an exact path and SHA-256')
        paths.append(row['path'])
    if not paths:return
    # destination() does not create directories. Missing/unsafe inputs fail closed.
    with writer.destination(manager,list(dict.fromkeys(paths))) as (_,__,guard,current):
        for row in rows:
            value=image.get(row['path']) if row['path'] in outputs else current(row['path'])
            if value is None or writer.digest(value)!=row['sha256']:
                raise WorktreeError('VALIDATION_INPUT_CHANGED: pinned input requires recheck: '+row['path'])
        guard()


def run(manager, *, snapshot_commit, ordered_commits, promotion_id, artifact_paths,
        input_dependencies, prior, head_ref, save, service_args):
    from backend.core.macos_git_checkout import validate_commit
    from backend.core.loop.promotion_candidate import _raise
    validate_commit(snapshot_commit)
    commits=list(islice(iter(ordered_commits),9))
    if len(commits)>8 or len(set(commits))!=len(commits):life.fail('select at most eight distinct linear result commits')
    for commit in commits:validate_commit(commit)
    artifacts=list(islice(iter(artifact_paths),129))
    if len(artifacts)>128:life.fail('too many delivery artifacts')
    if artifacts:writer.index.validate_entries([(p,'0'*40,0o644) for p in artifacts if p!='*'])
    requested=list(islice(iter(input_dependencies),129))
    if len(requested)>128:life.fail('too many publication dependencies')
    checked(manager)
    if prior and prior.get('native_linear') is not True:life.fail('legacy promotion requires explicit migration')
    if prior and prior.get('state') not in ('prepared','committed'):life.fail('unknown publication record state')
    all_commits=list(dict.fromkeys([*prior.get('applied_commits',[]),*commits]))
    if len(all_commits)>128:life.fail('promotion result history exceeds budget')
    retained=anchors(manager,all_commits,snapshot_commit)
    if any(retained.get(c)!=v for c,v in prior.get('native_result_refs',{}).items()):life.fail('promotion result anchor changed')
    reconciled=False
    if prior.get('state')=='prepared':
        with file_writer_locks(manager.repo_root,[manager.repo_root/p for p in prior['paths']],service_args):
            checked(manager);writer.retire(manager,prior.get('native_write'))
            if snapshot.matches_record(manager,prior['result_commit'],prior['paths'],prior,'after'):
                prior={**{k:v for k,v in prior.items() if k!='previous'},'state':'committed','reconciled_effect':True}
                head_ref=save(prior,head_ref);reconciled=True
            elif snapshot.matches_record(manager,prior['before_commit'],prior['paths'],prior,'before'):
                prior=prior['previous'];head_ref=save(prior,head_ref)
            else:
                _raise(dict(error='PROMOTION_EFFECT_AMBIGUOUS',effect_state='ambiguous',execution_state='unknown',
                    detail='Prepared publication differs from both images; inspect without replay'))
    applied=list(prior.get('applied_commits',[]));pending=[c for c in commits if c not in applied]
    base=prior.get('result_commit') or snapshot_commit;tip=base
    if pending:
        with ReadRoot(manager.repo_root/'.git/objects') as root:
            for commit in pending:
                checked(manager)
                from backend.core.loop.native_invocation_scope import current_native_cancellation
                related=broker.query_git_objects(manager.repo_root,root,['merge-base','--is-ancestor',tip,commit],
                    cancellation=current_native_cancellation(manager.repo_root))
                if related.returncode or related.stdout:life.fail('nonlinear publication requires protected patch merging')
                tip=commit
    before=writer.image(manager,base);after=before if tip==base else writer.image(manager,tip)
    changed=sorted(p for p in set(before)|set(after) if before.get(p)!=after.get(p))
    paths=sorted(set(prior.get('paths',[]))|set(changed))
    writer.index.validate_entries([(p,'0'*40,0o644) for p in paths])
    deps=[*prior.get('input_dependencies',[]),*requested]
    dependencies(manager,after,paths,deps)
    if not snapshot.matches_record(manager,base,paths,prior,'after'):life.fail('workspace changed after publication baseline')
    record=dict(native_linear=True,snapshot_commit=snapshot_commit,before_commit=base,result_commit=tip,
        applied_commits=list(dict.fromkeys([*applied,*pending])),paths=paths,input_dependencies=deps,
        native_result_refs={c:retained[c] for c in [*applied,*pending]})
    if 'file_versions' in prior:record['file_versions']=prior['file_versions']
    def prepare(versions=None,*,native_write=None):
        nonlocal head_ref
        checked(manager)
        if anchors(manager,all_commits,snapshot_commit)!=retained:life.fail('sealed result anchor changed before publication')
        dependencies(manager,after,paths,deps)
        if versions is not None:record['file_versions']=versions
        if native_write is not None:record['native_write']=native_write
        previous=prior or dict(native_linear=True,state='committed',snapshot_commit=snapshot_commit,result_commit=base,
            applied_commits=applied,paths=[],native_result_refs={})
        head_ref=save({**copy.deepcopy(record),'state':'prepared','previous':copy.deepcopy(previous)},head_ref)
    facts={}
    if pending or not prior:
        if changed:
            facts=writer.commit(manager,changed,base,tip,service_args,prepare,guard_paths=paths,input_paths=[d['path'] for d in deps])
        else:
            with file_writer_locks(manager.repo_root,[manager.repo_root/p for p in {*paths,*(d['path'] for d in deps)}],service_args):
                if not snapshot.matches_record(manager,base,paths,prior,'after'):life.fail('workspace changed before empty publication')
                prepare([dict(path=p,before_sha256=writer.digest(before.get(p)),after_sha256=writer.digest(after.get(p))) for p in paths])
        try:save({**record,'state':'committed'},head_ref)
        except Exception as exc:
            _raise({**facts,'error':'PROMOTION_METADATA_PENDING','effect_state':'committed' if changed else 'not_committed',
                    'execution_state':'completed','detail':'Filesystem effect needs record reconciliation: '+str(exc)})
    files=[];notices=[]
    candidates=list(dict.fromkeys([*changed,*(paths if reconciled or not pending else []),*artifacts]))
    if len(candidates)>128:notices.append('Promotion artifact metadata limited to 128 files.')
    for p in candidates[:128]:
        if p=='*':continue
        value=after.get(p)
        try:
            if value is None or b'\0' in value[0]:raise ValueError('not a bounded text artifact')
            value[0].decode('utf-8-sig')
            files.append(dict(path=p,after_sha256=writer.digest(value),evidence_kind='committed_write' if p in changed else 'snapshot_verified'))
        except (ValueError,UnicodeError):notices.append(p+': not a bounded text artifact')
    return {**facts,**dict(promoted=True,changed_files=changed,commits=commits,applied_commits=pending,
        already_promoted=[c for c in commits if c in applied],snapshot_commit=snapshot_commit,result_commit=tip,
        reconciled_effect=reconciled,files=files,artifact_notices=notices,verification_basis='raw_file_versions')}
