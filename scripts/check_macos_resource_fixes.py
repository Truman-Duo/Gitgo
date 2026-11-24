"""Frozen operator acceptance for managed Node and sampled resource guard.

This never certifies a strict aggregate kernel CPU/memory limit.
"""
import argparse
import json
import os

# Pure validation/report helpers also import on non-POSIX hosts.
if os.name == 'posix':
    import pwd

from pathlib import Path
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime
if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid()==0:
    stage_runtime(__file__,support=('check_macos_user_broker.py','check_macos_developer_tools.py','retire_macos_unstarted_trial.py','sample_macos_trial_node.py'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import check_macos_user_broker as harness
from check_macos_developer_tools import workload,CASES
from backend.core.macos_helper_install import install,verify_bundle_path


def stress(kind,count):
    # Bounded even if the guard fails: <=2 workers, <=8 CPU seconds each,
    # <=80 MiB each, <=8 wall seconds for memory holders. No fork loop.
    action="end=time.process_time()+8\nwhile time.process_time()<end: pass" if kind=='cpu' else "blocks=[]\nfor _ in range(10):\n blocks.append(bytearray(8*1024**2));time.sleep(.04)\ntime.sleep(6)"
    return '''
import json,os,time
from pathlib import Path
children=[]
try:
 for _ in range(COUNT):
  pid=os.fork()
  if pid==0:
   os.setsid()
   exec(ACTION)
   os._exit(0)
  children.append(pid)
 Path('uncommitted.txt').write_text('must not publish')
 for pid in children:os.waitpid(pid,0)
finally:
 # If this becomes visible, the intended resource termination failed.
 Path('result.json').write_text(json.dumps({'uid':os.getuid(),'unexpected_completion':True}))
'''.replace('COUNT',str(count)).replace('ACTION',repr(action))


def sample_probe(child):
    # Only this trusted, direct child, whose environment contains no secrets.
    # Do not sample task UIDs or arbitrary processes supplied by a workload.
    if child.poll() is not None:return dict(skipped='probe already exited')
    with tempfile.TemporaryDirectory(prefix='gitgo-probe-sample-') as directory:
        target=Path(directory)/'sample.txt'
        result=subprocess.run(['/usr/bin/sample',str(child.pid),'1','10','-file',str(target)],
            stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=4,
            env={'PATH':'/usr/bin:/bin','HOME':'/','__CF_USER_TEXT_ENCODING':f'0x{os.getuid():X}:0:0'})
        trace=''
        if target.exists():
            with target.open('rb') as stream:trace=stream.read(60000).decode(errors='replace')
        return dict(returncode=result.returncode,stderr=result.stderr[-2000:],trace=trace)


def probe_helper(helper,evidence,*,startup_seconds=40,diagnostic_after=5):
    from backend.core import macos_identity as identity
    from backend.core.macos_project_authority import API_VERSION as authority_version
    from backend.core.macos_resource_guard import API_VERSION as resource_version
    # This is a trusted startup check, separate from all task/resource limits.
    # Do not inherit sudo/shell environment, stdio or a different user's CF UID.
    env={'PATH':'/usr/bin:/bin','HOME':'/','PYTHONDONTWRITEBYTECODE':'1',
         '__CF_USER_TEXT_ENCODING':f'0x{os.getuid():X}:0:0'}
    code='''import json,sys
print('python-entry',file=sys.stderr,flush=True)
from backend.core.macos_resource_guard import API_VERSION
print('resource-import',file=sys.stderr,flush=True)
from backend.core.macos_identity import boot_id
from backend.core.macos_project_authority import API_VERSION as authority
print('authority-import',file=sys.stderr,flush=True)
boot=boot_id()
print('boot-read',file=sys.stderr,flush=True)
print(json.dumps([API_VERSION,authority,boot]),flush=True)
'''
    start=time.monotonic()
    child=subprocess.Popen([str(helper),'--gitgo-internal-role','python','-B','-I','-c',code],
        env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        try:out,err=child.communicate(timeout=min(diagnostic_after,startup_seconds))
        except subprocess.TimeoutExpired:
            evidence['slow_start']=True
            try:evidence['startup_sample']=sample_probe(child)
            except Exception as exc:evidence['sample_error']=type(exc).__name__+': '+str(exc)
            out,err=child.communicate(timeout=max(.01,startup_seconds-(time.monotonic()-start)))
        evidence.update(returncode=child.returncode,stdout=out[-4000:],stderr=err[-4000:])
        if child.returncode or json.loads(out)!=[resource_version,authority_version,identity.boot_id()]:
            raise RuntimeError('Helper lacks the current resource guard / boot identity contract')
        evidence['passed']=True
    except subprocess.TimeoutExpired:
        evidence['timed_out']=True
        raise RuntimeError('Trusted helper startup deadline exceeded; see helper_probe evidence') from None
    finally:
        if child.poll() is None:child.kill()
        out,err=child.communicate(timeout=5)
        evidence.update(returncode=child.returncode,stdout=out[-4000:],stderr=err[-4000:],
                        elapsed_seconds=time.monotonic()-start,reaped=child.poll() is not None)


def run_acceptance(a,uid,report):
    report['phase']='install'
    helper=install(a.bundle,allow_local_adhoc=a.allow_local_adhoc);verify_bundle_path(helper)
    report.update(helper=str(helper),phase='helper_probe',helper_probe={})
    print('Resource fixes acceptance: helper startup probe',file=sys.stderr,flush=True)
    probe_helper(helper,report['helper_probe'])
    harness.HELPER=helper;owner=pwd.getpwuid(uid)
    normal=dict(memory_bytes=512*1024**2,process_limit=32,cpu_seconds=120)
    cpu={**normal,'cpu_seconds':2};memory={**normal,'memory_bytes':160*1024**2}
    body=CASES['node_npm'].replace('/opt/homebrew/bin/node','node').replace('/opt/homebrew/bin/npm','npm')
    body=body.replace("os.environ['PATH']='/opt/homebrew/bin:/usr/bin:/bin'",'pass')
    body="measure['cf_uid_matches']=os.environ.get('__CF_USER_TEXT_ENCODING')==f'0x{os.getuid():X}:0:0'\nassert measure['cf_uid_matches']\nrun(['node','--version'])\n"+body
    body=body.replace("const cp=require('child_process');", "console.log('node-main-started');const cp=require('child_process');")
    managed_node=workload('node_npm',body=body)
    idle="import json,os,time\nfrom pathlib import Path\ntime.sleep(3)\nPath('result.json').write_text(json.dumps({'uid':os.getuid(),'idle_survived':True}))"
    fixtures=[(n,workload(n),normal,None) for n in ('shell','python','git','clang_make','bun','resource_file_fd','resource_process','resource_memory')]
    fixtures += [('node_npm_managed',managed_node,normal,None),('cpu_idle',idle,cpu,None),
        ('cpu_single',stress('cpu',1),cpu,'resource_cpu'),('cpu_children',stress('cpu',2),cpu,'resource_cpu'),
        ('memory_children',stress('memory',2),memory,'resource_memory')]
    report['phase']='cases'
    try:
        if a.retire_unstarted_lease:
            from retire_macos_unstarted_trial import retire
            report['prior_unstarted_retirement']=retire(a.retire_unstarted_lease)
        for name,source,policy,reason in fixtures:
            print('Resource fixes acceptance: '+name,file=sys.stderr,flush=True)
            observer=None
            if name=='node_npm_managed':
                from sample_macos_trial_node import collect
                observer=lambda lease:collect(lease,helper)
            case=harness.acceptance_case('success',owner,task_source=source,resource_limits=policy,expected_resource_reason=reason,observer=observer)
            case['execution_checks_passed']=case['passed']
            case['case']=name;case['requested_resources']=policy
            project=Path(case['artifacts'])/'user/project';path=project/'measurements.json'
            if path.exists():case['measurements']=json.loads(path.read_text())
            if case['passed']:
                m=case.get('measurements',{})
                if name in ('shell','python','git','clang_make','bun','node_npm_managed'):case['passed']=m.get('compatible') is True
                if name=='resource_process':case['passed']=m['limits']['RLIMIT_NPROC']==[32,32] and m['children_created']<32 and m['kernel_fork_refusal_observed']
                if name=='resource_file_fd':case['passed']=m['fd_limit_observed'] and m['file_limit_observed']
                if reason:case['passed']=not (project/'uncommitted.txt').exists() and case['state']['reason']==reason
            report['cases'].append(case)
            # Compatibility failure is not a cleanup failure. Collect independent
            # results only after all original isolation/publication/cleanup checks
            # succeeded. Overall acceptance still requires EVERY case to pass.
            if not can_continue(case):break
        if len(report['cases'])==len(fixtures) and all(can_continue(c) for c in report['cases']):
            for fault in ('cancel','timeout','broker_crash'):
                print('Resource fixes regression: '+fault,file=sys.stderr,flush=True)
                case=harness.acceptance_case(fault,owner)
                report['cases'].append(case)
                if not case['passed']:break
        report['suite_complete']=len(report['cases'])==len(fixtures)+3
        report['passed']=report['suite_complete'] and all(c['passed'] for c in report['cases'])
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
        raise
    finally:
        report['cases_run']=len(report['cases'])
        report['failed_cases']=[c['case'] for c in report['cases'] if not c['passed']]
    if report['passed']:report['phase']='complete'
    elif report.get('suite_complete'):report['phase']='complete_with_failures'


def can_continue(case):
    if case.get('passed') is True:return True
    return (case.get('case')=='node_npm_managed' and case.get('execution_checks_passed') is True
            and case.get('final_state',{}).get('phase')=='clean'
            and case.get('final_state',{}).get('cleanup',{}).get('observed_empty') is True
            and case['final_state']['cleanup'].get('issues')==[]
            and case.get('final_remaining')==[] and case.get('broker_unloaded') is True
            and case.get('recovery_job_unloaded') is True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',action='store_true');p.add_argument('--staged',action='store_true')
    p.add_argument('--bundle',required=True);p.add_argument('--allow-local-adhoc',action='store_true')
    p.add_argument('--retire-unstarted-lease',help='Explicitly retire this never-started legacy temporary lease first')
    a=p.parse_args();uid=int(os.environ.get('SUDO_UID','0'))
    if not a.run or not a.staged or os.getuid()!=0 or not 501<=uid<200000000:p.error('Run with sudo --run from ordinary login account')
    report=dict(passed=False,production_enabled=False,strict_aggregate_verified=False,cases=[],phase='setup')
    try:run_acceptance(a,uid,report)
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
        raise
    finally:print(json.dumps(report,indent=2),flush=True)
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
