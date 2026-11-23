"""Bounded compatibility/resource audit through the existing frozen broker.

No downloads, no real project edits, no capability enablement. Resource gaps
are reported as gaps, even when the acceptance harness/cleanup succeeds.
"""
from __future__ import annotations
import argparse
import json
import os

# Pure validation/report helpers also import on non-POSIX hosts.
if os.name == 'posix':
    import pwd

from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime
if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid()==0:
    stage_runtime(__file__,support=('check_macos_user_broker.py',))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import check_macos_user_broker as harness
from backend.core.macos_helper_install import verify_bundle_path

# Each fixture is offline and has an independent finite workload bound.
CASES={
'shell':r'''
run(['/bin/sh','-c','printf shell-ok | /usr/bin/tr a-z A-Z'], 'SHELL-OK')
''',
'python':r'''
run(['/usr/bin/python3','-I','-c',"import subprocess;print(subprocess.check_output(['/bin/sh','-c','printf python-child-ok']).decode())"], 'python-child-ok')
run(['/usr/bin/python3','-I','-m','venv','--without-pip',str(scratch/'venv')])
run([str(scratch/'venv/bin/python3'),'-I','-c',"print('venv-ok')"], 'venv-ok')
''',
'git':r'''
repo=scratch/'repo'
run(['/usr/bin/git','init','-q',str(repo)])
(repo/'hello.txt').write_text('hello\n')
run(['/usr/bin/git','-C',str(repo),'add','hello.txt'])
run(['/usr/bin/git','-C',str(repo),'-c','user.name=Trial','-c','user.email=trial@example.invalid','-c','commit.gpgsign=false','commit','-qm','trial'])
run(['/usr/bin/git','-C',str(repo),'show','HEAD:hello.txt'],'hello')
''',
'clang_make':r'''
(scratch/'main.c').write_text('#include <stdio.h>\nint main(void){puts("compiled-child-ok");return 0;}\n')
(scratch/'Makefile').write_text('all:\n\t/usr/bin/clang main.c -o hello\n\t./hello\n')
run(['/usr/bin/make','-C',str(scratch)],'compiled-child-ok')
''',
'node_npm':r'''
run(['/opt/homebrew/bin/node','-e',"const cp=require('child_process'); console.log(cp.execFileSync('/bin/sh',['-c','printf node-child-ok']).toString())"],'node-child-ok')
# npm needs node on PATH; this does not broaden the sandbox read allowlist.
os.environ['PATH']='/opt/homebrew/bin:/usr/bin:/bin'
(scratch/'package.json').write_text(json.dumps({'private':True,'scripts':{'test':'node -e "console.log(12345)"'}}))
run(['/opt/homebrew/bin/npm','--offline','--prefix',str(scratch),'run','test'],'12345')
''',
'bun':r'''
run(['/opt/homebrew/bin/bun','-e',"console.log(Bun.spawnSync(['/bin/sh','-c','printf bun-child-ok']).stdout.toString())"],'bun-child-ok')
''',
'resource_file_fd':r'''
import errno,resource,signal
signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
handles=[];fd_error=None
try:
    for _ in range(160):handles.append(os.open('/dev/null',os.O_RDONLY))
except OSError as exc:fd_error=exc.errno
finally:
    for fd in handles:os.close(fd)
file_error=None
try:
    with (scratch/'large').open('wb',buffering=0) as f:
        for _ in range(20): f.write(b'x'*65536)
except OSError as exc:file_error=exc.errno
measure.update(opened_fds=len(handles),fd_errno=fd_error,file_bytes=(scratch/'large').stat().st_size,file_errno=file_error,
    fd_limit_observed=fd_error==errno.EMFILE,file_limit_observed=file_error==errno.EFBIG)
''',
'resource_process':r'''
import errno
children=[];failure=None
try:
    # At most 70 children, each lives <=3 seconds, no recursive fork.
    for _ in range(70):
        try: pid=os.fork()
        except OSError as exc:failure=exc.errno;break
        if pid==0:
            time.sleep(3);os._exit(0)
        children.append(pid)
finally:
    for pid in children:os.waitpid(pid,0)
measure.update(children_created=len(children),fork_errno=failure,configured_process_limit=32,
    configured_process_limit_exceeded=len(children)>32,kernel_fork_refusal_observed=failure==errno.EAGAIN)
''',
'resource_memory':r'''
# Bounded 64 MiB touch, never attempt to exhaust host RAM. Darwin address-space
# rlimit is only recorded, not treated as aggregate physical-memory enforcement.
block=bytearray(64*1024*1024)
for index in range(0,len(block),4096):block[index]=1
measure.update(touched_bytes=len(block),configured_memory_bytes=512*1024*1024,
    aggregate_memory_enforcement_verified=False)
del block
''',
'resource_cpu':r'''
import resource,signal
before=resource.getrusage(resource.RUSAGE_CHILDREN)
code='import time;print("cpu-workload-started",flush=True);end=time.process_time()+10\nwhile time.process_time()<end: pass'
started=time.monotonic()
try:
    child=subprocess.run(['/usr/bin/python3','-I','-c',code],capture_output=True,text=True,timeout=10.5)
    after=resource.getrusage(resource.RUSAGE_CHILDREN)
    consumed=after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime
    measure.update(returncode=child.returncode,cpu_seconds=consumed,elapsed=time.monotonic()-started,
        stdout=child.stdout[-2000:],stderr=child.stderr[-2000:],
        per_process_cpu_limit_observed=child.returncode==-signal.SIGKILL and consumed>=7.5,
        aggregate_cpu_enforcement_verified=False)
    if 'cpu-workload-started' not in child.stdout or child.returncode not in (0,-signal.SIGKILL):
        raise RuntimeError('CPU workload did not execute correctly: '+child.stderr[-1000:])
except subprocess.TimeoutExpired:
    measure.update(cpu_probe_timed_out=True,aggregate_cpu_enforcement_verified=False)
    raise RuntimeError('CPU probe reached wall deadline; CPU limit result inconclusive')
''',
}


