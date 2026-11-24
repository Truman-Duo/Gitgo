"""Operator-only bounded Node stability, two-UID isolation and resource audit.

No VM, downloads, cache eviction, production enablement or real project edits.
Fresh service/UID is not a cold OS cache; --after-reboot-report proves a new boot.
"""
from __future__ import annotations
import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import os

# Pure validation/report helpers also import on non-POSIX hosts.
if os.name == 'posix':
    import pwd

from pathlib import Path
import stat
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime
if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid()==0:
    stage_runtime(__file__,support=('check_macos_user_broker.py','check_macos_developer_tools.py','check_macos_resource_fixes.py','macos_resource_boundary_cases.py'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import check_macos_user_broker as harness
from check_macos_developer_tools import workload
from check_macos_resource_fixes import probe_helper
from macos_resource_boundary_cases import run_boundaries
from backend.core import macos_identity as identity
from backend.core.sandbox_macos import load_state
from backend.core.macos_helper_install import install,verify_bundle_path

NORMAL=dict(memory_bytes=512*1024**2,process_limit=32,cpu_seconds=120)


def node_source():
    return workload('node_stability',body=r'''
assert os.environ.get('__CF_USER_TEXT_ENCODING')==f'0x{os.getuid():X}:0:0'
(scratch/'package.json').write_text(json.dumps({'private':True,'scripts':{'test':'node -e "console.log(12345)"'}}))
# Six fresh processes per task; no root sampling and no retries on a failure.
measure['first_node_startup_budget_seconds']=10
for index in range(2):
 run(['node','--version'],'v',timeout=10 if index==0 else 4)
 if not measure['commands'][-1]['passed']:raise RuntimeError('Node startup failed; no retry')
 run(['node','-e',"console.log('node-main-started');console.log(require('child_process').execFileSync('/bin/sh',['-c','printf node-child-ok']).toString())"],'node-child-ok')
 if not measure['commands'][-1]['passed']:raise RuntimeError('Node child execution failed; no retry')
 run(['npm','--offline','--prefix',str(scratch),'run','test'],'12345')
 if not measure['commands'][-1]['passed']:raise RuntimeError('npm execution failed; no retry')
''')


def churn_source(count=60):
    if type(count) is not int or not 1<=count<=120:raise ValueError('Bounded child count required')
    # <=120 sequential children, <=.015 CPU seconds each plus startup. Memory
    # touches <=8 MiB at a time, no recursive/unbounded fork or host exhaustion.
    return workload('short_children',body=r'''
import resource
before=resource.getrusage(resource.RUSAGE_CHILDREN)
started=time.monotonic();count=0
for _ in range(CHILD_COUNT):
 if time.monotonic()-started>4:break
 pid=os.fork()
 if pid==0:
  block=bytearray(8*1024**2);end=time.process_time()+.015
  while time.process_time()<end:pass
  os._exit(0)
 _,status=os.waitpid(pid,0)
 if status:raise RuntimeError('Child exited abnormally')
 count+=1
usage=resource.getrusage(resource.RUSAGE_CHILDREN)
measure.update(children_reaped=count,waited_child_cpu_seconds=usage.ru_utime+usage.ru_stime-before.ru_utime-before.ru_stime,
               workload_wall_seconds=time.monotonic()-started)
assert count>0
'''.replace('CHILD_COUNT',str(count)))


def pair_source(role):
    return r'''
import errno,json,os,signal,time
from pathlib import Path
role=ROLE
signal.signal(signal.SIGUSR1,signal.SIG_IGN)
Path('canary.txt').write_text('peer-canary-'+role)
Path('ready.json').write_text(json.dumps({'pid':os.getpid(),'uid':os.getuid()}))
def read_when(path,seconds):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  try:return json.loads(Path(path).read_text())
  except (FileNotFoundError,json.JSONDecodeError):time.sleep(.02)
 raise RuntimeError('Coordinator deadline: '+path)
peer=read_when('peer.json',7)
facts={}
def deny(key,call):
 try:call()
 except OSError as exc:facts[key]=exc.errno in (errno.EACCES,errno.EPERM)
 else:facts[key]=False
canary=Path(peer['workspace'])/'canary.txt'
deny('peer_read_denied',lambda:canary.read_bytes())
deny('peer_write_denied',lambda:canary.write_text('escaped'))
deny('peer_signal_denied',lambda:os.kill(peer['pid'],signal.SIGUSR1))
facts['distinct_uid']=peer['uid']!=os.getuid()
Path('checks.json').write_text(json.dumps(facts))
read_when('go.json',7)
if role=='limited':
 Path('uncommitted.txt').write_text('must not publish')
 pid=os.fork()
 if pid==0:
  os.setsid();end=time.process_time()+6
  while time.process_time()<end:pass
  os._exit(0)
 os.waitpid(pid,0)
 raise RuntimeError('Expected CPU termination did not occur')
else:
 # Must still be alive AFTER the other identity was cleaned. Root observes
 # heartbeat progress before giving permission to finish.
 end=time.monotonic()+8;sequence=0
 while time.monotonic()<end and not Path('finish.json').exists():
  sequence+=1;Path('heartbeat.json').write_text(json.dumps({'sequence':sequence}));time.sleep(.04)
 if not Path('finish.json').exists():raise RuntimeError('Peer cleanup not confirmed')
 facts['own_canary_intact']=Path('canary.txt').read_text()=='peer-canary-'+role
 facts['survived_peer_cleanup']=True;facts['uid']=os.getuid()
 Path('result.json').write_text(json.dumps(facts))
'''.replace('ROLE',repr(role))


def write_control(work,uid,name,value):
    # Pin the known task directory; exclusive creation never follows a task
    # symlink or truncates an existing file. Parent is controlled by root.
    directory=os.open(work,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(directory)
        if info.st_uid!=uid or not stat.S_ISDIR(info.st_mode):raise RuntimeError('Workspace identity changed')
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        try:
            os.fchown(fd,uid,uid)
            raw=json.dumps(value).encode()
            if os.write(fd,raw)!=len(raw):raise RuntimeError('Short control write')
        finally:os.close(fd)
    finally:os.close(directory)


def read_evidence(work,uid,name,seconds=6):
    def read():
        try:return json.loads(identity.bounded_read(work/name,uid))
        except (FileNotFoundError,json.JSONDecodeError):return None
    return harness.wait_for(read,seconds,'Missing trial evidence: '+name)


class Pair:
    def __init__(self):
        self.lock=threading.Lock();self.ready=threading.Barrier(2,timeout=9)
        self.checked=threading.Barrier(2,timeout=9);self.members={};self.evidence={}

    def observe(self,role,lease):
        try:
            state=load_state(lease);uid=state['uid'];work=Path(state['workspace'])
            ready=read_evidence(work,uid,'ready.json')
            if ready['uid']!=uid or not any(r['pid']==ready['pid'] for r in identity.owned_rows(uid)):
                raise RuntimeError('Task readiness does not match live lease identity')
            with self.lock:self.members[role]=dict(uid=uid,workspace=str(work),pid=ready['pid'],lease=str(lease))
            self.ready.wait()
            peer=self.members['survivor' if role=='limited' else 'limited']
            if peer['uid']==uid:raise RuntimeError('Concurrent UID collision')
            write_control(work,uid,'peer.json',{k:peer[k] for k in ('uid','workspace','pid')})
            facts=read_evidence(work,uid,'checks.json')
            if set(facts)!={'peer_read_denied','peer_write_denied','peer_signal_denied','distinct_uid'} or not all(v is True for v in facts.values()):
                raise RuntimeError('Cross-task isolation failed: '+repr(facts))
            if identity.bounded_read(work/'canary.txt',uid)!=('peer-canary-'+role).encode():
                raise RuntimeError('Peer changed canary')
            with self.lock:self.evidence[role]=facts
            self.checked.wait()
            write_control(work,uid,'go.json',{})
            if role=='survivor':
                other=Path(peer['lease'])
                end_state=harness.wait_for(lambda:(s if (s:=load_state(other))['phase']=='clean' else None),7,'Limited task did not clean')
                if end_state.get('reason')!='resource_cpu' or identity.owned_rows(peer['uid']):raise RuntimeError('Peer termination not confirmed')
                before=read_evidence(work,uid,'heartbeat.json')['sequence'];time.sleep(.2)
                after=read_evidence(work,uid,'heartbeat.json')['sequence']
                if after<=before:raise RuntimeError('Survivor stopped during peer cleanup')
                self.evidence['survivor_heartbeat_after_cleanup']=[before,after]
                write_control(work,uid,'finish.json',{})
            return dict(passed=True,role=role)
        except BaseException:
            self.ready.abort();self.checked.abort();raise


def concurrent_case(owner):
    pair=Pair()
    def run(role):
        return harness.acceptance_case('success',owner,task_source=pair_source(role),
            resource_limits={**NORMAL,'cpu_seconds':2} if role=='limited' else NORMAL,
            expected_resource_reason='resource_cpu' if role=='limited' else None,
            observer=lambda lease:pair.observe(role,lease))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures={r:pool.submit(run,r) for r in ('limited','survivor')}
        cases={r:f.result() for r,f in futures.items()}
    good=all(c['passed'] and c.get('operator_observation',{}).get('passed') for c in cases.values())
    good=good and 'survivor_heartbeat_after_cleanup' in pair.evidence
    return dict(passed=bool(good),cases=cases,evidence=pair.evidence)


def measurement(case):
    path=Path(case['artifacts'])/'user/project/measurements.json'
    return json.loads(path.read_text()) if path.exists() else {}


def verify_reboot_reference(path,boot,helper):
    raw=Path(path).read_bytes();previous=json.loads(raw)
    if (previous.get('bounded_suite_passed') is not True or previous.get('helper_probe',{}).get('passed') is not True
        or not identity.valid_boot_id(previous.get('boot')) or not identity.valid_boot_id(boot)
        or previous['boot']==boot or previous.get('helper')!=str(helper)):
        raise RuntimeError('Expected a successful reference from another boot with the same sealed helper')
    return dict(previous_boot=previous['boot'],reference_sha256=hashlib.sha256(raw).hexdigest())


def verify_coalition_results(report):
    cases=report['node_rounds']+list(report['concurrent']['cases'].values())
    cases += [report['short_children'],report['short_cpu_limit']]
    cases += report.get('resource_boundaries',{}).get('cases',[])
    ids=[]
    for case in cases:
        obs=case.get('final_state',{}).get('resource_observation',{});coal=obs.get('coalition') or {}
        cid=coal.get('id')
        if (obs.get('cpu_accounting')!='launchd_resource_coalition' or type(cid) is not int or cid<=1
            or coal.get('includes_retired_tasks') is not True or coal.get('includes_supervisor') is not True
            or coal.get('cpu_ns')!=obs.get('observed_cpu_ns')):
            return dict(passed=False,error='Missing authoritative CPU ledger evidence')
        ids.append(cid)
    return dict(passed=len(ids)==len(set(ids)),task_count=len(ids),distinct_coalitions=len(set(ids)))


def run(args,owner,report):
    helper=install(args.bundle,allow_local_adhoc=args.allow_local_adhoc);verify_bundle_path(helper)
    harness.HELPER=helper;report['helper']=str(helper)
    report['boot']=identity.boot_id();report['helper_probe']={};probe_helper(helper,report['helper_probe'])
    report['reboot_verified']=False
    if args.after_reboot_report:
        report['reboot_reference']=verify_reboot_reference(args.after_reboot_report,report['boot'],helper)
        report['reboot_verified']=True
    # First Node runs before other workload warmup. Fresh UID/service each time.
    for index in range(args.rounds):
        print('Stability: fresh-service Node round '+str(index+1),file=sys.stderr,flush=True)
        case=harness.acceptance_case('success',owner,task_source=node_source(),resource_limits=NORMAL)
        case['measurements']=measurement(case)
        case['passed']=bool(case['passed'] and case['measurements'].get('compatible') and len(case['measurements'].get('commands',[]))==6)
        report['node_rounds'].append(case)
        if not case['passed']:return
    print('Concurrency: two live identities, cross-access denial, selective CPU cleanup',file=sys.stderr,flush=True)
    report['concurrent']=concurrent_case(owner)
    if not report['concurrent']['passed']:return
    print('Resources: bounded short-lived children accounting audit',file=sys.stderr,flush=True)
    case=harness.acceptance_case('success',owner,task_source=churn_source(),resource_limits=NORMAL)
    case['measurements']=measurement(case);report['short_children']=case
    m=case['measurements'];actual=m.get('waited_child_cpu_seconds',0)
    observed=case.get('state',{}).get('resource_observation',{}).get('observed_cpu_ns',0)/1e9
    case['audit_valid']=bool(case['passed'] and m.get('children_reaped',0)>0 and actual>0)
    # Observer total includes parents; a lower total proves a gap, a higher
    # total does NOT establish complete child accounting.
    case['proven_minimum_unobserved_cpu_seconds']=max(0,actual-observed)
    case['complete_accounting_verified']=False
    case['fixture_cpu_lower_bound_covered']=case['audit_valid'] and observed>=actual
    if not case['passed']:return
    print('Resources: short-lived child CPU budget termination',file=sys.stderr,flush=True)
    limited=harness.acceptance_case('success',owner,task_source=churn_source(120),
        resource_limits={**NORMAL,'cpu_seconds':1},expected_resource_reason='resource_cpu')
    report['short_cpu_limit']=limited
    report['bounded_suite_passed']=bool(case['fixture_cpu_lower_bound_covered'] and limited['passed'])
    report['ready_for_production']=False
    if report['bounded_suite_passed'] and getattr(args,'resource_boundaries',False):
        report['resource_boundaries']={}
        run_boundaries(owner,harness,NORMAL,report['resource_boundaries'])
        # Completion of a diagnostic does not certify coverage or enforcement.
        report['resource_fixture_coverage_passed']=report['resource_boundaries'].get('sampled_fixture_lower_bounds_covered',False)
        report['bounded_suite_passed']=bool(report['resource_boundaries']['execution_and_cleanup_passed']
                                           and report['resource_fixture_coverage_passed'])
    if getattr(args,'require_coalition',False):
        report['coalition_verification']=verify_coalition_results(report)
        report['bounded_suite_passed']=bool(report['bounded_suite_passed'] and report['coalition_verification']['passed'])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',action='store_true');p.add_argument('--staged',action='store_true')
    p.add_argument('--bundle',required=True);p.add_argument('--allow-local-adhoc',action='store_true')
    p.add_argument('--rounds',type=int,default=6);p.add_argument('--after-reboot-report')
    p.add_argument('--resource-boundaries',action='store_true',help='Add bounded resource enforcement and sampling-gap audits')
    p.add_argument('--require-coalition',action='store_true',help='Require distinct kernel CPU ledgers for every invocation')
    a=p.parse_args();uid=int(os.environ.get('SUDO_UID','0'))
    if not a.run or not a.staged or os.getuid()!=0 or not 501<=uid<200000000 or not 2<=a.rounds<=12:p.error('Use sudo --run; rounds must be 2..12')
    report=dict(bounded_suite_passed=False,ready_for_production=False,strict_aggregate_verified=False,node_rounds=[])
    try:run(a,pwd.getpwuid(uid),report)
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc);raise
    finally:print(json.dumps(report,indent=2),flush=True)
    if not report['bounded_suite_passed']:raise SystemExit(1)


if __name__=='__main__':main()
