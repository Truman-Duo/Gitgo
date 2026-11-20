"""Operator acceptance: ordinary Host -> project grant -> dedicated UID.

Creates disposable real projects, temporary launchd jobs and private journals.
Never runs the Dashboard as root or changes an existing project's ACL/owner.
Reports are evidence, not permission to enable the production capability.
"""
from __future__ import annotations
import argparse
import dataclasses
import json
import os

# Pure validation/report helpers also import on non-POSIX hosts.
if os.name == 'posix':
    import pwd

from pathlib import Path
import plistlib
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime, BACKGROUND, ISOLATION

if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid()==0:
    stage_runtime(__file__)

SOURCE=Path(__file__).resolve().parents[1]
if not getattr(sys,'frozen',False): sys.path.insert(0,str(SOURCE))
HELPER=None
from backend.core import macos_broker_protocol as wire
from backend.core import macos_broker_service as broker
from backend.core import macos_identity as identity
from backend.core.sandbox_macos import load_state


def client(config, mode, marker, expected_uid, replay=''):
    if os.getuid()!=expected_uid or os.geteuid()!=expected_uid or expected_uid==0:
        raise RuntimeError('Acceptance client must be an actual ordinary OS user')
    from backend.core.macos_broker_client import broker_session
    runtime=dict(frozen=bool(getattr(sys,'frozen',False)),executable=sys.executable)
    with broker_session(config) as binding:
        if mode=='credential-probe':
            # A valid, inert request must be refused because of its credential
            # state, not because its shape or selected command is invalid.
            fd=wire.open_directory(binding['project'])
            connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);connection.settimeout(5)
            try:
                connection.connect(binding['socket']);assert wire.peer_uid(connection)==0
                wire.send_directory(connection,fd)
                wire.send(connection,dict(op='run',token=binding['token'],grant_id=binding['grant_id'],
                    invocation_id=uuid.uuid4().hex,execution_basis='b'*64,timeout=5,
                    request=dict(tool_name='exec_command',args={'_workspace':'/role','argv':['/usr/bin/true']},
                                 _native_sandbox_workspace='/role'),image=dict(files=[],directories=[])))
                response=wire.receive(connection)
                assert response.get('event')=='denied',response
                print(json.dumps(dict(passed=True,uid=os.getuid(),runtime=runtime,denial=response.get('error'))))
            finally: connection.close();os.close(fd)
            return
        if mode=='reject':
            facts=reject_requests(binding,replay)
            print(json.dumps(dict(uid=os.getuid(),runtime=runtime,passed=all(facts.values()),facts=facts)))
            return
        from backend.core.loop.models import AgentProcess, RingLevel
        from backend.core.loop.session import AgentSession
        from backend.core.loop.tools import ToolRegistry
        from backend.core.loop.execution_context import ExecutionContext
        from backend.core.loop.tool_pipeline import ToolPipeline
        from backend.core.tools.catalog import build_workspace_tools
        from backend.core.storage import close_owned_storage
        root=binding['project']
        process=AgentProcess('broker-acceptance','worker',RingLevel.RING_3,active_task_id='task',
            worktree_path=root,session=AgentSession(),tool_registry=ToolRegistry(['exec_command']))
        process.update_context_snapshot(lambda s:{**s,'project_name':'broker-acceptance'})
        ctx=ExecutionContext(process,process.session,root)
        if mode=='recover':
            from backend.core.storage import get_storage
            from backend.core.storage.native_invocations import NAMESPACE, pending_native_invocations
            from backend.core.loop.native_invocation_scope import check_native_recovery
            from backend.core.sandbox import SandboxDenied
            store=get_storage(root)
            before=pending_native_invocations(store)
            assert len(before)==1 and before[0]['invocation_id']==replay
            codes=[];heads=[]
            try:
                for _ in range(2):
                    try: check_native_recovery(ctx)
                    except SandboxDenied as exc: codes.append(exc.code)
                    else: raise RuntimeError('Interrupted task admission was not blocked')
                    heads.append(store.get_state_ref(NAMESPACE,'pending/'+replay))
                after=pending_native_invocations(store)
                item=json.loads(store.read_blob(heads[-1],repair=False))
                facts=dict(cleanup_confirmed=after[0]['runtime_cleanup']['complete'] is True,
                    outcome_unknown=after[0]['code']=='NATIVE_OUTCOME_UNCONFIRMED' and item['recovered_outcome']=='unknown',
                    idempotent=heads[0]==heads[1],blocked=codes==['NATIVE_OUTCOME_UNCONFIRMED']*2,
                    original_retained=bool(store.read_blob(item['recovery_previous_ref'],repair=False)),
                    revision_saved=store.get_state_ref(NAMESPACE,'recovery/'+replay)==heads[-1],
                    no_automatic_resume=process.recovery['resume_forbidden'] is True)
                print(json.dumps(dict(uid=os.getuid(),runtime=runtime,passed=all(facts.values()),facts=facts,
                    before=before,after=after,receipt=item.get('recovery_receipt'))),flush=True)
            finally: close_owned_storage()
            return
        stop=threading.Event()
        def cancellation():
            while not stop.wait(.03):
                if Path(marker).exists(): ctx.cancel();return
        watcher=threading.Thread(target=cancellation,daemon=True)
        if mode=='cancel': watcher.start()
        try:
            tool=build_workspace_tools(root)['exec_command']
            args={'argv':['/usr/bin/python3','-I','task.py'],'timeout':2 if mode=='timeout' else 12}
            result=ToolPipeline().execute({'name':'exec_command','args':args},tool,ctx,'broker-'+mode)
            print(json.dumps(dict(uid=os.getuid(),runtime=runtime,result=dataclasses.asdict(result)),default=str),flush=True)
        finally:
            stop.set()
            if watcher.is_alive(): watcher.join(1)
            close_owned_storage()


