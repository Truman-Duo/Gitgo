"""Acceptance must distinguish compatibility, cleanup and accounting coverage."""
import sys
import pytest

if sys.platform == 'win32':
    pytest.skip('POSIX identity and file operations', allow_module_level=True)

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from scripts import check_macos_stability_concurrency as check


def test_exclusive_control_write_cannot_follow_task_symlink(monkeypatch,tmp_path_factory):
    outside=tmp_path_factory/'outside';outside.write_text('keep')
    (tmp_path_factory/'peer.json').symlink_to(outside)
    with pytest.raises(FileExistsError):check.write_control(tmp_path_factory,os.getuid(),'peer.json',{'x':1})
    assert outside.read_text()=='keep'


def test_control_write_uses_new_owned_file(monkeypatch,tmp_path_factory):
    owner=Mock();monkeypatch.setattr(check.os,'fchown',owner)
    check.write_control(tmp_path_factory,os.getuid(),'peer.json',{'uid':123})
    assert json.loads((tmp_path_factory/'peer.json').read_text())=={'uid':123}
    assert owner.call_args.args[1:]==(os.getuid(),os.getuid())
    with pytest.raises(FileExistsError):check.write_control(tmp_path_factory,os.getuid(),'peer.json',{})


def test_wrong_workspace_owner_is_rejected(tmp_path_factory):
    with pytest.raises(RuntimeError,match='identity changed'):check.write_control(tmp_path_factory,200000001,'peer.json',{})
    assert not (tmp_path_factory/'peer.json').exists()


@pytest.mark.parametrize('source',[check.node_source(),check.churn_source(),check.pair_source('limited'),check.pair_source('survivor')])
def test_generated_workloads_compile(source):compile(source,'bounded-trial','exec')


def test_node_compatibility_failure_stops_without_retry(monkeypatch):
    monkeypatch.setattr(check,'install',lambda *a,**k:Path('/sealed/gitgo-host'))
    monkeypatch.setattr(check,'verify_bundle_path',lambda _:None)
    monkeypatch.setattr(check.identity,'boot_id',lambda:'1712b6c7-f85c-42cc-9780-851da992c246')
    monkeypatch.setattr(check,'probe_helper',lambda *a:None)
    acceptance=Mock(return_value={'passed':True});monkeypatch.setattr(check.harness,'acceptance_case',acceptance)
    monkeypatch.setattr(check,'measurement',lambda _:dict(compatible=False,commands=[dict(passed=False)]))
    concurrent=Mock();monkeypatch.setattr(check,'concurrent_case',concurrent)
    report={'bounded_suite_passed':False,'node_rounds':[]}
    check.run(SimpleNamespace(bundle='bundle',allow_local_adhoc=True,after_reboot_report=None,rounds=6),None,report)
    assert acceptance.call_count==1 and not report['node_rounds'][0]['passed'] and not report['bounded_suite_passed']
    concurrent.assert_not_called()


def test_cpu_undercount_does_not_become_strict_resource_success(monkeypatch):
    monkeypatch.setattr(check,'install',lambda *a,**k:Path('/sealed/gitgo-host'))
    monkeypatch.setattr(check,'verify_bundle_path',lambda _:None)
    monkeypatch.setattr(check.identity,'boot_id',lambda:'1712b6c7-f85c-42cc-9780-851da992c246')
    monkeypatch.setattr(check,'probe_helper',lambda *a:None)
    monkeypatch.setattr(check,'concurrent_case',lambda _:dict(passed=True))
    monkeypatch.setattr(check.harness,'acceptance_case',lambda *a,**k:dict(passed=True,state={'resource_observation':{'observed_cpu_ns':100000000}}))
    values=iter([{'compatible':True,'commands':[{}]*6},{'children_reaped':60,'waited_child_cpu_seconds':.9}])
    monkeypatch.setattr(check,'measurement',lambda _:next(values))
    report={'bounded_suite_passed':False,'node_rounds':[]}
    check.run(SimpleNamespace(bundle='bundle',allow_local_adhoc=True,after_reboot_report=None,rounds=1),None,report)
    assert not report['bounded_suite_passed'] and not report['ready_for_production']
    assert report['short_children']['proven_minimum_unobserved_cpu_seconds']==pytest.approx(.8)
    assert not report['short_children']['complete_accounting_verified']


def test_first_node_start_budget_is_bounded_and_failure_never_retries(monkeypatch,tmp_path_factory):
    calls=[]
    def timeout(argv,**kwargs):
        calls.append((argv,kwargs['timeout']))
        raise check.harness.subprocess.TimeoutExpired(argv,kwargs['timeout'])
    monkeypatch.setattr(check.harness.subprocess,'run',timeout)
    monkeypatch.setenv('__CF_USER_TEXT_ENCODING',f'0x{os.getuid():X}:0:0')
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path_factory)
        exec(check.node_source(),{})
    m=json.loads((tmp_path_factory/'measurements.json').read_text())
    assert calls==[(['node','--version'],10)]
    assert not m['compatible'] and 'no retry' in m['errors'][0]


def test_only_first_node_version_gets_startup_budget(monkeypatch,tmp_path_factory):
    calls=[]
    def success(argv,**kwargs):
        calls.append((argv[0:2],kwargs['timeout']))
        return SimpleNamespace(returncode=0,stdout='v24 node-child-ok 12345',stderr='')
    monkeypatch.setattr(check.harness.subprocess,'run',success)
    monkeypatch.setenv('__CF_USER_TEXT_ENCODING',f'0x{os.getuid():X}:0:0')
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path_factory)
        exec(check.node_source(),{})
    m=json.loads((tmp_path_factory/'measurements.json').read_text())
    assert m['compatible'] and [t for _,t in calls]==[10,4,4,4,4,4]
