"""Bounded fixtures and honest coverage assessment for the operator harness.

Diagnostic samples use a generous policy to retain evidence even when the
observer misses a burst. They never certify enforcement of a lower budget.
"""
import json
import math
from pathlib import Path

try:
    from .check_macos_developer_tools import workload
    from .check_macos_resource_fixes import stress
except ImportError:
    from check_macos_developer_tools import workload
    from check_macos_resource_fixes import stress


def orphan_cpu_source():
    # Twelve workers total, one at a time, <=.12 CPU seconds / 2 wall seconds
    # each. No recursive fork loop or root execution of these fixtures.
    return workload('orphan_cpu_audit',body=r'''
records=[]
for index in range(12):
 path=scratch/str(index)
 bridge=os.fork()
 if bridge==0:
  middle=os.getpid();worker=os.fork()
  if worker:os._exit(0)
  try:
   os.setsid();end=time.monotonic()+1
   while os.getppid()==middle and time.monotonic()<end:time.sleep(.001)
   reparented=os.getppid()!=middle
   before=time.process_time();wall=time.monotonic()
   while time.process_time()-before<.12 and time.monotonic()-wall<2:pass
   path.write_text(json.dumps({'reparented':reparented,'cpu_seconds':time.process_time()-before}))
   os._exit(0)
  except BaseException:os._exit(3)
 _,status=os.waitpid(bridge,0)
 if status:raise RuntimeError('Bridge failed')
 end=time.monotonic()+3
 while True:
  try:record=json.loads(path.read_text());break
  except (FileNotFoundError,json.JSONDecodeError):
   if time.monotonic()>=end:raise RuntimeError('Orphan evidence deadline')
   time.sleep(.005)
 records.append(record)
measure.update(workers=records,orphan_cpu_seconds=sum(r['cpu_seconds'] for r in records))
time.sleep(.15)
''')


def memory_burst_source():
    # One 192 MiB mapping at a time, twelve bursts, released before the next.
    # Direct page writes establish a touched-byte lower bound independently
    # of the sampling guard. A 512 MiB policy preserves diagnostic output.
    return workload('memory_burst_audit',body=r'''
import mmap
durations=[];size=192*1024**2
for _ in range(12):
 started=time.monotonic()
 with mmap.mmap(-1,size) as block:
  for index in range(0,size,4096):block[index]=1
  time.sleep(.002)
 durations.append(time.monotonic()-started)
 time.sleep(.013)
measure.update(bursts=len(durations),touched_bytes=size,burst_seconds=durations,
               enforcement_test=False)
''')


def memory_released_source():
    # One brief 192 MiB allocation, then one second alive with it unmapped.
    # The real supervisor runs normally: it may catch the live allocation or
    # the retained peak. A separate deterministic libproc test starts observing
    # only after release, so catching this allocation is not called proof of
    # detection after release.
    return workload('memory_released_peak',body=r'''
import mmap
size=192*1024**2;started=time.monotonic()
with mmap.mmap(-1,size) as block:
 for index in range(0,size,4096):block[index]=1
elapsed=time.monotonic()-started
measure.update(touched_bytes=size,released=True,burst_seconds=elapsed)
time.sleep(1)
''')


def process_source():
    return workload('process_refusal',body=r'''
import errno
children=[];failure=None
try:
 for _ in range(12):
  try:pid=os.fork()
  except OSError as exc:failure=exc.errno;break
  if pid==0:time.sleep(2);os._exit(0)
  children.append(pid)
finally:
 for pid in children:os.waitpid(pid,0)
measure.update(children_created=len(children),kernel_fork_refusal_observed=failure==errno.EAGAIN)
''')


def fixtures(normal):
    return [
        ('memory_below_budget',workload('resource_memory'),normal,None),
        ('memory_sustained',stress('memory',2),{**normal,'memory_bytes':160*1024**2},'resource_memory'),
        ('process_refusal',process_source(),{**normal,'process_limit':8},None),
        ('file_fd_refusal',workload('resource_file_fd'),normal,None),
        ('orphan_cpu_audit',orphan_cpu_source(),normal,None),
        ('orphan_cpu_limit',orphan_cpu_source(),{**normal,'cpu_seconds':1},'resource_cpu'),
        ('memory_burst_audit',memory_burst_source(),normal,None),
        ('memory_released_peak_audit',memory_released_source(),normal,None),
        ('memory_released_peak_limit',memory_released_source(),{**normal,'memory_bytes':160*1024**2},'resource_memory'),
    ]


def positive(value):
    return type(value) in (int,float) and math.isfinite(value) and value>0