def reject_requests(binding,replay):
    project=Path(binding['project']);other=project.parent/'other';other.mkdir(exist_ok=True)
    facts={}
    def attempt(name,changes=None,wrong_fd=False,stale=False):
        request=dict(op='run',token=binding['token'],grant_id=binding['grant_id'],
            invocation_id=uuid.uuid4().hex,execution_basis='b'*64,timeout=5,
            request={'tool_name':'exec_command','args':{'_workspace':'/role-only'},
                     '_native_sandbox_workspace':'/role-only'},image={'files':[],'directories':[]})
        request.update(changes or {})
        fd=wire.open_directory(other if wrong_fd else project)
        connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);connection.settimeout(5)
        saved=project.with_name('project-held')
        try:
            if stale: project.rename(saved);project.mkdir(mode=0o700)
            connection.connect(binding['socket'])
            assert wire.peer_uid(connection)==0
            wire.send_directory(connection,fd);wire.send(connection,request)
            result=wire.receive(connection)
            facts[name]=result.get('event')=='denied' and result.get('invocation_id')==request['invocation_id']
        finally:
            connection.close();os.close(fd)
            if stale: project.rmdir();saved.rename(project)
    attempt('wrong_token',{'token':'invalid'})
    attempt('claimed_uid',{'owner_uid':0})
    attempt('client_selected_policy',{'profile':'(allow default)'})
    attempt('other_project',wrong_fd=True)
    attempt('replaced_project',stale=True)
    attempt('replay',{'invocation_id':replay})
    return facts


def child_command(config,mode,marker,uid,replay=''):
    command=[str(HELPER),'--gitgo-internal-role','python'] if HELPER else [sys.executable]
    return [*command,'-B','-I',str(Path(__file__)), '--client',str(config),
            '--mode',mode,'--marker',str(marker),'--uid',str(uid),'--replay',replay]


def launch_broker(root):
    label='org.gitgo.user-broker-trial.'+root.name.removeprefix('gitgo-macos-broker-')
    bootstrap='import runpy,sys;sys.path.insert(0,'+repr(str(SOURCE))+');runpy.run_module("backend.core.macos_broker_service",run_name="__main__")'
    command=[str(HELPER),'--gitgo-internal-role','macos-broker-service',str(root)] if HELPER else [sys.executable,'-B','-I','-c',bootstrap,str(root)]
    job=dict(Label=label,ProgramArguments=command,
        RunAtLoad=True,KeepAlive=False,Umask=0o077,ExitTimeOut=3,
        StandardOutPath=str(root/'private/service.out'),StandardErrorPath=str(root/'private/service.err'))
    path=root/'private/job.plist';path.write_bytes(plistlib.dumps(job));path.chmod(0o600)
    subprocess.run(['/bin/launchctl','bootstrap','system',str(path)],check=True,capture_output=True,timeout=8)
    return label


def wait_for(predicate,seconds,message):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=predicate()
        if value: return value
        time.sleep(.04)
    raise TimeoutError(message)


