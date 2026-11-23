"""Isolate invocation CPU ledgers; lost accounting must never become zero CPU."""
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
from backend.core import macos_coalition as coal,macos_resource_guard as resource


def snapshot(cpu,started=3,exited=2):return dict(cpu_ticks=cpu,started=started,exited=exited)


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(coal,'os',SimpleNamespace(getpid=lambda:42,getppid=lambda:1))
    monkeypatch.setattr(resource,'timebase',lambda:(125,3))
    return lambda pid:7 if pid==42 else 1


def test_exited_orphans_remain_charged_without_pid_samples(isolated):
    ledger=coal.CoalitionCPU(ids=isolated,reader=Mock(side_effect=[snapshot(30),snapshot(36_000_030,27,26)]))
    guard=resource.Guard(200000001,dict(cpu_seconds=1,memory_bytes=64*1024**2,process_limit=8),reader=lambda _:[],cpu_ledger=ledger)
    assert guard.check()=='resource_cpu'
    e=guard.evidence()
    assert e['observed_cpu_ns']==1_500_000_000 and e['tracked_identities']==0
    assert e['coalition']['tasks_started']==24 and e['coalition']['tasks_exited']==24
    assert e['coalition']['includes_supervisor'] and not e['strict_aggregate']


def test_startup_cpu_is_excluded_and_live_sample_not_added_twice(isolated):
    ledger=coal.CoalitionCPU(ids=isolated,reader=Mock(side_effect=[snapshot(24_000_000),snapshot(36_000_000,4,2)]))
    guard=resource.Guard(200000001,dict(cpu_seconds=1,memory_bytes=64*1024**2,process_limit=8),
        reader=lambda _:[dict(key=(100,1),cpu_ns=400_000_000,child_cpu_ns=0,memory=0,lifetime_peak_memory=0,coalition_id=7)],cpu_ledger=ledger)
    assert guard.check() is None and guard.cpu_ns==500_000_000


def test_shared_launchd_coalition_refused(isolated):
    with pytest.raises(RuntimeError,match='isolated'):
        coal.CoalitionCPU(ids=lambda _:1,reader=Mock())


def test_multiple_live_tasks_before_launch_refused(isolated):
    with pytest.raises(RuntimeError,match='other live tasks'):
        coal.CoalitionCPU(ids=isolated,reader=lambda _:snapshot(30,4,2))


def test_non_launchd_parent_refused(isolated,monkeypatch):
    monkeypatch.setattr(coal.os,'getppid',lambda:22)
    with pytest.raises(RuntimeError,match='isolated'):
        coal.CoalitionCPU(ids=isolated,reader=Mock())


def test_cross_coalition_task_refused(isolated):
    ledger=coal.CoalitionCPU(ids=isolated,reader=lambda _:snapshot(30))
    with pytest.raises(RuntimeError,match='escaped'):ledger.read([dict(coalition_id=8)])


def test_supervisor_coalition_change_refused(isolated):
    ids=Mock(side_effect=[7,1,9]);ledger=coal.CoalitionCPU(ids=ids,reader=lambda _:snapshot(30))
    with pytest.raises(RuntimeError,match='changed'):ledger.read([])


@pytest.mark.parametrize('current',[snapshot(29),snapshot(40,2,1),snapshot(40,3,1),snapshot(40,2,3),snapshot(-1),snapshot(True)])
def test_invalid_or_regressing_counters_refused(isolated,current):
    ledger=coal.CoalitionCPU(ids=isolated,reader=Mock(side_effect=[snapshot(30),current]))
    with pytest.raises(RuntimeError):ledger.read([])


def test_ledger_read_error_never_falls_back_to_sampled_cpu(isolated):
    ledger=coal.CoalitionCPU(ids=isolated,reader=Mock(side_effect=[snapshot(30),PermissionError('denied')]))
    guard=resource.Guard(200000001,dict(cpu_seconds=1,memory_bytes=64*1024**2,process_limit=8),reader=lambda _:[],cpu_ledger=ledger)
    with pytest.raises(PermissionError):guard.check()
