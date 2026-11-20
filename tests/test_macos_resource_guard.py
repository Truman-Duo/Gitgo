import os
import subprocess
import sys
from unittest.mock import Mock
import pytest
from backend.core.macos_resource_guard import Guard,validate,observe
from backend.core import macos_identity as identity

POLICY=dict(memory_bytes=64*1024**2,process_limit=8,cpu_seconds=2)

def row(pid,cpu=0,memory=0,start=1,child=0,peak=0):return dict(key=(pid,start),cpu_ns=cpu,child_cpu_ns=child,memory=memory,lifetime_peak_memory=peak)

@pytest.mark.parametrize('change',[{'process_limit':True},{'cpu_seconds':0},{'memory_bytes':-1},{'extra':1}])
def test_malformed_policy_refused(change):
    with pytest.raises(ValueError):validate({**POLICY,**change})

def test_live_and_retired_process_cpu_is_accumulated():
    reader=Mock(side_effect=[[row(1,800_000_000),row(2,700_000_000)],[row(2,900_000_000)],[row(1,500_000_000,start=2)]])
    guard=Guard(200000001,POLICY,reader=reader)
    assert guard.check() is None
    assert guard.check() is None
    assert guard.check()=='resource_cpu'
    assert guard.cpu_ns==2_200_000_000
    assert guard.evidence()['strict_aggregate'] is False

def test_memory_and_process_counts_are_summed():
    guard=Guard(200000001,POLICY,reader=lambda _:[row(1,memory=40*1024**2),row(2,memory=40*1024**2)])
    assert guard.check()=='resource_memory'
    guard=Guard(200000001,POLICY,reader=lambda _:[row(i) for i in range(9)])
    assert guard.check()=='resource_processes'

def test_failed_observation_is_not_zero_usage():
    guard=Guard(200000001,POLICY,reader=Mock(side_effect=PermissionError('denied')))
    with pytest.raises(PermissionError):guard.check()

def test_counter_regression_refused():
    guard=Guard(200000001,POLICY,reader=Mock(side_effect=[[row(1,100)],[row(1,99)]]))
    guard.check()
    with pytest.raises(RuntimeError,match='regressed'):guard.check()

@pytest.mark.skipif(os.name != 'posix', reason='POSIX rlimit contract')
def test_rlimits_receive_policy_not_experimental_defaults(monkeypatch):
    calls=[];monkeypatch.setattr(identity.resource,'setrlimit',lambda key,value:calls.append((key,value)))
    identity.limits(POLICY)
    assert (identity.resource.RLIMIT_NPROC,(8,8)) in calls
    assert (identity.resource.RLIMIT_CPU,(2,2)) in calls
    assert not any(key in (identity.resource.RLIMIT_AS,identity.resource.RLIMIT_RSS) for key,_ in calls)

