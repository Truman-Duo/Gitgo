"""Operator-only fault acceptance for the project-authorized Git read bridge.

The staged test broker instruments ONLY its launch call: real fixed Git runs
first, then a bounded fixture holds the leader and a double-forked descendant.
No production request field/environment switch permits this instrumentation.
Protocol, native journal, UID/Seatbelt, supervisor and recovery are unmodified.
This proves lifecycle integration, not a naturally slow Git command. --helper
additionally requires the installed frozen runtime throughout the launch chain.
Retain uncertain leases; never unload their recovery service.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime
if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid()==0:
    stage_runtime(__file__,support=('check_macos_user_broker.py','check_macos_git_read.py','macos_git_packaged_support.py'))
if not getattr(sys,'frozen',False):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core import macos_broker_protocol as wire
from backend.core import macos_broker_service as broker
from backend.core import macos_identity as identity
from backend.core.sandbox_macos import load_state, lease_root
from check_macos_user_broker import wait_broker, launchctl, wait_for
import macos_git_packaged_support as packaged

MODES=('cancel','timeout','owner_disconnect','broker_crash','guardian_crash')
# Output appears in the real broker pipe before the fault. The Host must still
# withhold it until process completion and the bound cleanup receipt.
DRIVER=r'''
import errno,json,os,subprocess,sys,time
from pathlib import Path
command,canary=json.loads(sys.argv[1]),Path(sys.argv[2])
writing=command[-3:]==['hash-object','-w','--stdin']
checking_out=len(command)>3 and command[-2]==str(Path.cwd()/'checkout')
data=sys.stdin.buffer.read() if writing else None
result=subprocess.run(command,input=data,capture_output=True,timeout=10)
result.stdout=result.stdout.decode();result.stderr=result.stderr.decode()
expected=__import__('hashlib').sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()+'\n' if writing else 'blob\n'
checkout_oid=None
if checking_out and result.returncode==0:
    proof=json.loads(result.stdout)
    raw=__import__('base64').b64decode(proof['commit'],validate=True)
    checkout_oid=__import__('hashlib').sha1(b'commit '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    if checkout_oid!=command[-1] or Path('checkout/object.txt').read_bytes()!=b'object-view-test\n':
        raise RuntimeError('Checkout fixture content mismatch')
    expected=result.stdout
if result.returncode or result.stdout!=expected:
    raise RuntimeError('Actual Git query failed: '+result.stderr[-1000:])
for action in (lambda:canary.read_bytes(),lambda:canary.write_text('escaped')):
    try: action()
    except OSError as exc:
        if exc.errno not in (errno.EPERM,errno.EACCES): raise
    else: raise RuntimeError('Outside canary was accessible')
print(result.stdout,end='',flush=True)
pid=os.fork()
if pid==0:
    os.setsid()
    if os.fork(): os._exit(0)
    Path('detached-tmp').write_text(json.dumps({'pid':os.getpid(),'sid':os.getsid(0),
        'pgid':os.getpgrp(),'uid':os.getuid()}))
    Path('detached-tmp').replace('detached-ready')
    time.sleep(45)
    os._exit(0)
os.waitpid(pid,0)
end=time.monotonic()+5
while not Path('detached-ready').exists():
    if time.monotonic()>end: raise RuntimeError('Detached fixture not ready')
    time.sleep(.02)
Path('git-query-tmp').write_text(json.dumps({'git_output':result.stdout,'outside_denied':True,
    'checkout_oid':checkout_oid,'checkout_files_matched':checking_out,
    'pid':os.getpid(),'sid':os.getsid(0),'pgid':os.getpgrp(),'uid':os.getuid()}))
Path('git-query-tmp').replace('git-query-ready')
time.sleep(45)
'''


def instrumented_command(command,policy):
    """Harness-only wrapper for exactly the command already selected by broker."""
    from backend.core.macos_git_read import read_command
    if len(command)>3 and command[-2]==str(policy.workspace/'checkout'):
        from backend.core.macos_git_checkout import command as checkout_command
        expected=checkout_command(policy.workspace/'object-view',policy.workspace/'checkout',command[-1])
    elif command[-3:]==['hash-object','-w','--stdin']:
        from backend.core.macos_git_write import command as producer
        expected,_=producer(policy.workspace/'object-view',command[-3:],b'',None)
    else:expected=read_command(policy.workspace/'object-view',command[-3:])
    if command!=expected:
        raise ValueError('Fixture accepts only the fixed Git read command')
    return ['/usr/bin/python3','-I','-c',DRIVER,json.dumps(command),str(policy.workspace.parent/'canary')]


def serve_fixture(root):
    if sys.platform!='darwin' or os.getuid()!=0 or os.geteuid()!=0:
        raise PermissionError('Fault fixture broker requires the administrator harness')
    root=Path(root).absolute()
    if root.parent!=Path('/private/tmp') or not root.name.startswith('gitgo-macos-broker-'):
        raise ValueError('Fault broker cannot instrument a persistent installation')
    if getattr(sys,'frozen',False):
        from backend.core.macos_helper_install import verify_installed_runtime
        verify_installed_runtime()
        identity.atomic_json(root/'private/fixture-runtime.json',packaged.evidence())
    original=broker.sandbox_popen
    def controlled(command,policy,**kwargs):
        wrapped=instrumented_command(command,policy)
        canary=policy.workspace.parent/'canary'
        with canary.open('x') as stream: stream.write('unchanged')
        canary.chmod(0o666)
        return original(wrapped,policy,**kwargs)
    broker.sandbox_popen=controlled
    try: broker.serve(root)
    finally: broker.sandbox_popen=original


def launch_fixture(root):
    label='org.gitgo.git-read-fault.'+root.name.removeprefix('gitgo-macos-broker-')
    job=dict(Label=label,ProgramArguments=packaged.command(Path(__file__),'--fixture-broker',root),RunAtLoad=True,KeepAlive=False,Umask=0o077,ExitTimeOut=3,
        StandardOutPath=str(root/'private/service.out'),StandardErrorPath=str(root/'private/service.err'))
    path=root/'private/job.plist';path.write_bytes(plistlib.dumps(job));path.chmod(0o600)
    result=launchctl('bootstrap','system',str(path))
    if result.returncode: raise RuntimeError(result.stderr)
    return label


def client(config,uid,mode,oid,marker,consumers=False,objects=False,checkout=False):
    if os.getuid()!=uid or os.geteuid()!=uid or uid==0: raise RuntimeError('Ordinary Host required')
    from backend.core.macos_broker_client import broker_session, read_git_objects
    from backend.core.sandbox_read import ReadRoot
    from backend.core.storage import get_storage,close_owned_storage
    from backend.core.storage.native_invocations import NAMESPACE,pending_native_invocations
    cancelled=threading.Event();done=threading.Event()
    def watch():
        while not done.wait(.02):
            if Path(marker).exists(): cancelled.set();return
    watcher=threading.Thread(target=watch,daemon=True)
    if mode=='cancel': watcher.start()
    try:
        with broker_session(config) as binding:
            project=Path(binding['project']);store=get_storage(project)
            accepted=False;error='';output=None
            try:
                if checkout:
                    from backend.core.macos_broker_client import checkout_git_commit
                    with ReadRoot(project/'.git/objects') as root:
                        output=checkout_git_commit(project,root,oid,
                            timeout=8 if mode=='timeout' else 30,cancellation=cancelled).commit
                elif consumers or objects:
                    from types import SimpleNamespace
                    from backend.adapters.local_git_runner import ProtectedLocalGitRunner
                    from backend.core.loop.native_invocation_scope import native_invocation_scope
                    ctx=SimpleNamespace(workspace_path=project,storage=store,cancellation=cancelled,
                        session=SimpleNamespace(session_id=uuid.uuid4().hex),process=SimpleNamespace(recovery=None))
                    with native_invocation_scope(ctx,uuid.uuid4().hex,0):
                        result=ProtectedLocalGitRunner(project).run(['hash-object','-w','--stdin'] if objects else ['cat-file','-t',oid],timeout=8 if mode=='timeout' else 30,**({'text':False,'input_bytes':b'fault-object'} if objects else {}))
                        if result.returncode:raise RuntimeError('Git read failed')
                        output=result.stdout.decode("ascii") if isinstance(result.stdout,bytes) else result.stdout
                else:
                    with ReadRoot(project/'.git/objects') as root:
                        output=read_git_objects(project,root,['cat-file','-t',oid],
                            timeout=8 if mode=='timeout' else 30,cancellation=cancelled)
                accepted=True
            except Exception as exc: error=type(exc).__name__+': '+str(exc)
            rows=store.list_state_refs(NAMESPACE,prefix='history/',limit=10)
            history=[json.loads(store.read_blob(r['value_ref'],repair=False)) for r in rows]
            pending=pending_native_invocations(store,include_active=True)
            print(json.dumps(dict(uid=os.getuid(),runtime=packaged.evidence(),consumers=consumers,objects=objects,checkout=checkout,output_accepted=accepted,output=output,error=error,
                history=history,pending=pending,cancel_requested=cancelled.is_set())),flush=True)
    finally:
        done.set()
        if watcher.is_alive():watcher.join(1)
        close_owned_storage()


def reconcile(config,uid,identifier):
    if os.getuid()!=uid or os.geteuid()!=uid or uid==0:raise RuntimeError('Ordinary recovery Host required')
    from backend.core.macos_broker_client import broker_session,read_git_objects
    from backend.core.sandbox_read import ReadRoot
    from backend.core.sandbox import SandboxDenied
    from backend.core.storage import get_storage,close_owned_storage
    from backend.core.storage.native_invocations import NAMESPACE,pending_native_invocations,reconcile_native_invocations
    try:
        with broker_session(config) as binding:
            project=Path(binding['project']);store=get_storage(project)
            before=pending_native_invocations(store,include_active=True)
            assert len(before)==1 and before[0]['invocation_id']==identifier
            old=store.get_state_ref(NAMESPACE,'pending/'+identifier)
            changed=reconcile_native_invocations(store)
            head=store.get_state_ref(NAMESPACE,'pending/'+identifier)
            item=json.loads(store.read_blob(head,repair=False))
            again=reconcile_native_invocations(store)
            blocked=False
            with ReadRoot(project/'.git/objects') as root:
                try:read_git_objects(project,root,['cat-file','-t','0'*40],timeout=5)
                except SandboxDenied as exc:blocked=exc.code=='NATIVE_OUTCOME_UNCONFIRMED'
            facts=dict(original_retained=item.get('recovery_previous_ref')==old and bool(store.read_blob(old,repair=False)),
                cleanup_confirmed=item.get('runtime_cleanup',{}).get('complete') is True,
                outcome_unknown=item.get('recovered_outcome')=='unknown',
                idempotent=changed==[identifier] and again==[] and store.get_state_ref(NAMESPACE,'pending/'+identifier)==head,
                admission_blocked=blocked,history_not_invented=store.get_state_ref(NAMESPACE,'history/'+identifier) is None)
            print(json.dumps(dict(passed=all(facts.values()),uid=os.getuid(),runtime=packaged.evidence(),facts=facts,
                receipt=item.get('recovery_receipt'),pending=pending_native_invocations(store,include_active=True))),flush=True)
    finally:close_owned_storage()


def snapshot(project):
    return {str(p.relative_to(project)):(p.stat().st_uid,p.stat().st_mode,
        hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
        for p in [project,*project.rglob('*')]}


def observe_ready(lease,objects=False,checkout_commit=None):
    state=load_state(lease);work=Path(state['workspace'])
    try:
        ready=json.loads(identity.bounded_read(work/'git-query-ready',state['uid']))
        detached=json.loads(identity.bounded_read(work/'detached-ready',state['uid']))
    except FileNotFoundError:return None
    rows=identity.owned_rows(state['uid']);pids={r['pid'] for r in rows}
    if checkout_commit:
        output_ok=ready.get('checkout_oid')==checkout_commit and ready.get('checkout_files_matched') is True
        from backend.core.macos_git_checkout import object_oid
        try:
            import base64
            output_ok=output_ok and object_oid('commit',base64.b64decode(json.loads(ready['git_output'])['commit'],validate=True))==checkout_commit
        except (KeyError,ValueError,TypeError):output_ok=False
    else:output_ok=ready.get('git_output')==(hashlib.sha1(b'blob 12\0fault-object').hexdigest()+'\n' if objects else 'blob\n')
    if (not output_ok or ready.get('outside_denied') is not True or
            ready['uid']!=state['uid'] or detached['uid']!=state['uid'] or
            ready['pid'] not in pids or detached['pid'] not in pids or
            ready['sid']==detached['sid'] or ready['pgid']==detached['pgid'] or
            state['phase']!='running' or state['launch_count']!=1 or
            os.getsid(detached['pid'])!=detached['sid'] or os.getpgid(detached['pid'])!=detached['pgid']):
        raise RuntimeError('Fixture did not prove a live detached descendant after actual Git output')
    return dict(leader=ready,detached=detached,owned_rows=rows)


def case(owner,mode,consumers=False,objects=False,checkout=False):
    from check_macos_git_read import fixture
    base=Path(tempfile.mkdtemp(prefix='gitgo-git-fault-check-',dir='/private/tmp'));base.chmod(0o711)
    home=base/'user';home.mkdir(mode=0o700);os.chown(home,owner.pw_uid,owner.pw_gid)
    _,oid,first,_,extension=fixture(home,checkout=checkout)
    if checkout:oid=first
    project=home/'source';(project/'.git').rmdir();(project/'old-git').rename(project/'.git')
    metadata=project/'.git/gitgo';metadata.mkdir();(metadata/'project-id').write_text(str(uuid.uuid4())+'\n')
    for directory,dirs,files in os.walk(project):
        os.chown(directory,owner.pw_uid,owner.pw_gid)
        for name in files:os.chown(Path(directory)/name,owner.pw_uid,owner.pw_gid)
    before=snapshot(project)
    endpoint=broker.provision(project,owner.pw_uid,lifetime=240)
    env=dict(PATH='/usr/bin:/bin',HOME=str(home),TMPDIR=str(home),PYTHONDONTWRITEBYTECODE='1',
        GITGO_STATE_HOME=str(home/'state'),GITGO_CONFIG_PATH=str(home/'config.json'),GITGO_LLM_CONFIG_PATH=str(home/'llm.json'))
    report=dict(case=mode,passed=False,broker=str(endpoint),artifacts=str(base),test_launch_wrapper=True)
    label=child=lease=None
    try:
        label=launch_fixture(endpoint);wait_broker(endpoint,label)
        child=subprocess.Popen(packaged.command(Path(__file__),'--client',endpoint/'client.json',
            '--uid',owner.pw_uid,'--mode',mode,'--oid',oid,'--marker',home/'cancel',*(['--consumers'] if consumers else []),*(['--objects'] if objects else []),*(['--checkout'] if checkout else [])),
            user=owner.pw_uid,group=owner.pw_gid,extra_groups=[],env=env,cwd=home,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def active():
            if child.poll() is not None:raise RuntimeError('Host ended before fault readiness')
            try:
                value=json.loads(identity.bounded_read(endpoint/'private/active.json',0,True))
                path=Path(value['lease']);state=load_state(path)
                return path if state['phase']=='running' else None
            except FileNotFoundError:return None
        lease=wait_for(active,6,'No running Git task')
        report['lease']=str(lease)
        report['before_fault']=wait_for(lambda:observe_ready(lease,objects,oid if checkout else None),5,'Git fixture not ready')
        state=load_state(lease);identifier=state['invocation_id'];fault=broker.raw_time()
        if mode=='cancel':(home/'cancel').touch()
        elif mode=='owner_disconnect':child.kill()
        elif mode in ('broker_crash','guardian_crash'):
            target=label if mode=='broker_crash' else 'org.gitgo.execution-trial.'+lease.name
            action=launchctl('kill','SIGKILL','system/'+target)
            if action.returncode:raise RuntimeError(action.stderr)
        out,err=child.communicate(timeout=40)
        report['client_exit']=child.returncode;report['client_stderr']=err[-4000:]
        if out.strip():report['client']=json.loads(out)
        final=wait_for(lambda:(s if (s:=load_state(lease))['phase']=='clean' else None),20,'Cleanup unconfirmed')
        report['fault_to_clean_seconds']=broker.raw_time()-fault
        report['state']={k:v for k,v in final.items() if k not in ('argv','env','profile')}
        report['remaining']=identity.owned_rows(final['uid'])
        response=report.get('client',{})
        good=(final['launch_count']==1 and final['cleanup']['observed_empty'] is True and
            not final['cleanup'].get('issues') and not report['remaining'] and
            0<final['finished']-final['started']<40)
        if mode=='owner_disconnect':good=good and child.returncode==-signal.SIGKILL and not out.strip()
        else:
            good=good and child.returncode==0 and response.get('output_accepted') is False and response.get('output') is None and bool(response.get('error'))
        if mode=='cancel':
            good=good and response.get('cancel_requested') is True and 'cancelled' in response.get('error','') and final['reason']=='cancel'
        elif mode=='timeout':
            good=good and 'timed out' in response.get('error','') and final['reason'] in ('cancel','timeout')
        elif mode=='owner_disconnect':
            good=good and final['reason']=='cancel'
        elif mode=='broker_crash':
            good=good and final['reason']=='owner_eof'
        elif mode=='guardian_crash':
            good=good and final['generation']==2 and final['reason']=='automatic_recovery'
        if mode in ('owner_disconnect','broker_crash'):
            launchctl('bootout','system/'+label);label=launch_fixture(endpoint);wait_broker(endpoint,label)
            recovered=subprocess.run(packaged.command(Path(__file__),'--recover',endpoint/'client.json',
                '--uid',owner.pw_uid,'--invocation',identifier),user=owner.pw_uid,group=owner.pw_gid,
                extra_groups=[],env=env,cwd=home,capture_output=True,text=True,timeout=20)
            report['recovery_stderr']=recovered.stderr[-4000:]
            report['recovery']=json.loads(recovered.stdout) if recovered.stdout.strip() else {}
            good=good and recovered.returncode==0 and report['recovery'].get('passed') is True
        else:
            history=response.get('history',[])
            good=good and len(history)==1 and history[0]['invocation_id']==identifier and not response.get('pending')
            good=good and history[0]['runtime_cleanup']['complete'] is True
        if packaged.HELPER:
            runtime=json.loads(identity.bounded_read(endpoint/'private/fixture-runtime.json',0,True))
            report['fixture_runtime']=runtime
            report['frozen_supervisor']=packaged.task_evidence(lease,final,packaged.HELPER)
            checked=[runtime]+([response.get('runtime',{})] if mode!='owner_disconnect' else [])
            if mode in ('owner_disconnect','broker_crash'):checked.append(report['recovery'].get('runtime',{}))
            good=good and report['frozen_supervisor'] and all(r.get('embedded_backend') is True and r.get('executable')==str(packaged.HELPER) for r in checked)
        report['seen']=json.loads(identity.bounded_read(endpoint/'private/seen.json',0,True))
        report['source_unchanged']=snapshot(project)==before and not extension.exists()
        report['canary_unchanged']=Path(final['workspace']).parent.joinpath('canary').read_text()=='unchanged'
        report['passed']=bool(good and report['seen']==[identifier] and load_state(lease)['launch_count']==1 and
            report['source_unchanged'] and report['canary_unchanged'])
    except Exception as exc:report['error']=type(exc).__name__+': '+str(exc)
    finally:
        if child is not None:
            if child.poll() is None:child.kill()
            out,err=child.communicate(timeout=5)
            if err:report['client_stderr']=err[-4000:]
            if out and 'client' not in report:report['client_raw']=out[-8000:]
        if label:launchctl('bootout','system/'+label)
        try:
            if lease is None and (endpoint/'private/active.json').exists():
                lease=Path(json.loads(identity.bounded_read(endpoint/'private/active.json',0,True))['lease'])
            if lease is not None:
                report['lease']=str(lease)
                final=wait_for(lambda:(s if (s:=load_state(lease))['phase']=='clean' else None),20,'Retain recovery service')
                report['final_state']={k:v for k,v in final.items() if k not in ('argv','env','profile')}
                report['final_remaining']=identity.owned_rows(final['uid'])
                if report['final_remaining'] or not final['cleanup'].get('observed_empty') or final['cleanup'].get('issues'):
                    raise RuntimeError('UID not confirmed clean')
                job='system/org.gitgo.execution-trial.'+lease.name
                launchctl('bootout',job);status=launchctl('print',job)
                report['task_job_unloaded']=status.returncode==113 and 'Could not find service' in status.stderr
        except Exception as exc:report['cleanup_error']=type(exc).__name__+': '+str(exc);report['passed']=False
        if label:
            status=launchctl('print','system/'+label)
            report['broker_unloaded']=status.returncode==113 and 'Could not find service' in status.stderr
        for name in ('service.err','last-error.json'):
            p=endpoint/'private'/name
            if p.exists():report[name]=identity.bounded_read(p,0).decode(errors='replace')[-4000:]
        report['passed']=bool(report['passed'] and report.get('task_job_unloaded') and report.get('broker_unloaded'))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--staged',action='store_true')
    parser.add_argument('--fixture-broker');parser.add_argument('--client');parser.add_argument('--recover')
    parser.add_argument('--uid',type=int,default=0);parser.add_argument('--mode',choices=MODES,default='cancel')
    parser.add_argument('--oid');parser.add_argument('--marker');parser.add_argument('--invocation')
    parser.add_argument('--helper')
    parser.add_argument('--consumers',action='store_true')
    parser.add_argument('--objects',action='store_true')
    parser.add_argument('--checkout',action='store_true')
    args=parser.parse_args()
    if args.checkout and (args.objects or args.consumers):parser.error('Checkout faults are a distinct operation; omit --objects/--consumers')
    packaged.select_helper(args.helper)
    if args.fixture_broker:serve_fixture(args.fixture_broker);return
    if args.client:client(args.client,args.uid,args.mode,args.oid,args.marker,args.consumers,args.objects,args.checkout);return
    if args.recover:reconcile(args.recover,args.uid,args.invocation);return
    if not args.run or not args.staged or sys.platform!='darwin' or os.getuid()!=0 or os.geteuid()!=0:
        parser.error('Use sudo with --run on macOS')
    uid=int(os.environ.get('SUDO_UID','0'))
    if not 501<=uid<identity.UID_MIN:raise RuntimeError('Run sudo from the ordinary login account')
    report=dict(runtime=str(Path(__file__).resolve().parents[1]),production_enabled=False,frozen_package_verified=False,
        test_launch_wrapper=True,consumers=args.consumers,objects=args.objects,checkout=args.checkout,helper=str(packaged.HELPER) if packaged.HELPER else None,cases=[])
    for mode in MODES:
        print('Git broker fault: '+mode,file=sys.stderr,flush=True)
        result=case(pwd.getpwuid(uid),mode,args.consumers,args.objects,args.checkout);report['cases'].append(result)
        if not result['passed']:break
    report['passed']=len(report['cases'])==len(MODES) and all(item['passed'] for item in report['cases'])
    report['frozen_package_verified']=bool(packaged.HELPER and report['passed'])
    print(json.dumps(report,indent=2),flush=True)
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
