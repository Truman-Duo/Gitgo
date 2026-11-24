"""Real administrator acceptance for renewal, revocation and prior-grant recovery.

Creates a disposable project and a unique resident service. Never resets Host
pending outcomes or the broker replay ledger. Final cleanup preserves evidence.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime,BACKGROUND
if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid()==0:
    stage_runtime(__file__,support=('check_macos_user_broker.py',))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core import macos_helper_lifecycle as life,macos_project_authority as authority
from backend.core import macos_broker_service as broker,macos_identity as identity
from backend.core.sandbox_macos import load_state
import check_macos_user_broker as harness


def step(name): print('Authorization acceptance: '+name,file=sys.stderr,flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--staged',action='store_true')
    parser.add_argument('--bundle',required=True);parser.add_argument('--allow-local-adhoc',action='store_true')
    args=parser.parse_args();uid=int(os.environ.get('SUDO_UID','0'))
    if not args.run or not args.staged or not args.allow_local_adhoc or os.getuid()!=0 or not 501<=uid<identity.UID_MIN:
        parser.error('Use sudo from a login account with --run --allow-local-adhoc')
    owner=pwd.getpwuid(uid);base=Path(tempfile.mkdtemp(prefix='gitgo-auth-check-',dir='/private/tmp'));base.chmod(0o711)
    user=base/'user';user.mkdir(mode=0o700);os.chown(user,uid,owner.pw_gid)
    project=user/'project';project.mkdir(mode=0o700);os.chown(project,uid,owner.pw_gid)
    (project/'.git').mkdir(mode=0o700);os.chown(project/'.git',uid,owner.pw_gid)
    (project/'.git/config').write_text('metadata-canary');os.chown(project/'.git/config',uid,owner.pw_gid)
    name='auth-'+uuid.uuid4().hex[:20];root=life.service_root(name)
    env={'PATH':'/usr/bin:/bin','HOME':str(user),'TMPDIR':str(user),'PYTHONDONTWRITEBYTECODE':'1',
        'GITGO_STATE_HOME':str(user/'state'),'GITGO_CONFIG_PATH':str(user/'config.json'),
        'GITGO_LLM_CONFIG_PATH':str(user/'llm.json')}
    report=dict(passed=False,service=name,registration=str(root),artifacts=str(base),owner_uid=uid,
                production_enabled=False,checks={},tasks=[])
    child=None;copies=[]
    def write_task(text):
        path=project/'task.py';path.write_text(text);os.chown(path,uid,owner.pw_gid)
    def start(mode,config=None,replay=''):
        harness.HELPER=Path(life.configuration(root)['helper'])
        return subprocess.Popen(harness.child_command(config or root/'client.json',mode,user/'cancel',uid,replay),
            user=uid,group=owner.pw_gid,extra_groups=[],env=env,cwd=user,stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    def collect(proc):
        out,err=proc.communicate(timeout=35)
        if proc.returncode: raise RuntimeError('Ordinary Host failed: '+err[-4000:]+out[-1000:])
        value=json.loads(out);assert value['uid']==uid and value['runtime']['frozen'];return value
    def preserve_client():
        value=json.loads(identity.bounded_read(root/'client.json',uid,True))
        path=root/('old-client-'+uuid.uuid4().hex+'.json');identity.atomic_json(path,value);os.chown(path,uid,-1)
        copies.append(path);return path
    def denied(config=None):
        value=collect(start('credential-probe',config));assert value['passed'];return value['denial']
    def record(proc,mode,lease=None):
        value=collect(proc)
        if lease is None: lease=Path(json.loads(identity.bounded_read(root/'private/active.json',0,True))['lease'])
        state=harness.wait_for(lambda:(s if (s:=load_state(lease))['phase']=='clean' else None),20,'UID cleanup not confirmed')
        assert state['launch_count']==1 and state['cleanup']['observed_empty'] and not state['cleanup']['issues']
        assert not identity.owned_rows(state['uid'])
        result=value['result']
        if mode=='success':
            assert not result['is_error'] and result['receipt']['effect_state']=='committed'
            facts=json.loads((project/'result.json').read_text());assert facts['uid']==state['uid'] and facts['child']=='child-ok'
        else:
            assert result['is_error']
            assert not (project/'births.jsonl').exists() and not (project/'detached').exists()
        report['tasks'].append(dict(mode=mode,client=value,lease=str(lease),state={k:v for k,v in state.items() if k not in ('argv','env','profile')}))
        return state
    def success():
        write_task("import os,json,subprocess\nfrom pathlib import Path\nPath('result.json').write_text(json.dumps({'uid':os.getuid(),'child':subprocess.check_output(['/bin/sh','-c','printf child-ok']).decode()}))\n")
        proc=start('success')
        try: record(proc,'success')
        finally:
            if proc.poll() is None: proc.kill();proc.wait(timeout=5)
    def background():
        write_task(BACKGROUND);proc=start('broker_crash')
        previous=report['tasks'][-1]['lease'] if report['tasks'] else None
        def active():
            if proc.poll() is not None: raise RuntimeError('Host exited before background task started')
            try:
                entry=json.loads(identity.bounded_read(root/'private/active.json',0,True))
                if entry['lease']==previous:return None
                lease=Path(entry['lease']);state=load_state(lease)
                if state['phase']!='running':return None
                births=identity.bounded_read(Path(state['workspace'])/'births.jsonl',state['uid'])
                return lease if births.count(b'"kind": "birth"')>=6 else None
            except FileNotFoundError:return None
        try:return proc,harness.wait_for(active,20,'Detached descendants not running')
        except BaseException:
            proc.kill();proc.wait(timeout=5);raise
    try:
        step('install with explicit one-hour authority')
        report['initial']=life.register(name,args.bundle,project,uid,lifetime=3600,allow_local_adhoc=True)
        success();report['checks']['initial_execution']=True
        step('renew credential, reject old credential, enforce real expiry')
        old=preserve_client();previous=broker.grant(root,live=False)['grant_id'];seen=(root/'private/seen.json').read_bytes()
        renewed=authority.renew(name,lifetime=10);assert renewed['grant_id']!=previous
        assert (root/'private/seen.json').read_bytes()==seen
        report['checks']['old_credential_denied']=denied(old)
        success()
        expires=broker.grant(root,live=False)['expires']
        while broker.raw_time()<expires:time.sleep(.1)
        report['checks']['expired_credential_denied']=denied()
        assert not life.status(name)['authorization_live']
        authority.renew(name,lifetime=3600);success();report['checks']['renew_after_expiry']=True
        rejection=collect(start('reject',replay=report['tasks'][0]['state']['invocation_id']))
        assert rejection['passed'];report['checks']['old_invocation_replay_denied']=rejection['facts']
        step('revoke active detached descendants and refuse further work')
        child,lease=background()
        try:authority.renew(name)
        except RuntimeError as exc:assert 'busy' in str(exc)
        else:raise RuntimeError('Active execution was silently renewed')
        report['checks']['busy_renewal_refused']=True
        revoked=authority.revoke(name);assert revoked['cleanup_confirmed'];record(child,'revoked',lease);child=None
        report['checks']['revoked_credential_denied']=denied()
        assert authority.revoke(name)['cleanup_confirmed']
        authority.renew(name);success();report['checks']['explicit_reauthorization']=True
        step('interrupt credential publication; admission must stay closed')
        old=preserve_client();publish=authority.publish_client
        def interrupted(*args):
            publish(*args);raise OSError('intentional interruption after client publication')
        authority.publish_client=interrupted
        try:
            try:authority.renew(name)
            except OSError:pass
            else:raise RuntimeError('Renewal fault was not injected')
        finally:authority.publish_client=publish
        assert authority.pending(root) and life.status(name)['authorization_pending']
        report['checks']['partial_old_denied']=denied(old)
        report['checks']['partial_new_denied']=denied()
        authority.renew(name);assert not authority.pending(root)
        success();report['checks']['partial_renewal_recovered']=True
        step('broker crash, credential rotation, authenticated prior-grant recovery')
        child,lease=background();old_grant=broker.grant(root,live=False)['grant_id']
        result=life.ctl('kill','SIGKILL','system/'+life.PREFIX+name);assert result.returncode==0,result.stderr
        state=record(child,'broker_crash',lease);child=None
        life.ready(root)
        seen=(root/'private/seen.json').read_bytes();new=authority.renew(name)
        assert new['grant_id']!=old_grant
        recovered=collect(start('recover',replay=state['invocation_id']))
        assert recovered['passed'] and recovered['receipt']['grant_id']==old_grant
        assert (root/'private/seen.json').read_bytes()==seen and load_state(lease)['launch_count']==1
        assert len(json.loads(seen))==len(report['tasks'])
        report['reconciliation']=recovered;report['checks']['prior_grant_cleanup_without_replay']=True
        report['passed']=True
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc);report['traceback']=traceback.format_exc()[-16000:]
        for name2 in ('service.err','last-error.json'):
            path=root/'private'/name2
            if path.exists():report[name2]=identity.bounded_read(path,0).decode(errors='replace')[-6000:]
    finally:
        if child is not None and child.poll() is None:child.kill();child.wait(timeout=5)
        try:
            if (root/'private/service.json').exists():
                def clean():
                    try:life.assert_idle(root);return True
                    except (RuntimeError,FileNotFoundError):return False
                harness.wait_for(clean,20,'UID cleanup unresolved; retaining service')
                life.uninstall(name)
                for path in copies:
                    if path.exists():path.unlink()
                for path in (root/'private').glob('invocation-*.json'):
                    binding=json.loads(identity.bounded_read(path,0,True));target='system/org.gitgo.execution-trial.'+binding['lease_id']
                    life.ctl('bootout',target)
                    def unloaded():
                        observed=life.ctl('print',target)
                        if observed.returncode not in (0,113):
                            raise RuntimeError('Could not inspect execution service: '+observed.stderr[-1000:])
                        return observed.returncode==113
                    harness.wait_for(unloaded,10,'Execution service still registered after bootout')
                report['final_unloaded']=life.ctl('print','system/'+life.PREFIX+name).returncode==113
                report['final_idle']=True
        except Exception as exc:report['cleanup_error']=str(exc);report['passed']=False
    report['passed']=bool(report['passed'] and report.get('final_unloaded') and report.get('final_idle'))
    print(json.dumps(report,indent=2),flush=True)
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps(dict(passed=False,phase='setup_or_report',error=type(exc).__name__+': '+str(exc))),flush=True)
        raise SystemExit(1)
