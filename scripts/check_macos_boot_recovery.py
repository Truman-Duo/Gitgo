"""Two-stage real reboot acceptance, using a disposable durable project.

Prepare leaves one stopped test broker registered for next boot and one real
interrupted lease. No task processes are left running. Resume checks automatic
startup and cleanup, explicit reauthorization and unknown-outcome retention.
Abort can retire the interrupted lease without restarting the computer.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time
import traceback
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime,BACKGROUND
if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid()==0:
    stage_runtime(__file__,support=('check_macos_user_broker.py',))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core import macos_helper_lifecycle as life,macos_project_authority as authority
from backend.core import macos_identity as identity,macos_broker_service as broker,macos_boot_recovery as recovery
from backend.core.macos_helper_install import trusted_directory
from backend.core.sandbox_macos import load_state,DURABLE_STORE
import check_macos_user_broker as harness

BASE=Path('/private/var/db/gitgo-reboot-check-v1')


def progress(message):print('Boot acceptance: '+message,file=sys.stderr,flush=True)


def child(base,manifest,mode,replay=''):
    uid=manifest['owner_uid'];owner=pwd.getpwuid(uid);user=base/'user'
    harness.HELPER=Path(manifest['helper'])
    env={'PATH':'/usr/bin:/bin','HOME':str(user),'TMPDIR':str(user),'PYTHONDONTWRITEBYTECODE':'1',
         'GITGO_STATE_HOME':str(user/'state'),'GITGO_CONFIG_PATH':str(user/'config.json'),
         'GITGO_LLM_CONFIG_PATH':str(user/'llm.json')}
    return subprocess.Popen(harness.child_command(life.service_root(manifest['service'])/'client.json',mode,user/'cancel',uid,replay),
        user=uid,group=owner.pw_gid,extra_groups=[],env=env,cwd=user,stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)


def collected(proc):
    out,err=proc.communicate(timeout=40)
    if proc.returncode:raise RuntimeError('Ordinary Host failed: '+err[-4000:]+out[-1000:])
    value=json.loads(out);assert value['uid']!=0 and value['runtime']['frozen'];return value


def job_gone(label):
    result=life.ctl('print','system/'+label)
    if result.returncode not in (0,113):raise RuntimeError('Could not inspect job: '+result.stderr)
    return result.returncode==113


def supervisor_stopped(lease):
    fd=os.open(lease/'service.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return False
        return True
    finally:os.close(fd)


def retire(base,manifest):
    root=life.service_root(manifest['service']);lease=Path(manifest['lease']) if manifest.get('lease') else None
    if lease:
        s=load_state(lease)
        if s['phase']!='clean':
            if s['boot_id']!=identity.boot_id():recovery.recover_store()
            else:
                code='from backend.core.macos_execution_service import serve; import sys; raise SystemExit(serve(sys.argv[1],recover_only=True))'
                result=subprocess.run([manifest['helper'],'--gitgo-internal-role','python','-B','-I','-c',code,str(lease)],
                    stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=20,env={'PATH':'/usr/bin:/bin','HOME':'/'})
                if result.returncode:raise RuntimeError('Explicit cleanup failed: '+result.stderr[-3000:])
        assert load_state(lease)['phase']=='clean' and not identity.owned_rows(load_state(lease)['uid'])
    life.uninstall(manifest['service'])
    for path in (root/'private').glob('invocation-*.json'):
        record=json.loads(identity.bounded_read(path,0,True));label='org.gitgo.execution-trial.'+record['lease_id']
        life.ctl('bootout','system/'+label)
        harness.wait_for(lambda:job_gone(label),10,'Execution job not removed')
    assert job_gone(life.PREFIX+manifest['service'])
    assert not (root/'client.json').exists() and not (root/'broker.sock').exists() and not life.job_path(root).exists()


def prepare(args,uid):
    name='boot-'+uuid.uuid4().hex[:20];base=BASE/name;root=life.service_root(name)
    trusted_directory(BASE.parent);BASE.mkdir(mode=0o711,exist_ok=True);trusted_directory(BASE)
    base.mkdir(mode=0o711);(base/'private').mkdir(mode=0o700)
    owner=pwd.getpwuid(uid);user=base/'user';user.mkdir(mode=0o700);os.chown(user,uid,owner.pw_gid)
    project=user/'project';project.mkdir(mode=0o700);os.chown(project,uid,owner.pw_gid)
    (project/'.git').mkdir(mode=0o700);os.chown(project/'.git',uid,owner.pw_gid)
    (project/'.git/config').write_text('boot-metadata-canary');os.chown(project/'.git/config',uid,owner.pw_gid)
    manifest=dict(schema=1,service=name,owner_uid=uid,boot=identity.boot_id(),project=str(project),phase='preparing')
    proc=None
    try:
        progress('install durable helper and execute as ordinary user')
        initial=life.register(name,args.bundle,project,uid,lifetime=3600,allow_local_adhoc=True)
        manifest['helper']=initial['helper'];authority.supports_renewal(manifest['helper'])
        task=project/'task.py';task.write_text("from pathlib import Path\nPath('result.txt').write_text('before-reboot-ok')\n");os.chown(task,uid,-1)
        result=collected(child(base,manifest,'success'));assert not result['result']['is_error']
        assert (project/'result.txt').read_text()=='before-reboot-ok'
        manifest['initial_client']=result
        previous=json.loads(identity.bounded_read(root/'private/active.json',0,True))['lease']
        task.write_text(BACKGROUND);proc=child(base,manifest,'broker_crash')
        def running():
            entry=json.loads(identity.bounded_read(root/'private/active.json',0,True))
            if entry['lease']==previous:return None
            lease=Path(entry['lease']);s=load_state(lease)
            if s['phase']!='running':return None
            try:births=identity.bounded_read(Path(s['workspace'])/'births.jsonl',s['uid'])
            except FileNotFoundError:return None
            return lease if births.count(b'"kind": "birth"')>=6 else None
        lease=harness.wait_for(running,20,'Background births did not start');assert lease.parent==DURABLE_STORE
        manifest['lease']=str(lease);manifest['invocation_id']=load_state(lease)['invocation_id']
        identity.atomic_json(base/'private/manifest.json',manifest)
        progress('interrupt supervisor and broker; wait for bounded children to exit')
        label='org.gitgo.execution-trial.'+lease.name
        result=life.ctl('bootout','system/'+label)
        harness.wait_for(lambda:job_gone(label),10,'Execution supervisor did not stop')
        harness.wait_for(lambda:supervisor_stopped(lease),10,'Supervisor still owns execution lease')
        life.unload(root)  # Retain plist, so the next system boot starts the broker.
        failed=collected(proc);proc=None;assert failed['result']['is_error']
        empty=0
        def no_children():
            nonlocal empty
            empty=0 if identity.owned_rows(load_state(lease)['uid']) else empty+1
            return empty>=3
        harness.wait_for(no_children,25,'Bounded descendants did not exit; abort required')
        state=load_state(lease)
        assert state['phase']!='clean' and state['launch_count']==1
        manifest.update(phase='awaiting_reboot',interrupted_client=failed,interrupted_state=state,
            prior_state_sha256=hashlib.sha256(identity.bounded_read(lease/'state.json',0,True)).hexdigest(),
            grant_id=broker.grant(root,live=False)['grant_id'])
        saved=broker.grant(root,live=False)
        manifest.update(project_identity=saved['identity'],project_object=saved['project_object'])
        identity.atomic_json(base/'private/manifest.json',manifest)
        return dict(passed=True,phase='awaiting_reboot',service=name,artifacts=str(base),lease=str(lease),
                    boot=manifest['boot'],task_uid=state['uid'],task_processes_remaining=False,
                    project_identity=manifest['project_identity'],project_object=manifest['project_object'],
                    broker_loaded=False,launch_plist_retained=True,production_enabled=False)
    except Exception as failure:
        if proc and proc.poll() is None:proc.kill();proc.wait(timeout=5)
        if manifest.get('helper'):
            try:retire(base,manifest)
            except Exception as exc:progress('Cleanup unresolved: '+str(exc))
        raise RuntimeError('Preparation failed for '+name+'; evidence at '+str(base)+': '+str(failure)) from failure


def resume(name,uid,abort=False):
    life.service_root(name);base=BASE/name;trusted_directory(base/'private')
    manifest=json.loads(identity.bounded_read(base/'private/manifest.json',0,True))
    assert manifest['schema']==1 and manifest['service']==name and manifest['owner_uid']==uid
    if manifest['phase']=='complete':return json.loads(identity.bounded_read(base/'private/report.json',0,True))
    if abort:
        retire(base,manifest);return dict(passed=True,phase='aborted',service=name)
    current=identity.boot_id()
    if not identity.valid_boot_id(manifest['boot']):
        raise RuntimeError('Legacy calendar-time preparation cannot verify a boot transition; retain evidence')
    if current==manifest['boot']:raise RuntimeError('macOS has not restarted; preparation retained')
    root=life.service_root(name);project=Path(manifest['project']);lease=Path(manifest['lease'])
    progress('verify automatic launchd startup, without manually starting broker')
    assert life.ctl('print','system/'+life.PREFIX+name).returncode==0
    life.ready(root)
    state=load_state(lease);proof=recovery.verify_recovery(lease,state,current)
    assert proof['prior_sha256']==manifest['prior_state_sha256'] and state['launch_count']==1
    assert state['invocation_id']==manifest['invocation_id']
    assert not life.status(name)['authorization_live']
    denied=collected(child(base,manifest,'credential-probe'));assert denied['passed']
    try:authority.renew(name)
    except ValueError as exc:assert 'Cross-boot' in str(exc)
    else:raise RuntimeError('Authority silently crossed a boot')
    progress('explicitly reauthorize; reconcile old invocation without replay')
    renewed=authority.renew(name,after_reboot=True)
    assert renewed['grant_id']!=manifest['grant_id']
    seen=(root/'private/seen.json').read_bytes()
    recovered=collected(child(base,manifest,'recover',manifest['invocation_id']))
    assert recovered['passed'] and recovered['receipt']['origin_boot']==manifest['boot']
    assert recovered['receipt']['boot']==current and recovered['receipt']['grant_id']==manifest['grant_id']
    assert (root/'private/seen.json').read_bytes()==seen and len(json.loads(seen))==2
    assert (project/'result.txt').read_text()=='before-reboot-ok' and (project/'.git/config').read_text()=='boot-metadata-canary'
    assert not (project/'births.jsonl').exists() and not (project/'detached').exists()
    report=dict(passed=True,phase='complete',service=name,origin_boot=manifest['boot'],current_boot=current,
                lease=str(lease),state=state,expired_authority_denied=denied,renewed=renewed,
                original_project_identity=manifest['project_identity'],
                current_project_identity=broker.grant(root,live=False)['identity'],
                project_object=broker.grant(root,live=False)['project_object'],
                recovery=recovered,output_not_published=True,no_replay=True,production_enabled=False)
    retire(base,manifest);report['final_unloaded']=True;report['final_idle']=True
    identity.atomic_json(base/'private/report.json',report)
    identity.atomic_json(base/'private/manifest.json',{**manifest,'phase':'complete'})
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--staged',action='store_true')
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--prepare',action='store_true');action.add_argument('--resume',action='store_true');action.add_argument('--abort',action='store_true')
    parser.add_argument('--name');parser.add_argument('--bundle');parser.add_argument('--allow-local-adhoc',action='store_true')
    parser.add_argument('--replace-test',help='Explicitly abort this earlier test before preparing a fresh one')
    args=parser.parse_args();uid=int(os.environ.get('SUDO_UID','0'))
    life.require_admin()
    if not args.run or not args.staged or not 501<=uid<identity.UID_MIN:parser.error('Run with sudo from an ordinary login account')
    if args.prepare and (not args.bundle or not args.allow_local_adhoc):parser.error('Preparation requires --bundle --allow-local-adhoc')
    if not args.prepare and not args.name:parser.error('Resume/abort requires --name from preparation report')
    if args.replace_test:
        if not args.prepare:parser.error('--replace-test requires --prepare')
        progress('retire earlier test '+args.replace_test)
        resume(args.replace_test,uid,abort=True)
    result=prepare(args,uid) if args.prepare else resume(args.name,uid,args.abort)
    print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps(dict(passed=False,error=type(exc).__name__+': '+str(exc))),flush=True)
        raise SystemExit(1)
