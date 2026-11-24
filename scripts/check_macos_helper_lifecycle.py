"""Real launchd install/restart/upgrade/rollback/removal acceptance.

Explicit local ad-hoc test only. Uses a disposable project and a unique service,
not the user's app or project. Retains sealed versions and root recovery evidence.
Needs sudo; prints one JSON report, progress goes to stderr.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime, BACKGROUND, ISOLATION
if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid()==0:
    stage_runtime(__file__,support=('check_macos_user_broker.py',))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core import macos_helper_lifecycle as life
from backend.core import macos_identity as identity
from backend.core import macos_broker_service as broker
from backend.core.sandbox_macos import load_state
import check_macos_user_broker as harness


def step(name): print('Lifecycle acceptance: '+name,file=sys.stderr,flush=True)


def prepare_inert_candidate(destination):
    """Prepare an inert local fixture without system flags/platform trust."""
    destination.mkdir()
    executable=destination/'gitgo-host'
    shutil.copyfile('/usr/bin/false',executable)
    executable.chmod(0o755)
    # A relocated Apple platform binary is not a distributable helper. Give
    # this disposable fixture the same explicitly admitted local ad-hoc trust
    # as the acceptance bundle; leave the system executable unchanged.
    subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(executable)],
                   check=True,capture_output=True,timeout=10)
    subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(executable)],
                   check=True,capture_output=True,timeout=10)
    result=subprocess.run([str(executable)],capture_output=True,timeout=10)
    if result.returncode!=1: raise RuntimeError('Inert fixture did not exit normally with status 1')
    (destination/'intent.txt').write_text('Ad-hoc signed inert exit binary, deliberate startup failure fixture\n')


def service_pid(root):
    result=life.ctl('print','system/'+life.PREFIX+root.name)
    found=re.search(r'^\s*pid = (\d+)\s*$',result.stdout,re.M)
    return int(found[1]) if result.returncode==0 and found else None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--staged',action='store_true')
    parser.add_argument('--bundle',required=True);parser.add_argument('--allow-local-adhoc',action='store_true')
    args=parser.parse_args()
    uid=int(os.environ.get('SUDO_UID','0'))
    if not args.run or not args.staged or not args.allow_local_adhoc or os.getuid()!=0 or not 501<=uid<identity.UID_MIN:
        parser.error('Use sudo from a login account with --run --allow-local-adhoc')
    owner=pwd.getpwuid(uid)
    base=Path(tempfile.mkdtemp(prefix='gitgo-lifecycle-check-',dir='/private/tmp'));base.chmod(0o711)
    home=base/'user';home.mkdir(mode=0o700);os.chown(home,uid,owner.pw_gid)
    project=home/'project';project.mkdir(mode=0o700);os.chown(project,uid,owner.pw_gid)
    (project/'.git').mkdir(mode=0o700);os.chown(project/'.git',uid,owner.pw_gid)
    (project/'.git/config').write_text('metadata-canary');os.chown(project/'.git/config',uid,owner.pw_gid)
    name='accept-'+uuid.uuid4().hex[:20];root=life.service_root(name)
    canary=base/'outside.txt';canary.write_text('outside-canary');canary.chmod(0o666)
    # All versions have the same implementation: B's immutable resource marker
    # proves a real path switch, not compatibility with an unbuilt future release.
    candidate=base/'candidate';shutil.copytree(Path(args.bundle).resolve(),candidate,symlinks=True)
    (candidate/'_internal/lifecycle-test-version.txt').write_text('B\n')
    broken=base/'broken';prepare_inert_candidate(broken)
    env={'PATH':'/usr/bin:/bin','HOME':str(home),'TMPDIR':str(home),'PYTHONDONTWRITEBYTECODE':'1',
        'GITGO_STATE_HOME':str(home/'state'),'GITGO_CONFIG_PATH':str(home/'config.json'),
        'GITGO_LLM_CONFIG_PATH':str(home/'llm.json')}
    report=dict(passed=False,service=name,artifacts=str(base),registration=str(root),owner_uid=uid,
                production_enabled=False,checks={},tasks=[])
    child=None
    def write_task(text):
        # Fixture setup as admin, ordinary project retains its original owner.
        path=project/'task.py';path.write_text(text);os.chown(path,uid,owner.pw_gid)
    def start(mode):
        helper=life.configuration(root)['helper'];harness.HELPER=Path(helper)
        return subprocess.Popen(harness.child_command(root/'client.json',mode,home/'cancel',uid),
            user=uid,group=owner.pw_gid,extra_groups=[],env=env,cwd=home,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    def finish(proc,mode):
        out,err=proc.communicate(timeout=35)
        if proc.returncode: raise RuntimeError('Ordinary frozen Host failed: '+err[-4000:]+out[-1000:])
        response=json.loads(out)
        if not response['runtime']['frozen'] or response['uid']!=uid: raise RuntimeError('Wrong client runtime')
        active=json.loads(identity.bounded_read(root/'private/active.json',0,True));lease=Path(active['lease'])
        state=harness.wait_for(lambda:(s if (s:=load_state(lease))['phase']=='clean' else None),18,'Task UID not clean')
        assert state['launch_count']==1 and state['cleanup']['observed_empty'] and not state['cleanup']['issues']
        assert not identity.owned_rows(state['uid'])
        helper=life.configuration(root)['helper']
        assert state['argv']==[helper,'--gitgo-internal-role','tool-runner']
        supervisor=plistlib.loads(identity.bounded_read(lease/'job.plist',0,True))
        assert supervisor['ProgramArguments']==[helper,'--gitgo-internal-role','macos-execution-service',str(lease)]
        result=response['result']
        if mode=='cancel': assert result['is_error'] and (result.get('data') or {}).get('error')=='TOOL_CANCELLED'
        else:
            assert not result['is_error'] and result['receipt']['effect_state']=='committed'
            facts=json.loads((project/'result.json').read_text())
            assert facts['uid']==state['uid'] and all(v for k,v in facts.items() if k!='uid')
            assert canary.read_text()=='outside-canary'
        report['tasks'].append(dict(mode=mode,helper=helper,client=response,lease=str(lease),
            state={k:v for k,v in state.items() if k not in ('argv','env','profile')},remaining=[]))
        return state
    def success():
        import socket
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(2)
        try:
            write_task("import sys\nsys.argv=['task',"+repr(str(canary))+","+repr(str(listener.getsockname()[1]))+"]\n"+ISOLATION+"\nPath('outside-link').unlink()\nPath('result.json').write_text(json.dumps(result))\n")
            proc=start('success')
            try: finish(proc,'success')
            finally:
                if proc.poll() is None: proc.kill();proc.wait(timeout=5)
        finally: listener.close()
    try:
        step('install resident service A')
        initial=life.register(name,args.bundle,project,uid,lifetime=600,allow_local_adhoc=True)
        report['initial']=initial
        assert life.status(name)['authorization_live']
        installed=plistlib.loads(life.job_path(root).read_bytes())
        assert installed==life.job(root,initial['helper']) and installed['KeepAlive'] and installed['RunAtLoad']
        report['checks']['installed_resident_job']=True
        success();report['checks']['initial_frozen_execution']=True
        step('launchd automatic restart after SIGKILL')
        before=service_pid(root);assert before
        action=life.ctl('kill','SIGKILL','system/'+life.PREFIX+name);assert action.returncode==0,action.stderr
        after=harness.wait_for(lambda:(p if (p:=service_pid(root)) and p!=before else None),40,'KeepAlive did not restart')
        life.ready(root)
        report['checks']['automatic_restart']=dict(before=before,after=after)
        step('busy upgrade/uninstall refusal, then cancel detached descendants')
        write_task(BACKGROUND);child=start('cancel')
        previous_lease=report['tasks'][-1]['lease']
        def running():
            if child.poll() is not None: raise RuntimeError('Host exited before active task')
            current=json.loads(identity.bounded_read(root/'private/active.json',0,True))
            if current['lease']==previous_lease: return None
            state=load_state(Path(current['lease']))
            return state if state['phase']=='running' else None
        state=harness.wait_for(running,20,'Task not running')
        births=Path(state['workspace'])/'births.jsonl'
        def born():
            try: return identity.bounded_read(births,state['uid']).count(b'"kind": "birth"')
            except FileNotFoundError: return 0
        harness.wait_for(lambda:born()>=6,5,'Detached descendants not started')
        for operation in ('upgrade','uninstall'):
            try:
                if operation=='upgrade': life.upgrade(name,candidate,allow_local_adhoc=True)
                else: life.uninstall(name)
            except RuntimeError as exc:
                assert 'busy' in str(exc);report['checks']['busy_'+operation]=str(exc)
            else: raise RuntimeError('Active task lifecycle operation was accepted')
        assert life.configuration(root)==initial
        (home/'cancel').touch();finish(child,'cancel');child=None
        report['checks']['cancel_cleanup']=True
        step('upgrade to sealed version B')
        upgraded=life.upgrade(name,candidate,allow_local_adhoc=True)
        assert upgraded['helper']!=initial['helper'] and upgraded['previous']==initial['helper']
        assert plistlib.loads(life.job_path(root).read_bytes())==life.job(root,upgraded['helper'])
        success();report['checks']['upgrade_execution']=True;report['upgraded']=upgraded
        step('failed candidate startup restores B')
        try: life.upgrade(name,broken,allow_local_adhoc=True)
        except RuntimeError as exc:
            assert 'prior service restored' in str(exc);report['checks']['failed_upgrade_rollback']=str(exc)
        else: raise RuntimeError('Inert candidate unexpectedly passed readiness')
        assert life.configuration(root)['helper']==upgraded['helper']
        success();report['checks']['rollback_execution']=True
        step('recover interrupted maintenance without replay')
        saved=life.configuration(root)
        # Fault fixture: exact durable state between stop and candidate launch.
        with life.locked(root,'admin.lock'),life.locked(root,'admission.lock'):
            life.assert_idle(root)
            life.save(root,{**saved,'phase':'switching','previous':saved['helper'],'candidate':initial['helper']})
            life.unload(root)
        seen_before=(root/'private/seen.json').read_bytes()
        restored=life.recover(name);assert restored['helper']==saved['helper']
        life.recover(name)
        assert (root/'private/seen.json').read_bytes()==seen_before
        report['checks']['interrupted_recovery_no_replay']=True
        success();report['checks']['recovered_execution']=True
        step('expired authorization refuses execution while service remains resident')
        value=json.loads(identity.bounded_read(root/'private/grant.json',0,True))
        value['expires']=0;identity.atomic_json(root/'private/grant.json',value)
        assert life.status(name)['loaded'] and not life.status(name)['authorization_live']
        child=start('success');out,err=child.communicate(timeout=20)
        assert child.returncode==0 and json.loads(out)['result']['is_error'],err[-2000:]
        child=None
        assert (root/'private/seen.json').read_bytes()!=b''
        assert len(json.loads((root/'private/seen.json').read_bytes()))==len(report['tasks'])
        report['checks']['expired_grant_denied']=True
        step('uninstall and repeat uninstall')
        removed=life.uninstall(name);assert removed['phase']=='removed'
        assert life.uninstall(name)==removed
        assert not life.job_path(root).exists() and not (root/'client.json').exists() and not (root/'broker.sock').exists()
        assert life.ctl('print','system/'+life.PREFIX+name).returncode!=0
        assert (root/'private/service.json').exists() and Path(upgraded['helper']).exists()
        report['checks']['uninstall_idempotent_evidence_retained']=True
        report['passed']=True
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
        import traceback
        report['traceback']=traceback.format_exc()[-16000:]
        if root.exists():
            report['launchd']=(life.ctl('print','system/'+life.PREFIX+name).stdout)[-6000:]
            for member in ('service.err','last-error.json','lifecycle-failure.json'):
                path=root/'private'/member
                if path.exists(): report[member]=identity.bounded_read(path,0).decode(errors='replace')[-6000:]
    finally:
        if child is not None and child.poll() is None: child.kill();child.wait(timeout=5)
        # Keep a recovery service if cleanup cannot be positively established.
        try:
            if (root/'private/service.json').exists():
                harness.wait_for(lambda:idle(root),20,'Cleanup remains unresolved')
                life.uninstall(name)
                for path in (root/'private').glob('invocation-*.json'):
                    binding=json.loads(identity.bounded_read(path,0,True))
                    target='system/org.gitgo.execution-trial.'+binding['lease_id']
                    life.ctl('bootout',target)
                    assert life.ctl('print',target).returncode!=0
                report['final_service_unloaded']=life.ctl('print','system/'+life.PREFIX+name).returncode!=0
                report['final_idle']=True
        except Exception as exc: report['cleanup_error']=str(exc);report['passed']=False
    report['passed']=bool(report['passed'] and report.get('final_idle') and report.get('final_service_unloaded'))
    print(json.dumps(report,indent=2),flush=True)
    if not report['passed']: raise SystemExit(1)


def idle(root):
    try: life.assert_idle(root);return True
    except (RuntimeError,FileNotFoundError): return False


if __name__=='__main__':
    try: main()
    except Exception as exc:
        # Setup runs before the lifecycle report's try/finally. Preserve those
        # failures too instead of leaving redirected stdout as an empty file.
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps(dict(passed=False,phase='acceptance_setup_or_report',
                              error=type(exc).__name__+': '+str(exc))),flush=True)
        raise SystemExit(1)