def launchctl(*args):
    return subprocess.run(['/bin/launchctl',*args],capture_output=True,text=True,timeout=8)


def wait_unloaded(job, seconds=20):
    """bootout may return before launchd finishes removing its job record."""
    def absent():
        status=launchctl('print',job)
        if status.returncode==113 and 'Could not find service' in status.stderr:
            return True
        if status.returncode:
            raise RuntimeError('Cannot verify job removal: '+status.stderr)
        return False
    return wait_for(absent,seconds,'Job removal not confirmed: '+job)


def wait_broker(root,label,seconds=40):
    """Bounded service cold-start allowance, separate from task/cleanup limits.

    A socket pathname may be stale. Readiness requires a root peer replying to
    the deliberately unauthorized root caller, without issuing a task command.
    """
    started=time.monotonic();deadline=started+seconds;next_status=started
    while time.monotonic()<deadline:
        probe=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);probe.settimeout(.5)
        try:
            probe.connect(str(root/'broker.sock'))
            if wire.peer_uid(probe)!=0: raise RuntimeError('Broker readiness peer is not root')
            if wire.receive(probe).get('event')!='denied': raise RuntimeError('Unexpected readiness response')
            return time.monotonic()-started
        except (OSError,EOFError): pass
        finally: probe.close()
        if time.monotonic()>=next_status:
            status=launchctl('print','system/'+label)
            if status.returncode or (re.search(r'^\s*state = not running\s*$',status.stdout,re.M) and
                    re.search(r'^\s*last exit code = -?\d+\s*$',status.stdout,re.M)):
                raise RuntimeError('Broker exited before readiness: '+(status.stdout+status.stderr)[-3000:])
            next_status=time.monotonic()+1
        time.sleep(.1)
    raise TimeoutError('Broker was not ready within '+str(seconds)+' seconds')