@pytest.mark.skipif(sys.platform!='darwin',reason='Darwin libproc ABI')
def test_real_child_resource_counters(monkeypatch):
    code='import time;block=bytearray(32*1024**2);end=time.process_time()+.2\nwhile time.process_time()<end:pass\nprint("ready",flush=True);time.sleep(8)'
    child=subprocess.Popen([sys.executable,'-I','-c',code],stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='ready'
        monkeypatch.setattr(identity,'owned_rows',lambda uid:[{'pid':child.pid}])
        values=observe(os.getuid())
        assert len(values)==1 and values[0]['key'][0]==child.pid
        assert values[0]['memory']>=32*1024**2 and 200_000_000<=values[0]['cpu_ns']<2_000_000_000
        assert observe(200000001)==[]
    finally:child.kill();child.wait(timeout=3);child.stdout.close()


def test_unseen_reaped_children_raise_cpu_lower_bound():
    guard=Guard(200000001,POLICY,reader=lambda _:[row(1,100_000_000,child=2_000_000_000)])
    assert guard.check()=='resource_cpu'
    assert guard.evidence()['observed_self_cpu_ns']==100_000_000
    assert guard.cpu_ns==2_100_000_000


def test_reaped_children_are_not_added_twice_or_when_parent_is_reaped():
    samples=[
        [row(1,100_000_000),row(2,400_000_000),row(3,800_000_000)],
        [row(1,100_000_000),row(2,400_000_000,child=800_000_000)],
        [row(1,100_000_000,child=1_200_000_000)],
        [],
    ]
    guard=Guard(200000001,POLICY,reader=Mock(side_effect=samples))
    for _ in samples:
        assert guard.check() is None
        assert guard.cpu_ns==1_300_000_000


def test_independent_subtree_counters_are_not_unsafely_summed():
    # Without retained ancestry these may overlap. Do not manufacture a cap.
    guard=Guard(200000001,POLICY,reader=lambda _:[row(1,100_000_000,child=1_500_000_000),row(2,100_000_000,child=1_400_000_000)])
    assert guard.check() is None and guard.cpu_ns==1_600_000_000
    assert guard.evidence()['strict_aggregate'] is False


def test_child_counter_regression_fails_closed():
    guard=Guard(200000001,POLICY,reader=Mock(side_effect=[[row(1,child=100)],[row(1,child=99)]]))
    guard.check()
    with pytest.raises(RuntimeError,match='regressed'):guard.check()


def test_duplicate_identity_cannot_inflate_cpu_total():
    guard=Guard(200000001,POLICY,reader=lambda _:[row(1),row(1)])
    with pytest.raises(RuntimeError,match='Duplicate'):guard.check()


def test_released_peak_remains_over_budget_after_process_disappears():
    guard=Guard(200000001,POLICY,reader=Mock(side_effect=[
        [row(42,memory=8*1024**2,peak=96*1024**2)],[],[row(42,start=2,peak=1)]]))
    for _ in range(3):
        assert guard.check()=='resource_memory'
        e=guard.evidence()
        assert e['peak_observed_memory']==8*1024**2
        assert e['memory_peak_lower_bound']==96*1024**2
        assert e['memory_limit_basis']=='single_process_lifetime_peak'
        assert e['unobserved_exited_memory_covered'] is False


def test_historical_peaks_are_not_summed_or_added_to_current_memory():
    guard=Guard(200000001,POLICY,reader=Mock(side_effect=[
        [row(1,memory=8*1024**2,peak=48*1024**2),row(2,memory=24*1024**2,peak=48*1024**2)],
        [row(3,memory=16*1024**2,peak=32*1024**2)],[]]))
    for _ in range(3):
        assert guard.check() is None
        assert guard.evidence()['memory_peak_lower_bound']==48*1024**2
    assert guard.evidence()['memory_limit_basis'] is None


@pytest.mark.parametrize('peak',[-1,True,1.5,None])
def test_invalid_peak_is_not_silently_ignored(peak):
    guard=Guard(200000001,POLICY,reader=lambda _:[row(1,peak=peak)])
    with pytest.raises(RuntimeError,match='Invalid'):guard.check()


def test_missing_peak_cannot_fall_back_to_instantaneous_sample():
    value=row(1);del value['lifetime_peak_memory']
    with pytest.raises(KeyError):Guard(200000001,POLICY,reader=lambda _:[value]).check()


@pytest.mark.skipif(sys.platform!='darwin',reason='Darwin lifetime footprint ABI')
def test_real_released_burst_detected_without_sampling_the_allocation(monkeypatch):
    # The first observation happens AFTER unmapping. Child waits on a pipe so
    # this tests retained peak accounting rather than probabilistic sampling.
    code='''import mmap,sys
with mmap.mmap(-1,96*1024**2) as block:
 for offset in range(0,len(block),4096):block[offset]=1
print('released',flush=True)
sys.stdin.readline()
'''
    child=subprocess.Popen([sys.executable,'-I','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        import select
        assert select.select([child.stdout],[],[],10)[0]
        assert child.stdout.readline().strip()=='released'
        monkeypatch.setattr(identity,'owned_rows',lambda uid:[{'pid':child.pid}])
        guard=Guard(os.getuid(),POLICY)
        assert guard.check()=='resource_memory'
        e=guard.evidence()
        assert e['peak_observed_memory']<POLICY['memory_bytes']
        assert e['peak_single_process_memory']>=96*1024**2
        assert e['memory_limit_basis']=='single_process_lifetime_peak'
    finally:
        if child.poll() is None:child.kill()
        child.communicate(timeout=3)


@pytest.mark.skipif(sys.platform!='darwin',reason='Darwin reaped-child resource counters')
def test_real_unobserved_children_are_charged_through_live_parent(monkeypatch):
    code='''import os,resource,time,json,sys
for _ in range(60):
 pid=os.fork()
 if pid==0:
  end=time.process_time()+.02
  while time.process_time()<end:pass
  os._exit(0)
 os.waitpid(pid,0)
r=resource.getrusage(resource.RUSAGE_CHILDREN)
print(json.dumps({'cpu':r.ru_utime+r.ru_stime}),flush=True)
sys.stdin.readline()
'''
    child=subprocess.Popen([sys.executable,'-I','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        import json
        measured=json.loads(child.stdout.readline())['cpu']
        assert measured>=1.2
        monkeypatch.setattr(identity,'owned_rows',lambda uid:[{'pid':child.pid}])
        guard=Guard(os.getuid(),{**POLICY,'cpu_seconds':1})
        assert guard.check()=='resource_cpu'
        evidence=guard.evidence()
        assert evidence['observed_self_cpu_ns']<500_000_000
        assert evidence['reaped_subtree_cpu_lower_bound_ns']>=int(measured*1e9)
        assert evidence['observed_cpu_ns']<int(measured*1e9)+500_000_000
    finally:
        child.communicate('\n',timeout=5)