def assess(name,case):
    """Execution success is separate from diagnostic coverage / strict caps."""
    m=case.get('measurements',{});state=case.get('state',{})
    obs=state.get('resource_observation',{})
    good=case.get('passed') is True
    if name=='orphan_cpu_limit':
        return bool(good and case.get('resource_termination_confirmed') is True
                    and state.get('reason')=='resource_cpu'
                    and obs.get('observed_cpu_ns',0)>=1_000_000_000
                    and obs.get('cpu_accounting')=='launchd_resource_coalition')
    if name=='memory_sustained':
        return bool(good and case.get('resource_termination_confirmed') is True
                    and state.get('reason')=='resource_memory'
                    and obs.get('peak_observed_memory',0)>160*1024**2)
    if name=='memory_released_peak_limit':
        # Require the new counter; an old helper's sampled-only kill is not
        # acceptance of the peak-reading implementation. Early sampling can
        # legitimately kill during page touching, before a 192 MiB peak exists.
        peak=obs.get('peak_single_process_memory');basis=obs.get('memory_limit_basis')
        exceeded=(basis=='single_process_lifetime_peak' and type(peak) is int and peak>160*1024**2
                  or basis=='sampled_live_total' and obs.get('peak_observed_memory',0)>160*1024**2)
        return bool(good and case.get('resource_termination_confirmed') is True
                    and state.get('reason')=='resource_memory'
                    and obs.get('memory_accounting')=='sampled_live_total_and_single_process_lifetime_peak'
                    and type(peak) is int and peak>0 and exceeded)
    good=good and m.get('errors')==[]
    if name=='memory_below_budget':return bool(good and m.get('touched_bytes')==64*1024**2)
    if name=='memory_released_peak_audit':
        return bool(good and m.get('touched_bytes')==192*1024**2 and m.get('released') is True
                    and positive(m.get('burst_seconds'))
                    and obs.get('memory_accounting')=='sampled_live_total_and_single_process_lifetime_peak'
                    and obs.get('peak_single_process_memory',0)>=192*1024**2
                    and obs.get('memory_limit_basis') is None)
    if name=='process_refusal':
        count=m.get('children_created')
        return bool(good and type(count) is int and 0<count<8
                    and m.get('kernel_fork_refusal_observed') is True
                    and m.get('limits',{}).get('RLIMIT_NPROC')==[8,8])
    if name=='file_fd_refusal':
        return bool(good and m.get('fd_limit_observed') is True and m.get('file_limit_observed') is True
                    and m.get('limits',{}).get('RLIMIT_NOFILE')==[128,128]
                    and m.get('limits',{}).get('RLIMIT_FSIZE')==[1048576,1048576])
    if name=='orphan_cpu_audit':
        workers=m.get('workers',[]);actual=m.get('orphan_cpu_seconds');observed=obs.get('observed_cpu_ns')
        valid=bool(good and len(workers)==12 and positive(actual) and type(observed) is int and observed>=0
            and all(r.get('reparented') is True and positive(r.get('cpu_seconds')) and r['cpu_seconds']>=.12 for r in workers)
            and abs(actual-sum(r['cpu_seconds'] for r in workers))<1e-6)
        case['audit_valid']=valid;case['strict_aggregate_verified']=False
        if valid:
            case['proven_minimum_unobserved_cpu_seconds']=max(0,actual-observed/1e9)
            case['known_lower_bound_covered']=observed/1e9>=actual
        return valid
    if name=='memory_burst_audit':
        durations=m.get('burst_seconds',[]);observed=obs.get('peak_observed_memory')
        valid=bool(good and m.get('bursts')==12 and len(durations)==12 and all(positive(t) for t in durations)
                   and m.get('touched_bytes')==192*1024**2 and type(observed) is int and observed>=0)
        case['audit_valid']=valid;case['strict_aggregate_verified']=False;case['enforcement_test']=False
        if valid:
            case['known_lower_bound_covered']=observed>=m['touched_bytes']
            case['proven_minimum_unobserved_memory_bytes']=max(0,m['touched_bytes']-observed)
        return valid
    raise ValueError('Unknown boundary fixture: '+name)


def run_boundaries(owner,harness,normal,report):
    import sys
    report.update(completed=False,execution_and_cleanup_passed=False,
                  strict_aggregate_verified=False,cases=[],coverage_gaps=[])
    plans=fixtures(normal)
    for name,source,policy,reason in plans:
        print('Resource boundary: '+name,file=sys.stderr,flush=True)
        case=harness.acceptance_case('success',owner,task_source=source,
                                    resource_limits=policy,expected_resource_reason=reason)
        case['case']=name;case['execution_checks_passed']=case['passed'];case['requested_resources']=policy
        report['cases'].append(case)
        path=Path(case['artifacts'])/'user/project/measurements.json'
        if path.exists():case['measurements']=json.loads(path.read_text())
        case['passed']=assess(name,case)
        if case.get('audit_valid') and not case.get('known_lower_bound_covered'):
            report['coverage_gaps'].append(name)
        if not case['passed']:return
    report['completed']=len(report['cases'])==len(plans)
    report['execution_and_cleanup_passed']=report['completed']
    report['sampled_fixture_lower_bounds_covered']=report['completed'] and not report['coverage_gaps']