def acceptance_case(name,owner,*,task_source=None,resource_limits=None,expected_resource_reason=None,observer=None):
    if task_source is not None and name!='success':
        raise ValueError('Custom bounded workload requires the success acceptance path')
    root=Path(tempfile.mkdtemp(prefix='gitgo-broker-check-',dir='/private/tmp'))
    root.chmod(0o711)
    home=root/'user';home.mkdir(mode=0o700);os.chown(home,owner.pw_uid,owner.pw_gid)
    project=home/'project';project.mkdir(mode=0o700)
    (project/'.git').mkdir(mode=0o700);(project/'.git/config').write_text('metadata-canary')
    (project/'input.txt').write_text('input-canary')
    canary=root/'outside.txt';canary.write_text('host-canary');canary.chmod(0o666)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(4)
    task=BACKGROUND
    if name=='success':
        # Reuse fixed isolation actions, then remove the intentionally created
        # link: unsafe links are never eligible for the output export protocol.
        task="import sys\nsys.argv=['task',"+repr(str(canary))+","+repr(str(listener.getsockname()[1]))+"]\n"+ISOLATION+r'''
Path('outside-link').unlink()
result['input_copy']=Path('input.txt').read_text()=='input-canary'
denied('formal_project_read',lambda:Path(FORMAL_PROJECT+'/input.txt').read_bytes())
denied('formal_project_write',lambda:Path(FORMAL_PROJECT+'/input.txt').write_text('escape'))
Path('result.json').write_text(json.dumps(result))
'''.replace('FORMAL_PROJECT',repr(str(project)))
    if task_source is not None: task=task_source
    (project/'task.py').write_text(task)
    for directory,dirs,files in os.walk(project):
        os.chown(directory,owner.pw_uid,owner.pw_gid)
        for member in files: os.chown(Path(directory)/member,owner.pw_uid,owner.pw_gid)
    st=project.stat();original=(st.st_dev,st.st_ino,st.st_uid,st.st_gid,st.st_mode)
    original_task=(project/'task.py').read_bytes()
    endpoint=broker.provision(project,owner.pw_uid,lifetime=150,resources=resource_limits)
    env={'PATH':'/usr/bin:/bin','HOME':str(home),'TMPDIR':str(home),'PYTHONDONTWRITEBYTECODE':'1',
         'GITGO_STATE_HOME':str(home/'state'),'GITGO_CONFIG_PATH':str(home/'config.json'),
         'GITGO_LLM_CONFIG_PATH':str(home/'llm.json')}
    report=dict(case=name,passed=False,artifacts=str(root),broker=str(endpoint))
    label=None;child=None;lease=None;fault=None;state=None
    try:
        control=socket.create_connection(listener.getsockname(),timeout=1);control.close()
        assert canary.read_text()=='host-canary'
        label=launch_broker(endpoint)
        report['broker_startup_seconds']=wait_broker(endpoint,label)
        # A real root connection has the wrong OS user, even before any JSON.
        wrong=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);wrong.settimeout(5)
        try:
            wrong.connect(str(endpoint/'broker.sock'))
            report['wrong_os_uid_denied']=wire.receive(wrong).get('event')=='denied'
        finally: wrong.close()
        child=subprocess.Popen(child_command(endpoint/'client.json',name,home/'cancel',owner.pw_uid),
            user=owner.pw_uid,group=owner.pw_gid,extra_groups=[],env=env,cwd=home,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def active():
            if child.poll() is not None: raise RuntimeError('Ordinary Host exited before launch')
            try:
                current=json.loads(identity.bounded_read(endpoint/'private/active.json',0,True))
                candidate=Path(current['lease']);s=load_state(candidate)
                return candidate if s['phase'] in ('running','cleanup_pending','clean') else None
            except FileNotFoundError: return None
        if name=='success' and observer is not None:
            lease=wait_for(active,10,'Task was not admitted')
            try:report['operator_observation']=observer(lease)
            except Exception as exc:report['operator_observation_error']=type(exc).__name__+': '+str(exc)
        if name!='success':
            lease=wait_for(active,10,'Task was not admitted')
            s=load_state(lease);work=Path(s['workspace'])
            def birth_count():
                try:
                    raw=identity.bounded_read(work/'births.jsonl',s['uid'])
                    return sum(json.loads(line).get('kind')=='birth' for line in raw.split(b'\n')[:-1])
                except FileNotFoundError: return 0
            wait_for(lambda:birth_count()>=6,5,'Bounded detached producer did not start')
            before=birth_count();time.sleep(.16);after=birth_count()
            report['births_before_fault']=[before,after]
            if after<=before: raise RuntimeError('Producer not active at fault injection')
            fault=broker.raw_time()
            if name=='cancel': (home/'cancel').touch()
            elif name=='owner_disconnect': child.kill()
            elif name=='broker_crash':
                action=launchctl('kill','SIGKILL','system/'+label)
                if action.returncode: raise RuntimeError(action.stderr)
            elif name=='revoked':
                value=json.loads(identity.bounded_read(endpoint/'private/grant.json',0,True))
                value['revoked']=True;identity.atomic_json(endpoint/'private/grant.json',value)
        out,err=child.communicate(timeout=35)
        report['client_exit']=child.returncode;report['client_stderr']=err[-4000:]
        if out.strip(): report['client']=json.loads(out)
        if lease is None:
            record=json.loads(identity.bounded_read(endpoint/'private/active.json',0,True))
            lease=Path(record['lease'])
        state=wait_for(lambda:(s if (s:=load_state(lease))['phase']=='clean' else None),18,'UID not retired')
        report['state']={k:v for k,v in state.items() if k not in ('argv','env','profile')}
        report['remaining']=identity.owned_rows(state['uid'])
        if fault is not None: report['fault_to_observed_clean_seconds']=broker.raw_time()-fault
        good=(state['launch_count']==1 and state.get('cleanup',{}).get('observed_empty') is True
              and not state.get('cleanup',{}).get('issues') and not report['remaining'] and report['wrong_os_uid_denied'])
        if HELPER:
            job=plistlib.loads(identity.bounded_read(lease/'job.plist',0,True))
            report['frozen_supervisor']=job['ProgramArguments']==[str(HELPER),'--gitgo-internal-role','macos-execution-service',str(lease)]
            report['frozen_task_runner']=state['argv']==[str(HELPER),'--gitgo-internal-role','tool-runner']
            good=good and report['frozen_supervisor'] and report['frozen_task_runner']
            if name!='owner_disconnect': good=good and report.get('client',{}).get('runtime',{}).get('frozen') is True
        st=project.stat()
        report['project_metadata_unchanged']=original==(st.st_dev,st.st_ino,st.st_uid,st.st_gid,st.st_mode)
        report['original_files_unchanged']=((project/'input.txt').read_text()=='input-canary' and
            (project/'task.py').read_bytes()==original_task and (project/'.git/config').read_text()=='metadata-canary'
            and canary.read_text()=='host-canary')
        good=good and report['project_metadata_unchanged'] and report['original_files_unchanged']
        if name=='success' and expected_resource_reason:
            result=report.get('client',{}).get('result',{})
            report['resource_termination_confirmed']=state.get('reason')==expected_resource_reason
            good=good and result.get('is_error') is True and report['resource_termination_confirmed']
            good=good and not (project/'result.json').exists() and not (project/'measurements.json').exists()
            good=good and state.get('resource_observation',{}).get('samples',0)>0
        elif name=='success':
            result=report.get('client',{}).get('result',{})
            facts=json.loads((project/'result.json').read_text()) if (project/'result.json').exists() else {}
            report['isolation']=facts
            good=good and not result.get('is_error',True) and bool(facts) and all(v for k,v in facts.items() if k!='uid')
            good=good and facts.get('uid')==state['uid'] and facts.get('uid')!=owner.pw_uid
            receipt=result.get('receipt',{})
            good=good and receipt.get('effect_state')=='committed' and bool(receipt.get('file_service_result'))
            good=good and receipt.get('runtime_cleanup',{}).get('complete') is True
            replay=subprocess.run(child_command(endpoint/'client.json','reject',home/'cancel',owner.pw_uid,state['invocation_id']),
                user=owner.pw_uid,group=owner.pw_gid,extra_groups=[],env=env,cwd=home,capture_output=True,text=True,timeout=30)
            report['rejections']=json.loads(replay.stdout) if replay.stdout.strip() else {}
            report['rejection_stderr']=replay.stderr[-2000:]
            seen=json.loads(identity.bounded_read(endpoint/'private/seen.json',0,True))
            good=good and replay.returncode==0 and report['rejections'].get('passed') is True and seen==[state['invocation_id']]
        else:
            # All fault cases must stop before the 15-second workload expires.
            started=state.get('started',0)
            ended=state.get('finished',0)
            report['start_to_clean_seconds']=ended-started
            good=good and started>0 and 0<ended-started<14
            good=good and not (project/'births.jsonl').exists() and not (project/'detached').exists()
            if name=='owner_disconnect': good=good and child.returncode==-9
            else: good=good and child.returncode==0 and report.get('client',{}).get('result',{}).get('is_error') is True
            if name=='timeout':
                result=report.get('client',{}).get('result',{})
                data=result.get('data') or {}
                runner=result.get('diagnostics',{}).get('runner',{})
                # An arbitrary crash followed by UID cleanup does not prove
                # correct timeout handling by the real command runner.
                report['timeout_classified']=data.get('error') in ('COMMAND_TIMEOUT','TOOL_TIMEOUT')
                good=good and report['timeout_classified'] and 'PermissionError' not in runner.get('stderr','')
            if name=='cancel':
                data=report.get('client',{}).get('result',{}).get('data') or {}
                report['cancellation_classified']=data.get('error')=='TOOL_CANCELLED'
                good=good and report['cancellation_classified']
        if name in ('owner_disconnect','broker_crash'):
            # Restart only the broker. The independent UID supervisor has
            # already retired the original identity. No command is replayed.
            launchctl('bootout','system/'+label)
            label=launch_broker(endpoint)
            report['recovery_broker_startup_seconds']=wait_broker(endpoint,label)
            recovered=subprocess.run(child_command(endpoint/'client.json','recover',home/'cancel',owner.pw_uid,state['invocation_id']),
                user=owner.pw_uid,group=owner.pw_gid,extra_groups=[],env=env,cwd=home,capture_output=True,text=True,timeout=20)
            report['reconciliation']=json.loads(recovered.stdout) if recovered.stdout.strip() else {}
            report['reconciliation_stderr']=recovered.stderr[-4000:]
            seen=json.loads(identity.bounded_read(endpoint/'private/seen.json',0,True))
            unchanged=load_state(lease)
            good=good and recovered.returncode==0 and report['reconciliation'].get('passed') is True
            if HELPER: good=good and report['reconciliation'].get('runtime',{}).get('frozen') is True
            good=good and seen==[state['invocation_id']] and unchanged['launch_count']==1
            good=good and not (project/'births.jsonl').exists() and not identity.owned_rows(state['uid'])
        report['passed']=bool(good)
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
        if label:
            status=launchctl('print','system/'+label)
            report['broker_status_before_unload']=(status.stdout+status.stderr)[-6000:]
    finally:
        if child is not None:
            if child.poll() is None: child.kill()
            try:
                out,err=child.communicate(timeout=5)
                if 'client' not in report and out.strip(): report['client_raw']=out[-8000:]
                if err: report['client_stderr']=err[-4000:]
            except subprocess.TimeoutExpired: report['client_reap_failed']=True
        if label: launchctl('bootout','system/'+label)
        # Broker death closes the root-only lease socket; recovery service must
        # finish its own UID cleanup. Never unload that service while uncertain.
        try:
            if lease is None and (endpoint/'private/active.json').exists():
                lease=Path(json.loads(identity.bounded_read(endpoint/'private/active.json',0,True))['lease'])
            if lease is not None:
                final=wait_for(lambda:(s if (s:=load_state(lease))['phase']=='clean' else None),18,'Recovery remains pending')
                remaining=identity.owned_rows(final['uid'])
                report['final_state']={k:v for k,v in final.items() if k not in ('argv','env','profile')}
                report['final_remaining']=remaining
                if remaining or final.get('cleanup',{}).get('observed_empty') is not True:
                    raise RuntimeError('Retain UID recovery service')
                recovery_label='system/org.gitgo.execution-trial.'+lease.name
                launchctl('bootout',recovery_label)
                report['recovery_job_unloaded']=launchctl('print',recovery_label).returncode!=0
        except Exception as exc:
            report['cleanup_error']=str(exc);report['passed']=False
            if lease is not None:
                report['lease']=str(lease)
                try:
                    snapshot=load_state(lease)
                    report['failed_state']={k:v for k,v in snapshot.items() if k not in ('argv','env','profile')}
                    report['failed_remaining']=identity.owned_rows(snapshot['uid'])
                    report['observed_boot']=identity.boot_id()
                    status=launchctl('print','system/org.gitgo.execution-trial.'+lease.name)
                    report['execution_status']=(status.stdout+status.stderr)[-6000:]
                    report['execution_stderr']=identity.bounded_read(lease/'service.err',0).decode(errors='replace')[-6000:]
                except Exception as detail:
                    report['execution_diagnostic_error']=type(detail).__name__+': '+str(detail)
        if label: report['broker_unloaded']=launchctl('print','system/'+label).returncode!=0
        for name2 in ('last-error.json','service.err','service.out'):
            path=endpoint/'private'/name2
            if path.exists(): report[name2]=identity.bounded_read(path,0).decode(errors='replace')[-4000:]
        listener.close()
        report['passed']=bool(report['passed'] and report.get('broker_unloaded') and report.get('recovery_job_unloaded'))
    return report


def main():
    global HELPER
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--staged',action='store_true')
    parser.add_argument('--client');parser.add_argument('--mode',default='success')
    parser.add_argument('--marker',default='');parser.add_argument('--uid',type=int,default=0)
    parser.add_argument('--replay',default='')
    parser.add_argument('--helper',help='Installed sealed frozen helper, for packaged acceptance')
    args=parser.parse_args()
    if args.helper:
        from backend.core.macos_helper_install import BASE
        HELPER=Path(args.helper).absolute()
        if HELPER.parent.parent.parent!=BASE or HELPER.name!='gitgo-host' or not HELPER.is_file():
            parser.error('Expected the installed sealed helper')
    if args.client:
        client(args.client,args.mode,args.marker,args.uid,args.replay);return
    if not args.run or sys.platform!='darwin' or os.getuid()!=0 or not args.staged:
        parser.error('Use sudo with --run on macOS; ordinary Host runs only in the child')
    uid=int(os.environ.get('SUDO_UID','0'))
    if not 501<=uid<identity.UID_MIN: raise RuntimeError('Run sudo from your ordinary login account')
    owner=pwd.getpwuid(uid)
    report=dict(runtime=str(SOURCE),helper=str(HELPER) if HELPER else None,owner_uid=uid,production_enabled=False,cases=[])
    for name in ('success','cancel','timeout','owner_disconnect','broker_crash','revoked'):
        print('Testing ordinary-user broker: '+name,file=sys.stderr,flush=True)
        result=acceptance_case(name,owner);report['cases'].append(result)
        if not result['passed']: break
    report['passed']=len(report['cases'])==6 and all(c['passed'] for c in report['cases'])
    print(json.dumps(report,indent=2),flush=True)
    if not report['passed']: raise SystemExit(1)


if __name__=='__main__': main()