def workload(name,*,body=None):
    body=CASES[name] if body is None else body
    return r'''
import json,os,resource,shutil,subprocess,tempfile,time
from pathlib import Path
scratch=Path(tempfile.mkdtemp(prefix='dev-check-',dir='.')).resolve()
measure={'case':CASE,'commands':[],'errors':[],'limits':{
    name:list(resource.getrlimit(getattr(resource,name))) for name in
    ('RLIMIT_CPU','RLIMIT_AS','RLIMIT_RSS','RLIMIT_NPROC','RLIMIT_NOFILE','RLIMIT_FSIZE')}}
def run(argv,expected=None,*,timeout=4):
    item={'argv':argv};measure['commands'].append(item);started=time.monotonic()
    try:
        result=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
        item.update(returncode=result.returncode,stdout=result.stdout[-2000:],stderr=result.stderr[-2000:])
        item['passed']=result.returncode==0 and (expected is None or expected in result.stdout)
    except subprocess.TimeoutExpired as exc:
        def text(value):return value.decode(errors='replace') if isinstance(value,bytes) else (value or '')
        item.update(passed=False,error=type(exc).__name__+': '+str(exc),
                    stdout=text(exc.stdout)[-2000:],stderr=text(exc.stderr)[-2000:])
    except OSError as exc:
        item.update(passed=False,error=type(exc).__name__+': '+str(exc))
    finally:item['elapsed_seconds']=time.monotonic()-started
try:
    exec(BODY)
except Exception as exc:
    measure['errors'].append(type(exc).__name__+': '+str(exc))
finally:
    shutil.rmtree(scratch)
measure['compatible']=not measure['errors'] and bool(measure['commands']) and all(x['passed'] for x in measure['commands'])
Path('measurements.json').write_text(json.dumps(measure))
# This file asserts successful collection only. Compatibility has its own
# result above and is never inferred from a successful tool-runner exit.
Path('result.json').write_text(json.dumps({'uid':os.getuid(),'measurement_complete':not measure['errors']}))
'''.replace('CASE',repr(name)).replace('BODY',repr(body))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--staged',action='store_true')
    parser.add_argument('--helper',required=True)
    parser.add_argument('--case',action='append',choices=tuple(CASES),help='Run only the selected bounded case; repeat for multiple cases')
    args=parser.parse_args()
    uid=int(os.environ.get('SUDO_UID','0'))
    if not args.run or not args.staged or sys.platform!='darwin' or os.getuid()!=0 or not 501<=uid<200000000:
        parser.error('Use sudo --run from the ordinary login account on macOS')
    helper=Path(args.helper).absolute();verify_bundle_path(helper)
    harness.HELPER=helper;owner=pwd.getpwuid(uid)
    selected=list(dict.fromkeys(args.case or CASES))
    report=dict(audit_complete=False,production_enabled=False,helper=str(helper),selected_cases=selected,cases=[],
        resource_contract_satisfied=False,
        resource_gaps=['memory_bytes is not enforced as an aggregate task budget',
                       'process_limit=32 is not forwarded; experimental RLIMIT_NPROC is 64',
                       'CPU rlimit is per-process fixed 8 seconds, not aggregate task CPU'])
    try:
        for name in selected:
            print('Developer/resource audit: '+name,file=sys.stderr,flush=True)
            case=harness.acceptance_case('success',owner,task_source=workload(name))
            case['case']=name
            path=Path(case['artifacts'])/'user/project/measurements.json'
            if path.exists():case['measurements']=json.loads(path.read_text())
            report['cases'].append(case)
            if not case['passed']:break
        report['audit_complete']=len(report['cases'])==len(selected) and all(c['passed'] for c in report['cases'])
        report['compatible_tools']=[c['case'] for c in report['cases'] if c.get('measurements',{}).get('compatible')]
        report['incompatible_tools']=[c['case'] for c in report['cases'] if not c['case'].startswith('resource_') and not c.get('measurements',{}).get('compatible')]
    finally:print(json.dumps(report,indent=2),flush=True)
    if not report['audit_complete']:raise SystemExit(1)


if __name__=='__main__':main()
