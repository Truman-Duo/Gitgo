"""Opt-in ordinary-user Git read -> privileged broker acceptance.

Disposable project, temporary launchd broker, original native invocation journal.
No permanent installation, reboot, real project changes or production enablement.
Failure retains recovery evidence; never retires uncertain task identities.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime
if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid()==0:
    stage_runtime(__file__,support=('check_macos_user_broker.py','check_macos_git_read.py','macos_git_packaged_support.py','check_macos_worktree_client.py','check_macos_promotion_client.py'))
if not getattr(sys,'frozen',False):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core import macos_broker_protocol as wire
from backend.core import macos_broker_service as broker
from backend.core import macos_identity as identity
from backend.core.sandbox_macos import load_state
from check_macos_user_broker import launch_broker, wait_broker, launchctl, wait_for, wait_unloaded
import macos_git_packaged_support as packaged


def worktree_fault_modes(results):
    # A result batch has its own seven crash boundaries. The outer harness runs
    # the older worktree boundaries in a separate, complete broker batch.
    return () if results else ('reserved','building','prepared','published','retiring','unrecorded')


def expected_calls(consumers=False,objects=False,checkout=False,worktrees=False,index=False,results=False,compare=False,seal=False,runtime_merge=False,finalize=''):
    return ((10 if consumers else 3)+(3 if objects else 0)+(2 if checkout else 0)
            +((2+len(worktree_fault_modes(results))) if worktrees else 0)
            +(17 if index else 0)+((17 if finalize=='promotion_review' else 40 if finalize=='promotion_read' else 31 if finalize=='resume' else 23 if finalize=='upstream' else 41 if finalize=='boundaries' else 36 if finalize=='pending' else 25 if finalize=='ack' else 13 if runtime_merge else 23 if seal else 40) if results else 0)+(5+int(worktrees) if compare else 0))


def object_fixture_commit(runner,tree_oid,parent):
    """The nonempty fixture descends from the empty snapshot used by upstream tests."""
    import hashlib
    commit=runner.run(['commit-tree',tree_oid,'-p',parent,'-m','protected object acceptance'],timeout=20,
        author_identity={'name':'Gitgo Test','email':'test@local.invalid','date':'1700000000 +0000'})
    data=('tree '+tree_oid+'\nparent '+parent+'\nauthor Gitgo Test <test@local.invalid> 1700000000 +0000\n'
        'committer Gitgo Test <test@local.invalid> 1700000000 +0000\n\nprotected object acceptance\n').encode()
    expected=hashlib.sha1(b'commit '+str(len(data)).encode()+b'\0'+data).hexdigest()
    return commit,expected


def upstream_rejections(manager,base,tip,fork,blob):
    """Rejected inputs must leave no worktree authority or checkout behind."""
    from backend.core.loop.worktree import WorktreeError
    rows=manager.storage.list_worktree_states()
    directories=set(manager.worktrees_root.iterdir())
    metadata=set((manager.repo_root/'.git/worktrees').iterdir())
    head=(manager.repo_root/'.git/HEAD').read_bytes()
    cases=[]
    for name,start,commits,extra,message in (
        ('fork',tip,[fork],{},'nonlinear upstream'),
        ('reverse',tip,[base],{},'nonlinear upstream'),
        ('blob',base,[blob],{},'upstream must name a commit'),
        ('duplicate',base,[tip,tip],{},'duplicate upstream'),
        ('too_many',base,[tip]*9,{},'at most eight'),
        ('review',base,[tip],{'mode':'review'},'review cannot materialize')):
        process_id='reject-upstream-'+name
        assert manager.storage.load_worktree_state(process_id) is None
        try:manager.create(process_id=process_id,task_id='upstream-rejection',snapshot_commit=start,upstream_commits=commits,**extra)
        except WorktreeError as exc:assert message in str(exc),(name,str(exc))
        else:raise AssertionError('Invalid upstream accepted: '+name)
        assert manager.storage.list_worktree_states()==rows
        assert set(manager.worktrees_root.iterdir())==directories
        assert set((manager.repo_root/'.git/worktrees').iterdir())==metadata
        assert (manager.repo_root/'.git/HEAD').read_bytes()==head
        cases.append(dict(case=name,rejected=True,no_worktree_side_effect=True))
    return cases


def promotion_read_cases(manager,empty,commit):
    """Mutate only an exclusive disposable fixture; product comparison is read-only."""
    from backend.core.loop.promotion import _matches
    target=manager.repo_root/'data.bin';assert not os.path.lexists(target)
    cases=[]
    def check(name,oid,expected):
        actual=_matches(manager,oid,['data.bin'])
        assert actual is expected,(name,actual,expected)
        cases.append(dict(case=name,matched=True,comparison=actual))
    check('missing-business-file',commit,False)
    try:
        with target.open('xb') as stream:stream.write(b'gitgo protected object\0\xff\r\n')
        target.chmod(0o644);check('exact-binary',commit,True)
        target.chmod(0o755);check('executable-mode-drift',commit,False)
        target.chmod(0o644);target.write_bytes(b'user edited after snapshot');check('content-drift',commit,False)
    finally:target.unlink()
    check('expected-absence',empty,True)
    try:
        target.symlink_to(manager.repo_root/'nonexistent-comparison-fixture');check('dangling-symlink',empty,False)
    finally:target.unlink()
    try:
        target.mkdir();check('directory-in-place-of-file',empty,False)
    finally:target.rmdir()
    assert not os.path.lexists(target)
    return cases


def promotion_record_cases(manager,empty,commit):
    """Exercise the durable promotion recovery matcher on an exclusive fixture."""
    import hashlib
    from backend.core.loop.promotion_candidate import matches_record
    target=manager.repo_root/'data.bin';assert not os.path.lexists(target)
    data=b'gitgo protected object\0\xff\r\n';digest=hashlib.sha256(data).hexdigest()
    record={'file_versions':[{'path':'data.bin','before_sha256':'','after_sha256':digest}]}
    cases=[]
    def check(name,oid,side,expected,value=record):
        actual=matches_record(manager,oid,['data.bin'],value,side)
        assert actual is expected,(name,actual,expected)
        cases.append(dict(case='record-'+name,matched=True,comparison=actual))
    check('before-absent',empty,'before',True)
    check('absent-wrong-digest',empty,'after',False)
    try:
        with target.open('xb') as stream:stream.write(data)
        target.chmod(0o644)
        check('after-exact',commit,'after',True)
        check('present-wrong-digest',commit,'before',False)
        target.chmod(0o755);check('mode-drift',commit,'after',False)
        target.chmod(0o644);target.write_bytes(b'foreign edit');check('content-drift',commit,'after',False)
    finally:target.unlink()
    try:
        target.symlink_to(manager.repo_root/'nonexistent-record-fixture')
        check('dangling-absence',empty,'before',False)
    finally:target.unlink()
    try:
        target.mkdir();check('directory-absence',empty,'before',False)
    finally:target.rmdir()
    # These malformed records must be rejected before any native invocation.
    for name,value in [('null',None),('missing',[]),('duplicate',record['file_versions']*2),
                       ('foreign',[dict(record['file_versions'][0],path='foreign')]),
                       ('bad-digest',[dict(record['file_versions'][0],before_sha256='invalid')]),
                       ('extra-key',[dict(record['file_versions'][0],unknown=True)])]:
        check(name,empty,'before',False,{'file_versions':value})
    assert not os.path.lexists(target)
    return cases


def promotion_write_cases(manager,empty,commit,*,config=None,uid=None):
    import copy,json
    from backend.core import macos_promotion_write as write
    from backend.core.loop.promotion_candidate import matches_record
    from backend.core.knowledge.manager import LessonManager
    LessonManager.bind_storage(manager.repo_root,manager.storage)
    target=manager.repo_root/'data.bin';assert not os.path.lexists(target)
    cases=[];intents=[]
    args={'_mutation_lock_root':str(manager.storage.paths.state_home/'mutation-locks'),
          '_lesson_context':{'state_home':str(manager.storage.paths.state_home)}}
    # Normal cases also persist their intent in the canonical promotion ledger.
    key=manager._ref_id('promotion-proof-normal');head=manager.storage.get_state_ref('agent_promotion',key)
    assert head is None
    def prepare(versions,*,native_write):
        nonlocal head
        record=dict(file_versions=versions,native_write=native_write)
        ref=manager.storage.put_blob(json.dumps(record).encode(),media_type='application/json')
        import uuid
        assert manager.storage.commit_state_revision('agent_promotion',key,ref,expected_ref=head,revision_key=uuid.uuid4().hex)
        head=ref;intents.append(copy.deepcopy(native_write))
    for name,base,tip in [('create',empty,commit),('delete',commit,empty)]:
        result=write.commit(manager,['data.bin'],base,tip,args,prepare)
        assert result['effect_state']=='committed'
        if name=='create':assert target.read_bytes()==b'gitgo protected object\0\xff\r\n' and target.stat().st_mode&0o777==0o644
        else:assert not os.path.lexists(target)
        write.retire(manager,intents[-1]);assert not list(manager.repo_root.glob('.gitgo-promotion-*.tmp'))
        cases.append(dict(case='write-'+name,matched=True,effect='committed'))
    for mode in ('created','applied'):
        if config is not None:
            child=subprocess.run(packaged.command(Path(__file__).with_name('check_macos_promotion_client.py'),
                '--config',config,'--empty',empty,'--commit',commit,'--mode',mode,'--uid',uid),capture_output=True,text=True)
            assert child.returncode==86,(mode,child.returncode,child.stderr)
        else:
            from check_macos_promotion_client import exercise
            assert exercise(manager,empty,commit,mode,hard_exit=False)==86
        key=manager._ref_id('promotion-proof-'+mode)
        record=json.loads(manager.storage.read_blob(manager.storage.get_state_ref('agent_promotion',key)))
        temporary=manager.repo_root/record['native_write']['temporaries'][0]['path']
        assert temporary.exists()==(mode=='created')
        before=target.read_bytes() if target.exists() else None
        assert before==(b'gitgo protected object\0\xff\r\n' if mode=='applied' else None)
        write.retire(manager,record['native_write']);write.retire(manager,record['native_write'])
        assert not temporary.exists() and (target.read_bytes() if target.exists() else None)==before
        assert matches_record(manager,commit if mode=='applied' else empty,['data.bin'],record,'after' if mode=='applied' else 'before')
        if target.exists():target.unlink() # disposable acceptance fixture, never product recovery
        cases.append(dict(case='write-'+mode,matched=True,exit_code=86,hard_exit=config is not None,
            effect='committed' if mode=='applied' else 'not_committed',temporary_retired=True,no_replay=True))
    return cases


def promotion_flow_cases(manager,lease,*,config=None,uid=None):
    import json
    from backend.core import macos_promotion_write as write
    from backend.core.knowledge.manager import LessonManager
    LessonManager.bind_storage(manager.repo_root,manager.storage)
    target=manager.repo_root/'data.bin';assert not os.path.lexists(target)
    baseline=b'gitgo protected object\0\xff\r\n';output=b'published result fixture'
    cases=[]
    def invoke(name):return manager.promote(snapshot_commit=lease.snapshot_commit,ordered_commits=[lease.result_commit],promotion_id=name)
    try:
        with target.open('xb') as stream:stream.write(baseline)
        target.chmod(0o644)
        result=invoke('flow-normal')
        assert result['effect_state']=='committed' and result['changed_files']==['data.bin'] and target.read_bytes()==output
        cases.append(dict(case='flow-normal',matched=True,effect='committed'))
        result=invoke('flow-normal')
        assert result['applied_commits']==[] and result['already_promoted']==[lease.result_commit] and target.read_bytes()==output
        cases.append(dict(case='flow-repeat',matched=True,replayed=False))
        for mode in ('flow-created','flow-applied'):
            target.write_bytes(baseline)
            if config is not None:
                child=subprocess.run(packaged.command(Path(__file__).with_name('check_macos_promotion_client.py'),
                    '--config',config,'--empty',lease.snapshot_commit,'--commit',lease.result_commit,'--mode',mode,'--uid',uid),capture_output=True,text=True)
                assert child.returncode==86,(mode,child.returncode,child.stdout[-2000:],child.stderr[-4000:])
            else:
                from check_macos_promotion_client import exercise_formal
                assert exercise_formal(manager,lease.snapshot_commit,lease.result_commit,mode,hard_exit=False)==86
            key=manager._ref_id(mode);row=json.loads(manager.storage.read_blob(manager.storage.get_state_ref('agent_promotion',key)))
            assert row['state']=='prepared' and row['native_linear']
            temporary=manager.repo_root/row['native_write']['temporaries'][0]['path']
            assert temporary.exists()==(mode=='flow-created')
            assert target.read_bytes()==(baseline if mode=='flow-created' else output)
            calls=[];original=write.publish_file
            def observing(*a,**kw):calls.append(1);return original(*a,**kw)
            write.publish_file=observing
            try:result=invoke(mode)
            finally:write.publish_file=original
            assert len(calls)==(1 if mode=='flow-created' else 0)
            assert result['reconciled_effect']==(mode=='flow-applied') and target.read_bytes()==output
            row=json.loads(manager.storage.read_blob(manager.storage.get_state_ref('agent_promotion',key)))
            assert row['state']=='committed' and not temporary.exists()
            assert not list(manager.repo_root.glob('.gitgo-promotion-*.tmp'))
            cases.append(dict(case=mode,matched=True,exit_code=86,hard_exit=config is not None,
                writes_on_explicit_retry=len(calls),reconciled_effect=result['reconciled_effect'],state='committed',temporary_retired=True))
    finally:target.unlink()
    return cases


def client(config,uid,oids,consumers=False,objects=False,checkout=False,worktrees=False,index=False,results=False,compare=False,seal=False,runtime_merge=False,finalize=''):
    if os.getuid()!=uid or os.geteuid()!=uid or uid==0:
        raise RuntimeError('Git Host must run as the ordinary login user')
    from backend.core.macos_broker_client import broker_session, read_git_objects
    from backend.core.sandbox_read import ReadRoot
    from backend.core.storage import get_storage, close_owned_storage
    from backend.core.storage.native_invocations import NAMESPACE, pending_native_invocations
    facts=[]
    promotion_review=None
    try:
        with broker_session(config) as binding:
            project=Path(binding['project']);store=get_storage(project)
            oid,first,second=oids
            with ReadRoot(project/'.git/objects') as root:
                for query,expected in [(['cat-file','-t',oid],'blob\n'),
                    (['cat-file','-s',oid],'17\n'),(['merge-base',first,second],first+'\n')]:
                    result=read_git_objects(project,root,query,timeout=20)
                    facts.append(dict(query=query,matched=result==expected))
            if consumers:
                from backend.adapters.local_git_runner import ProtectedLocalGitRunner,LocalGitRunner
                from backend.core.loop.worktree import AgentWorktreeManager
                from backend.core.loop.git_execution import GitBoundaryError
                runner=ProtectedLocalGitRunner(project)
                for query,expected,code in [(['cat-file','-t',oid],'blob\n',0),
                        (['cat-file','-s',oid],'17\n',0),(['merge-base',first,second],first+'\n',0),
                        (['merge-base','--is-ancestor',first,second],'',0),
                        (['merge-base','--is-ancestor',second,first],'',1)]:
                    result=runner.run(query,timeout=20)
                    facts.append(dict(query=query,consumer='ProtectedLocalGitRunner',matched=result.stdout==expected and result.returncode==code))
                result=AgentWorktreeManager._run(project,['git','cat-file','-s',oid])
                facts.append(dict(query=['cat-file','-s',oid],consumer='AgentWorktreeManager',matched=result.returncode==0 and result.stdout==b'17\n'))
                result=LocalGitRunner(project).run(['cat-file','-t',oid],text=False)
                facts.append(dict(query=['cat-file','-t',oid],consumer='LocalGitRunner',matched=result.returncode==0 and result.stdout==b'blob\n'))
                try:runner.run(['update-ref','refs/heads/should-not-exist',first])
                except GitBoundaryError:pass
                else:raise RuntimeError('Consumer unexpectedly permitted Git writes')
            if objects:
                import hashlib
                from backend.adapters.local_git_runner import ProtectedLocalGitRunner
                runner=ProtectedLocalGitRunner(project)
                data=b'gitgo protected object\0\xff\r\n'
                expected=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
                result=runner.run(['hash-object','-w','--stdin'],text=False,input_bytes=data,timeout=20)
                facts.append(dict(query=['hash-object','-w','--stdin'],matched=result.returncode==0 and result.stdout==expected.encode()+b'\n'))
                tree=runner.run(['mktree','-z'],text=False,input_bytes=b'100644 blob '+expected.encode()+b'\tdata.bin\0',timeout=20)
                tree_oid=tree.stdout.decode().strip()
                tree_bytes=b'100644 data.bin\0'+bytes.fromhex(expected)
                expected_tree=hashlib.sha1(b'tree '+str(len(tree_bytes)).encode()+b'\0'+tree_bytes).hexdigest()
                facts.append(dict(query=['mktree','-z'],matched=tree.returncode==0 and tree_oid==expected_tree))
                commit,expected_commit=object_fixture_commit(runner,tree_oid,first)
                facts.append(dict(query=['commit-tree',tree_oid],matched=commit.returncode==0 and commit.stdout==expected_commit+'\n'))
                with ReadRoot(project/'.git/objects') as object_root:
                    from backend.core.macos_git_write import loose_oid
                    for value in (expected,tree_oid,commit.stdout.strip()):
                        name=value[:2]+'/'+value[2:]
                        with object_root.open(name) as opened:assert loose_oid(name,opened.read_bytes(max_bytes=4194304))==value
            if checkout:
                from backend.core.macos_broker_client import checkout_git_commit
                assert objects,'Nonempty checkout fixture requires object producers'
                for selected,expected_files in [(first,()),(expected_commit,(('data.bin',data,0o644),))]:
                    with ReadRoot(project/'.git/objects') as root:
                        image=checkout_git_commit(project,root,selected,timeout=30)
                    facts.append(dict(query=['checkout-commit',selected],matched=image.commit==selected and image.files==expected_files,
                        checkout_index_sha256=hashlib.sha256(image.index).hexdigest(),file_count=len(image.files)))
                assert not (project/'data.bin').exists() and not (project/'.git/worktrees').exists()
            if compare:
                compare_queries=[
                    (['ls-tree','-rz',expected_commit,'--'],b'100644 blob '+expected.encode()+b'\tdata.bin\0',1),
                    (['ls-tree','-rz',expected_commit,'--','missing','data.bin'],b'100644 blob '+expected.encode()+b'\tdata.bin\0',1),
                    (['diff','--no-renames','--name-only','-z',first,expected_commit,'--'],b'data.bin\0',2),
                    (['diff','--no-renames','--name-only','-z',expected_commit,expected_commit,'--'],b'',1),
                ]
                for query,wanted,image_calls in compare_queries:
                    value=runner.run(query,text=False)
                    assert value.returncode==0 and value.stdout==wanted
                    facts.extend(dict(query=['commit-comparison',*query],image=i,matched=True) for i in range(image_calls))
            lifecycle_facts=[]
            if worktrees:
                import threading
                from types import SimpleNamespace
                from backend.core.loop.worktree import AgentWorktreeManager,WorktreeLease,WorktreeError
                from backend.core.loop.native_invocation_scope import native_invocation_scope
                from backend.core.loop.git_execution import GitBoundaryError
                manager=AgentWorktreeManager(project,store)
                lease=manager.create(process_id='normal-worktree',task_id='worktree-acceptance',snapshot_commit=expected_commit)
                facts.append(dict(query=['worktree-create',expected_commit],matched=(Path(lease.path)/'data.bin').read_bytes()==data))
                assert manager.verify(WorktreeLease.from_dict(store.load_worktree_state(lease.process_id)))['valid']
                ctx=SimpleNamespace(workspace_path=Path(lease.path),storage=store,cancellation=threading.Event(),
                    process=SimpleNamespace(process_id=lease.process_id),session=SimpleNamespace(session_id='worktree-acceptance'))
                with native_invocation_scope(ctx,'worktree-execution',7):
                    linked=ProtectedLocalGitRunner(Path(lease.path))
                    assert linked.run(['rev-parse','HEAD']).stdout==expected_commit+'\n'
                    assert linked.run(['rev-parse','--path-format=absolute','--git-common-dir']).stdout==str(project/'.git')+'\n'
                    result=linked.run(['cat-file','-t',expected_commit])
                    facts.append(dict(query=['worktree-object-query',expected_commit],matched=result.stdout=='commit\n'))
                    if compare:
                        value=linked.run(['ls-tree','-rz',expected_commit,'--','data.bin'],text=False)
                        assert value.stdout==b'100644 blob '+expected.encode()+b'\tdata.bin\0'
                        facts.append(dict(query=['linked-commit-tree',expected_commit],matched=True))
                    ctx.process.process_id='foreign-task'
                    try:linked.run(['cat-file','-t',expected_commit])
                    except GitBoundaryError:pass
                    else:raise AssertionError('Cross-task worktree granted')
                fake=manager.worktrees_root/'unregistered';fake.mkdir()
                (fake/'.git').write_bytes((Path(lease.path)/'.git').read_bytes())
                try:ProtectedLocalGitRunner(fake).run(['rev-parse','HEAD'])
                except GitBoundaryError:pass
                else:raise AssertionError('Unregistered pointer granted')
                (fake/'.git').unlink();fake.rmdir()
                outside=manager.worktrees_root/'canary';outside.mkdir();(outside/'keep').write_bytes(b'keep')
                (Path(lease.path)/'escape').symlink_to(outside,target_is_directory=True)
                manager.dispose(lease);before_row=store.load_worktree_state(lease.process_id)
                manager.recover_owned(lease)
                assert store.load_worktree_state(lease.process_id)==before_row and (outside/'keep').read_bytes()==b'keep'
                assert not Path(lease.path).exists()
                lifecycle_facts.append(dict(case='normal',registered=True,permission_bound=True,published=True,
                    cross_task_denied=True,spoof_denied=True,disposed=True,idempotent=True,canary_unchanged=True))
                for mode in worktree_fault_modes(results):
                    before_history=len(store.list_state_refs(NAMESPACE,prefix='history/',limit=100))
                    result=subprocess.run(packaged.command(Path(__file__).with_name('check_macos_worktree_client.py'),
                        '--config',config,'--commit',first if mode=='finalize-upstream' else expected_commit,'--mode',mode,'--uid',uid,*(['--upstream',expected_commit] if mode=='finalize-upstream' else [])),
                        capture_output=True,text=True,timeout=60)
                    assert result.returncode==86,(mode,result.returncode,result.stderr)
                    lease=WorktreeLease.from_dict(store.load_worktree_state('fault-'+mode))
                    assert len(store.list_state_refs(NAMESPACE,prefix='history/',limit=100))==before_history+1
                    if mode=='unrecorded':
                        path=manager.worktrees_root/lease.macos_binding['stage']
                        assert path.is_dir() and 'work_identity' not in lease.macos_binding
                        try:manager.recover_owned(lease)
                        except WorktreeError:pass
                        else:raise AssertionError('Unknown directory adopted')
                        assert path.is_dir()
                        lifecycle_facts.append(dict(case=mode,exit_code=86,unrecorded_retained=True,automatic_resume=False,path=str(path)))
                    else:
                        manager.recover_owned(lease)
                        before_row=store.load_worktree_state(lease.process_id);manager.recover_owned(lease)
                        assert store.load_worktree_state(lease.process_id)==before_row
                        assert lease.state==('disposed' if mode=='retiring' else 'failed') and lease.macos_binding['phase']=='removed'
                        assert not Path(lease.path).exists()
                        for path in (manager.worktrees_root/lease.macos_binding['stage'],manager.worktrees_root/lease.macos_binding['trash'],
                                project/'.git/worktrees'/lease.macos_binding['metadata'],project/'.git/worktrees'/lease.macos_binding['meta_trash']):
                            assert not path.exists()
                        lifecycle_facts.append(dict(case=mode,exit_code=86,state=lease.state,cleanup='removed',idempotent=True,automatic_resume=False))
                    assert len(store.list_state_refs(NAMESPACE,prefix='history/',limit=100))==before_history+1
                    facts.append(dict(query=['worktree-crash',mode],matched=True))
                assert list((project/'.git/worktrees').iterdir())==[] and not (project/'data.bin').exists()
            index_facts=[]
            if index:
                from backend.core import macos_broker_client as client_api,macos_worktree_index as index_api
                manager=AgentWorktreeManager(project,store)
                lease=manager.create(process_id='index-normal',task_id='index-acceptance',snapshot_commit=expected_commit)
                facts.append(dict(query=['index-worktree-create',expected_commit],matched=True))
                path=Path(lease.path);meta=project/'.git/worktrees'/lease.macos_binding['metadata']
                (path/'data.bin').write_bytes(b'changed\0\xff\r\n');(path/'new').mkdir()
                (path/'new/run').write_bytes(b'#!/bin/sh\necho ok\n');(path/'new/run').chmod(0o755)
                original=client_api.produce_git_objects
                def observed(*a,**kw):
                    result=original(*a,**kw)
                    facts.append(dict(query=['index-producer',*a[2]],matched=result.returncode==0))
                    return result
                client_api.produce_git_objects=observed
                try:
                    first_index=manager.stage_index(lease,['data.bin','new/run'])
                    entries=index_api.decode((meta/'index').read_bytes())
                    assert entries==[('data.bin',index_api.object_oid('blob',b'changed\0\xff\r\n'),0o644),
                                     ('new/run',index_api.object_oid('blob',b'#!/bin/sh\necho ok\n'),0o755)]
                    assert first_index['tree']==index_api.tree_plan(entries)[0] and manager.verify(lease)['valid']
                    (path/'data.bin').unlink();second_index=manager.stage_index(lease,['data.bin'])
                    assert index_api.decode((meta/'index').read_bytes())==entries[1:] and manager.verify(lease)['valid']
                    assert (meta/'HEAD').read_text()==expected_commit+'\n'
                finally:client_api.produce_git_objects=original
                manager.dispose(lease)
                index_facts.append(dict(case='normal',added_modified_deleted=True,executable_mode=True,head_unchanged=True,
                    first_tree=first_index['tree'],second_tree=second_index['tree'],disposed=True))
                for mode in ('index-producing','index-prepared','index-replaced','index-committed'):
                    before_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}
                    result=subprocess.run(packaged.command(Path(__file__).with_name('check_macos_worktree_client.py'),
                        '--config',config,'--commit',first if mode=='finalize-upstream' else expected_commit,'--mode',mode,'--uid',uid,*(['--upstream',expected_commit] if mode=='finalize-upstream' else [])),capture_output=True,text=True,timeout=90)
                    assert result.returncode==86,(mode,result.returncode,result.stderr)
                    lease=WorktreeLease.from_dict(store.load_worktree_state('fault-'+mode))
                    manager.recover_owned(lease)
                    if mode=='index-committed':
                        assert lease.state=='leased' and manager.verify(lease)['valid']
                        assert lease.macos_binding['staged_tree'] and not lease.macos_binding.get('index_update')
                        manager.dispose(lease)
                    else:assert lease.state=='failed' and lease.macos_binding['phase']=='removed'
                    row=store.load_worktree_state(lease.process_id);manager.recover_owned(lease)
                    assert row==store.load_worktree_state(lease.process_id) and not Path(lease.path).exists()
                    new_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}-before_ids
                    assert len(new_ids)==(1 if mode=='index-producing' else 3)
                    facts.extend(dict(query=['index-crash',mode,key],matched=True) for key in sorted(new_ids))
                    index_facts.append(dict(case=mode,exit_code=86,state=lease.state,removed=True,idempotent=True,automatic_resume=False,producer_calls=len(new_ids)-1))
                assert list((project/'.git/worktrees').iterdir())==[]
            result_facts=[];upstream_cases=[];resume_cases=[];promotion_cases=[]
            if results:
                from backend.core import macos_broker_client as client_api
                manager=AgentWorktreeManager(project,store)
                before_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}
                lease=manager.create(process_id='result-normal',task_id='result-acceptance',snapshot_commit=expected_commit)
                from check_macos_worktree_client import ephemeral_privacy_fixture
                path=Path(lease.path);(path/'data.bin').write_bytes(ephemeral_privacy_fixture())
                try:manager.stage_index(lease,['data.bin'])
                except WorktreeError as exc:assert 'privacy scan' in str(exc)
                else:raise RuntimeError('Secret entered a staged object')
                assert len(store.list_state_refs(NAMESPACE,prefix='history/',limit=100))==len(before_ids)+1
                (path/'data.bin').write_bytes(b'published result fixture')
                manager.stage_index(lease,['data.bin']);published=manager.publish_index(lease)
                assert manager.verify(lease)['valid'] and lease.state=='leased' and not lease.result_commit
                assert (project/'.git'/published['ref']).read_text()==published['commit']+'\n'
                assert lease.macos_binding['commit']==expected_commit
                try:manager.stage_index(lease,['data.bin'])
                except WorktreeError:pass
                else:raise RuntimeError('Published result was restaged')
                digest=hashlib.sha256(b'published result fixture').hexdigest()
                artifact=manager.read_artifact(published['commit'],'data.bin',sha256=digest,offset=10)
                assert artifact['content']=='result fixture' and artifact['sha256']==digest
                assert artifact['working_tree_bytes_verified'] is False
                # Host-owned fixture represents Git packing only this exact ref.
                # Real git pack-refs is independently covered in ordinary-user tests.
                packed=project/'.git/packed-refs'
                assert not packed.exists()
                packed.write_text('# pack-refs with: sorted\n'+published['commit']+' '+published['ref']+'\n')
                (project/'.git'/published['ref']).unlink()
                assert manager.verify(lease)['valid']
                manager.recover_owned(lease);assert manager.verify(lease)['valid']
                if seal:
                    (path/'not-staged.txt').write_text('keep this edit')
                    try:manager.seal(lease)
                    except WorktreeError:pass
                    else:raise RuntimeError('Unstaged file silently discarded by seal')
                    assert lease.state=='leased' and not lease.result_commit
                    (path/'not-staged.txt').unlink()
                    (path/'.gitgo').mkdir();(path/'.gitgo/dependency_feedback.json').write_text('{}')
                    try:manager.seal(lease)
                    except WorktreeError:pass
                    else:raise RuntimeError('Runtime output silently discarded by seal')
                    assert (path/'.gitgo/dependency_feedback.json').read_text()=='{}'
                    (path/'.gitgo/dependency_feedback.json').unlink();(path/'.gitgo').rmdir()
                    if runtime_merge:
                        from check_macos_worktree_client import seed_runtime_outputs
                        seed_runtime_outputs(path,'runtime-normal')
                        if finalize:
                            from backend.core.dependency_graph import DependencyGraph,_save_graph
                            _save_graph(DependencyGraph(path))
                        else:manager.merge_runtime_outputs(lease);manager.merge_runtime_outputs(lease)
                    manager.seal(lease)
                    assert lease.state=='sealed' and lease.has_promotable_result
                    assert lease.result_commit==lease.own_commit==published['commit'] and lease.changed_files==['data.bin']
                    assert manager.verify(lease)['valid']
                    row=store.load_worktree_state(lease.process_id);manager.recover_owned(lease)
                    assert row==store.load_worktree_state(lease.process_id)
                review_ids=set()
                if finalize=='promotion_review':
                    from check_macos_promotion_client import exercise_product_review
                    promotion_review=exercise_product_review(manager,lease)
                    lease=WorktreeLease.from_dict(store.load_worktree_state(lease.process_id))
                    assert lease.promoted
                    review_ids={'history/'+key for key in
                                promotion_review['published']+promotion_review['repeat_verified']}
                    facts.extend(dict(query=['product-promotion-review',key],matched=True)
                                 for key in sorted(review_ids))
                manager.dispose(lease)
                artifact_after=manager.read_artifact(published['commit'],'data.bin',sha256=digest)
                assert artifact_after['content']=='published result fixture'
                assert artifact_after['sealed_commit']==published['commit'] and not path.exists()
                new_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}-before_ids
                assert review_ids<=new_ids
                new_ids-=review_ids
                assert len(new_ids)==(9 if seal else 7)
                facts.extend(dict(query=['result-normal',key],matched=True) for key in sorted(new_ids))
                result_facts.append(dict(case='normal',privacy_blocked_before_production=True,head_unchanged=True,
                    published=published,disposed=True,implicit_seal=False,explicit_seal=seal,artifact_before=artifact,artifact_after=artifact_after,packed_ref_fixture=True))
                if finalize=='upstream':
                    before_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}
                    upstream_cases=upstream_rejections(manager,first,expected_commit,second,oid)
                    new_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}-before_ids
                    assert len(new_ids)==5
                    facts.extend(dict(query=['upstream-rejection',key],matched=True) for key in sorted(new_ids))
                if finalize=='promotion_read':
                    before_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}
                    # The earlier comparison and write batches have their own
                    # accepted reports. One trial grant permits 64 connections.
                    promotion_cases=promotion_flow_cases(manager,lease,config=config,uid=uid)
                    new_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}-before_ids
                    assert len(new_ids)==31
                    facts.extend(dict(query=['promotion-precondition',key],matched=True) for key in sorted(new_ids))
                for mode in (() if finalize in ('promotion_read','promotion_review') else ('resume-prepared','resume-sealed') if finalize=='resume' else ('finalize-upstream',) if finalize=='upstream' else ('finalize-many','finalize-empty') if finalize=='boundaries' else ('finalize-intent','finalize-first','finalize-last') if finalize=='pending' else ('finalize-merged','finalize-sealed') if finalize=='ack' else ('runtime-intent','runtime-first','runtime-last','runtime-ack') if runtime_merge else ('seal-prepared','seal-committed') if seal else ('result-producing','result-verified','result-packed-locked','result-prepared','result-renamed','result-committed','result-unacknowledged')):
                    before_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}
                    child_result=subprocess.run(packaged.command(Path(__file__).with_name('check_macos_worktree_client.py'),
                        '--config',config,'--commit',first if mode=='finalize-upstream' else expected_commit,'--mode',mode,'--uid',uid,*(['--upstream',expected_commit] if mode=='finalize-upstream' else [])),capture_output=True,text=True,timeout=90)
                    assert child_result.returncode==86,(mode,child_result.returncode,child_result.stderr)
                    lease=WorktreeLease.from_dict(store.load_worktree_state('fault-'+mode))
                    if finalize=='resume':
                        rejection_evidence=json.loads(child_result.stdout)['resume_rejections']
                        assert {r['case'] for r in rejection_evidence}=={'forged','review','same_process','private_key'}
                        assert all(r['rejected'] and r['no_native_execution'] and r['no_worktree_side_effect'] for r in rejection_evidence)
                        source=WorktreeLease.from_dict(store.load_worktree_state('source-'+mode))
                        assert source.state=='leased' and not source.has_promotable_result and source.continuation_commit
                        checkpoint=source.macos_binding['result']
                        assert checkpoint['commit']==source.continuation_commit==lease.continuation_commit
                        assert lease.resumed_from_process_id==source.process_id
                        assert source.macos_binding['work_identity']!=lease.macos_binding['work_identity']
                        before_recovery={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}
                        manager.recover_owned(lease);manager.recover_owned(lease)
                        assert before_recovery=={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}
                        published_result=lease.macos_binding.get('result') or {}
                        if mode=='resume-prepared':
                            assert lease.state=='failed' and not Path(lease.path).exists()
                        else:
                            assert lease.state=='sealed' and lease.changed_files==['data.bin','new.bin'] and lease.own_commit
                            assert lease.macos_binding['commit']==expected_commit and manager.verify(lease)['valid']
                            assert (Path(lease.path)/'data.bin').read_bytes()==b'retained partial\0\xff'
                            assert (Path(lease.path)/'new.bin').read_bytes()==b'new partial\0\xfe'
                            manager.dispose(lease)
                        assert manager.verify(source)['valid'];manager.dispose(source)
                        new_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}-before_ids
                        assert len(new_ids)==(8 if mode=='resume-prepared' else 14),(mode,len(new_ids))
                        facts.extend(dict(query=['resume-crash',mode,key],matched=True) for key in sorted(new_ids))
                        resume_cases.append(dict(case=mode,rejections=rejection_evidence,exit_code=86,state=lease.state,checkpoint=checkpoint,
                            result=published_result,source_disposed=True,no_execution_authority_reused=True,
                            recovery_writes=False,automatic_resume=False))
                        continue
                    if runtime_merge:
                        from backend.core.macos_worktree_runtime import NAMES
                        root=project/'.gitgo'
                        before_runtime={n:(root/n).read_bytes() if (root/n).exists() else None for n in NAMES}
                        manager.recover_owned(lease);manager.recover_owned(lease)
                        assert all(((root/n).read_bytes() if (root/n).exists() else None)==v for n,v in before_runtime.items())
                        pending=mode in ('runtime-intent','runtime-first','finalize-intent','finalize-first')
                        acknowledged=lease.state=='sealed'
                        if finalize:assert acknowledged==(mode in ('finalize-sealed','finalize-many','finalize-empty','finalize-upstream'))
                        assert bool(lease.macos_binding.get('runtime_update'))==pending
                        if pending:
                            assert not manager.verify(lease)['valid']
                            try:manager.dispose(lease)
                            except WorktreeError:pass
                            else:raise RuntimeError('Pending runtime evidence was discarded')
                        if finalize:
                            if pending:manager.merge_runtime_outputs(lease)
                            if lease.state!='sealed':manager.seal(lease)
                        else:manager.merge_runtime_outputs(lease)
                        assert not lease.macos_binding.get('runtime_update') and manager.verify(lease)['valid']
                        lines=[json.loads(line) for line in (root/NAMES[1]).read_bytes().splitlines()]
                        assert sum(row['task_id']==mode for row in lines)==1
                        retained=lease.macos_binding['result']['ref'] if finalize else ''
                        if not finalize:manager.merge_runtime_outputs(lease)
                        manager.dispose(lease)
                        new_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}-before_ids
                        assert len(new_ids)==(9 if mode=='finalize-upstream' else 26 if mode=='finalize-many' else 6 if mode=='finalize-empty' else 7 if mode=='finalize-sealed' else 9 if finalize else 1)
                        facts.extend(dict(query=['runtime-crash',mode,key],matched=True) for key in sorted(new_ids))
                        result_facts.append(dict(case=mode,exit_code=86,state=lease.state,retained_ref=retained,commit=lease.result_commit,acknowledged_after_recovery=acknowledged,recovery_writes=False,
                            pending_after_recovery=pending,explicit_retry=True,append_count=1,automatic_resume=False))
                        continue
                    if mode=='result-unacknowledged':
                        intent=lease.macos_binding['result_update'];ref='refs/gitgo/results/'+intent['id']
                        rows=[line for line in packed.read_text().splitlines() if line and not line.startswith('#')]
                        rows.append(intent['commit']+' '+ref)
                        packed.write_text('# pack-refs with: sorted\n'+'\n'.join(sorted(rows,key=lambda line:line.split(' ',1)[1]))+'\n')
                        (project/'.git'/ref).unlink()
                    manager.recover_owned(lease)
                    acknowledged=lease.state=='sealed'
                    if seal:
                        assert lease.state==('sealed' if mode=='seal-committed' else 'leased')
                        assert bool(lease.result_commit)==(mode=='seal-committed')
                        assert manager.verify(lease)['valid']
                        retained=lease.macos_binding['result']['ref'];manager.dispose(lease)
                    elif mode=='result-committed':
                        assert manager.verify(lease)['valid'] and lease.macos_binding['result']
                        retained=lease.macos_binding['result']['ref'];manager.dispose(lease)
                    else:
                        assert lease.state=='failed' and not Path(lease.path).exists()
                        retained=lease.macos_binding['result_interrupted']['ref']
                        assert bool(retained)==(mode in ('result-renamed','result-unacknowledged'))
                    row=store.load_worktree_state(lease.process_id);manager.recover_owned(lease)
                    assert row==store.load_worktree_state(lease.process_id)
                    assert not (project/'.git/packed-refs.lock').exists()
                    new_ids={r['key'] for r in store.list_state_refs(NAMESPACE,prefix='history/',limit=100)}-before_ids
                    assert len(new_ids)==(7 if seal else 3 if mode=='result-producing' else 5)
                    facts.extend(dict(query=['result-crash',mode,key],matched=True) for key in sorted(new_ids))
                    result_facts.append(dict(case=mode,exit_code=86,state=lease.state,acknowledged=acknowledged,retained_ref=retained,
                        commit=(lease.macos_binding.get('result') or lease.macos_binding.get('result_interrupted'))['commit'],
                        idempotent=True,automatic_resume=False))
            rows=store.list_state_refs(NAMESPACE,prefix='history/',limit=100)
            records=[json.loads(store.read_blob(r['value_ref'],repair=False)) for r in rows]
            assert len(records)==len(facts) and not pending_native_invocations(store,include_active=True)
            assert all(r['tool_name'] in ('macos_git_read','macos_git_objects','macos_git_checkout') and r['launch_state']=='started' and
                r['runtime_cleanup']['complete'] is True and not r['runtime_cleanup']['issues'] and
                r['macos_broker_binding']['grant_id']==binding['grant_id'] for r in records)
            if worktrees:
                scoped=[r for r in records if r['execution_id']=='worktree-execution']
                assert len(scoped)==(2 if compare else 1) and all(r['call_index']==7 and r['session_id']=='worktree-acceptance' for r in scoped)
                assert scoped[0]['process_id']=='normal-worktree' and scoped[0]['task_id']=='normal-worktree'
            # Replay uses a valid fixed read request, ensuring its rejection
            # comes from the original invocation ledger, not a malformed command.
            from backend.core import macos_git_read as git
            with ReadRoot(project/'.git/objects') as root: view=git.capture_objects(root,project/'.git/objects')
            rejected={}
            for case in ('replay','wrong_project','unsupported_command'):
                fd=wire.open_directory(project.parent if case=='wrong_project' else project)
                connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);connection.settimeout(5)
                try:
                    connection.connect(binding['socket']);assert wire.peer_uid(connection)==0
                    wire.send_directory(connection,fd)
                    wire.send(connection,dict(op='git_read',token=binding['token'],grant_id=binding['grant_id'],
                        invocation_id=records[0]['invocation_id'] if case=='replay' else uuid.uuid4().hex,
                        execution_basis=records[0]['execution_basis'],timeout=5,
                        query=['status'] if case=='unsupported_command' else ['cat-file','-t',oid],
                        objects=git.view_to_wire(view),view_sha256=view.sha256))
                    response=wire.receive(connection);rejected[case]=response.get('event')=='denied'
                finally: connection.close();os.close(fd)
            print(json.dumps(dict(passed=all(f['matched'] for f in facts) and all(rejected.values()),
                uid=os.getuid(),runtime=packaged.evidence(),consumers=consumers,objects=objects,checkout=checkout,worktrees=worktrees,index=index,results=results,compare=compare,seal=seal,runtime_merge=runtime_merge,finalize=finalize,runtime_outputs=({str(p.relative_to(project)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (project/'.gitgo').rglob('*') if p.is_file()} if runtime_merge else {}),promotion_cases=promotion_cases,promotion_review=promotion_review,resume_cases=resume_cases,upstream_cases=upstream_cases,result_cases=result_facts,index_cases=index_facts,worktree_cases=lifecycle_facts,queries=facts,rejected=rejected,records=records)),flush=True)
    finally: close_owned_storage()


def acceptance(owner,consumers=False,objects=False,checkout=False,worktrees=False,index=False,results=False,compare=False,seal=False,runtime_merge=False,finalize=''):
    calls=expected_calls(consumers,objects,checkout,worktrees,index,results,compare,seal,runtime_merge,finalize)
    # Budget for the startup probe and the three rejection probes as well.
    if calls+4>64:
        raise ValueError('Split this acceptance batch to fit the trial broker connection budget')
    from check_macos_git_read import fixture
    base=Path(tempfile.mkdtemp(prefix='gitgo-git-broker-check-',dir='/private/tmp'));base.chmod(0o711)
    home=base/'user';home.mkdir(mode=0o700);os.chown(home,owner.pw_uid,owner.pw_gid)
    _,oid,first,second,marker=fixture(home)
    project=home/'source';(project/'.git').rmdir();(project/'old-git').rename(project/'.git')
    if worktrees:(project/'.git/HEAD').write_text(first+'\n')
    # Project registration is setup, not a side effect of a read request.
    metadata=project/'.git/gitgo';metadata.mkdir()
    (metadata/'project-id').write_text(str(uuid.uuid4())+'\n')
    for directory,dirs,files in os.walk(project):
        os.chown(directory,owner.pw_uid,owner.pw_gid)
        for name in files: os.chown(Path(directory)/name,owner.pw_uid,owner.pw_gid)
    # User-owned input's entire contents and modes must survive the read path.
    import hashlib
    def snapshot():
        return {str(p.relative_to(project)):(p.stat().st_uid,p.stat().st_mode,
                    hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                for p in [project,*project.rglob('*')]}
    before=snapshot()
    endpoint=broker.provision(project,owner.pw_uid,lifetime=480)
    report=dict(passed=False,artifacts=str(base),broker=str(endpoint),leases=[])
    label=None;child=None
    try:
        label=launch_broker(endpoint);report['startup_seconds']=wait_broker(endpoint,label)
        env=dict(PATH='/usr/bin:/bin',HOME=str(home),TMPDIR=str(home),PYTHONDONTWRITEBYTECODE='1',
            GITGO_STATE_HOME=str(home/'state'),GITGO_CONFIG_PATH=str(home/'config.json'),GITGO_LLM_CONFIG_PATH=str(home/'llm.json'))
        child=subprocess.Popen(packaged.command(Path(__file__), '--client',endpoint/'client.json',
            '--uid',owner.pw_uid,'--oids',json.dumps([oid,first,second]),*(['--consumers'] if consumers else []),*(['--objects'] if objects else []),*(['--checkout'] if checkout else []),*(['--worktrees'] if worktrees else []),*(['--index'] if index else []),*(['--results'] if results else []),*(['--compare'] if compare else []),*(['--seal'] if seal else []),*(['--runtime-merge'] if runtime_merge else []),*(['--finalize',finalize] if finalize else [])),
            user=owner.pw_uid,group=owner.pw_gid,extra_groups=[],cwd=home,env=env,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        out,err=child.communicate(timeout=240)
        report['client_stderr']=err[-6000:];report['client']=json.loads(out)
        if 'records' not in report['client']:raise RuntimeError('Client did not return acceptance evidence; inspect client_stderr and client protocol response')
        seen=json.loads(identity.bounded_read(endpoint/'private/seen.json',0,True))
        records=report['client'].get('records',[])
        report['original_journal_matches_broker']=set(seen)=={r['invocation_id'] for r in records} and len(seen)==expected_calls(consumers,objects,checkout,worktrees,index,results,compare,seal,runtime_merge,finalize)
        if packaged.HELPER:
            runtime=report['client'].get('runtime',{})
            job=__import__('plistlib').loads(identity.bounded_read(endpoint/'private/job.plist',0,True))
            report['frozen_broker_entry']=job['ProgramArguments']==[str(packaged.HELPER),'--gitgo-internal-role','macos-broker-service',str(endpoint)]
            assert runtime.get('embedded_backend') is True and runtime.get('executable')==str(packaged.HELPER) and report['frozen_broker_entry']
        after=snapshot()
        report['source_unchanged']=after==before
        if objects:
            from backend.core.macos_git_write import loose_oid
            added=set(after)-set(before)
            assert all(after.get(n)==v for n,v in before.items()),'Existing source changed'
            permitted_refs={}
            if results:
                cases=report['client']['result_cases']
                published=cases[0]['published'];permitted_refs['.git/'+published['ref']]=published['commit']
                permitted_refs.update({'.git/'+c['retained_ref']:c['commit'] for c in cases[1:] if c['retained_ref']})
                if finalize=='resume':
                    for item in report['client']['resume_cases']:
                        for result in (item['checkpoint'],item['result']):
                            if result:permitted_refs['.git/'+result['ref']]=result['commit']
                assert len(permitted_refs)==(1 if finalize in ('promotion_read','promotion_review') else 4 if finalize=='resume' else 2 if finalize=='upstream' else 4 if finalize=='pending' else 3 if finalize in ('ack','boundaries') else 1 if runtime_merge else 3 if seal else 4)
                report['result_refs_verified']=permitted_refs
            for name in added:
                path=project/name
                if worktrees and name=='.git/worktrees':
                    assert path.is_dir() and list(path.iterdir())==[]
                    continue
                if results and name in ('.git/refs/gitgo','.git/refs/gitgo/results'):
                    assert path.is_dir();continue
                if results and name=='.git/packed-refs':
                    expected=[published['commit']+' '+published['ref']]
                    if not seal:
                        interrupted=next(c for c in cases if c['case']=='result-unacknowledged')
                        expected.append(interrupted['commit']+' '+interrupted['retained_ref'])
                    assert path.read_text()=='# pack-refs with: sorted\n'+'\n'.join(sorted(expected,key=lambda line:line.split(' ',1)[1]))+'\n'
                    assert not (project/'.git'/published['ref']).exists()
                    report['packed_result_ref_verified']=True
                    continue
                if name in permitted_refs:
                    assert path.is_file() and path.read_text()==permitted_refs[name]+'\n';continue
                if runtime_merge and (name=='.gitgo' or name.startswith('.gitgo/')):
                    from backend.core.macos_worktree_runtime import NAMES
                    allowed={'.gitgo/'+n for n in NAMES}
                    locks={'.gitgo/metadata-locks/'+hashlib.sha256(n.encode()).hexdigest()+'/storage-maintenance.lock' for n in NAMES}
                    allowed|=locks;directories={str(p) for n in allowed for p in Path(n).parents if str(p)!='.'}
                    if path.is_file():
                        assert name in allowed and after[name][2]==report['client']['runtime_outputs'][name]
                        if name in locks:assert path.read_bytes()==b''
                    else:assert name in directories
                    continue
                assert name.startswith('.git/objects/'),'Non-object write escaped'
                if path.is_file():loose_oid(name.removeprefix('.git/objects/'),path.read_bytes())
                else:assert len(path.name)==2
            report['only_verified_objects_added']=bool(added) and not results
            report['only_verified_objects_and_result_refs_added']=bool(added) and results and not runtime_merge
            report['only_verified_objects_refs_and_runtime_added']=bool(added) and runtime_merge
            report['source_unchanged']=False
        report['extensions_not_executed']=not marker.exists()
        report['passed']=bool(child.returncode==0 and report['client']['passed'] and
            report['original_journal_matches_broker'] and (report.get('only_verified_objects_refs_and_runtime_added') if runtime_merge else report.get('only_verified_objects_and_result_refs_added') if results else report.get('only_verified_objects_added') if objects else report['source_unchanged']) and report['extensions_not_executed'])
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
    finally:
        if child is not None:
            if child.poll() is None: child.kill()
            out,err=child.communicate(timeout=5)
            if err: report['client_stderr']=err[-6000:]
            if 'client' not in report and out: report['client_raw']=out[-10000:]
        if label: launchctl('bootout','system/'+label)
        # Only observed-clean owned leases may have their job unloaded. Unknown
        # evidence stays intact and blocks further admission via the allocator.
        for path in sorted((endpoint/'private').glob('invocation-*.json')):
            item={}
            try:
                mapping=json.loads(identity.bounded_read(path,0,True))
                from backend.core.sandbox_macos import lease_root
                lease=lease_root(mapping)
                state=wait_for(lambda:(s if (s:=load_state(lease))['phase']=='clean' else None),20,'Git UID cleanup pending')
                item=dict(lease=str(lease),invocation_id=state['invocation_id'],uid=state['uid'],
                    phase=state['phase'],launch_count=state['launch_count'],cleanup=state.get('cleanup'),
                    remaining=identity.owned_rows(state['uid']))
                if packaged.HELPER:
                    item['frozen_supervisor']=packaged.task_evidence(lease,state,packaged.HELPER)
                    assert item['frozen_supervisor']
                    if state['argv'][0]!='/usr/bin/git':
                        from backend.core.macos_git_checkout import PRODUCER
                        assert checkout and state['argv'][:5]==[str(packaged.HELPER),'--gitgo-internal-role','python','-I','-c']
                        assert state['argv'][5]==PRODUCER and len(state['argv'])==9
                assert not item['remaining'] and state['cleanup']['observed_empty'] is True
                job='system/org.gitgo.execution-trial.'+lease.name
                launchctl('bootout',job)
                item['unloaded']=wait_unloaded(job)
                if not item['unloaded']: raise RuntimeError('Execution job absence unconfirmed')
            except Exception as exc:
                item['error']=type(exc).__name__+': '+str(exc);report['passed']=False
            report['leases'].append(item)
        if label:
            try:report['broker_unloaded']=wait_unloaded('system/'+label)
            except Exception as exc:
                report['broker_unloaded']=False;report['passed']=False
                report['broker_unload_error']=type(exc).__name__+': '+str(exc)
        for name in ('service.err','last-error.json'):
            path=endpoint/'private'/name
            if path.exists(): report[name]=identity.bounded_read(path,0).decode(errors='replace')[-6000:]
        report['passed']=bool(report['passed'] and report.get('broker_unloaded') and len(report['leases'])==expected_calls(consumers,objects,checkout,worktrees,index,results,compare,seal,runtime_merge,finalize) and
            all(item.get('launch_count')==1 and item.get('unloaded') for item in report['leases']))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--staged',action='store_true')
    parser.add_argument('--client');parser.add_argument('--uid',type=int,default=0);parser.add_argument('--oids')
    parser.add_argument('--helper')
    parser.add_argument('--consumers',action='store_true')
    parser.add_argument('--objects',action='store_true')
    parser.add_argument('--checkout',action='store_true')
    parser.add_argument('--worktrees',action='store_true')
    parser.add_argument('--index',action='store_true')
    parser.add_argument('--finalize',choices=('pending','ack','boundaries','upstream','resume','promotion_read','promotion_review'),default='')
    parser.add_argument('--runtime-merge',action='store_true')
    parser.add_argument('--seal',action='store_true')
    parser.add_argument('--compare',action='store_true')
    parser.add_argument('--results',action='store_true')
    args=parser.parse_args()
    if args.finalize and not args.runtime_merge:parser.error('--finalize requires --runtime-merge')
    if args.runtime_merge and not args.seal:parser.error('--runtime-merge requires --seal')
    if args.seal and not args.results:parser.error('--seal requires --results')
    if args.compare and not (args.checkout and args.objects and args.consumers):parser.error('--compare requires --checkout --objects --consumers')
    if args.results and (not args.worktrees or args.index):parser.error('--results requires --worktrees and is separate from --index')
    if args.index and not args.worktrees:parser.error('--index requires --worktrees')
    if args.worktrees and not (args.checkout and args.consumers):parser.error('--worktrees requires --checkout and --consumers')
    if args.checkout and not args.objects:parser.error('--checkout requires --objects for the nonempty fixture')
    packaged.select_helper(args.helper)
    if packaged.HELPER:
        import check_macos_user_broker
        check_macos_user_broker.HELPER=packaged.HELPER
    if args.client:
        try:client(args.client,args.uid,json.loads(args.oids),args.consumers,args.objects,args.checkout,args.worktrees,args.index,args.results,args.compare,args.seal,args.runtime_merge,args.finalize)
        except Exception:
            import traceback
            traceback.print_exc(file=sys.stderr)
            raise
        return
    if not args.run or not args.staged or sys.platform!='darwin' or os.getuid()!=0 or os.geteuid()!=0:
        parser.error('Use sudo with --run on macOS')
    uid=int(os.environ.get('SUDO_UID','0'))
    if not 501<=uid<identity.UID_MIN: raise RuntimeError('Run sudo from the ordinary login account')
    report=dict(runtime=str(Path(__file__).resolve().parents[1]),production_enabled=False,
        helper=str(packaged.HELPER) if packaged.HELPER else None,
        consumers=args.consumers,objects=args.objects,checkout=args.checkout,worktrees=args.worktrees,index=args.index,results=args.results,compare=args.compare,seal=args.seal,runtime_merge=args.runtime_merge,finalize=args.finalize,frozen_package_verified=False,case=acceptance(pwd.getpwuid(uid),args.consumers,args.objects,args.checkout,args.worktrees,args.index,args.results,args.compare,args.seal,args.runtime_merge,args.finalize))
    report['passed']=report['case']['passed']
    report['frozen_package_verified']=bool(packaged.HELPER and report['passed'])
    print(json.dumps(report,indent=2),flush=True)
    if not report['passed']: raise SystemExit(1)


if __name__=='__main__': main()
