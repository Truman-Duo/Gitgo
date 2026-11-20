"""Boundary diagnostics must not silently certify hard resource caps."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from scripts import macos_resource_boundary_cases as boundary
from scripts import check_macos_stability_concurrency as stability

OLD='1712b6c7-f85c-42cc-9780-851da992c246'
NEW='7712b6c7-f85c-42cc-9780-851da992c246'


@pytest.mark.parametrize('change,boot',[
    ({},OLD),({'helper':'/another/helper'},NEW),({'bounded_suite_passed':False},NEW),
    ({'helper_probe':{'passed':False}},NEW),({'boot':'invalid'},NEW),({},'invalid'),
])
def test_reboot_reference_rejects_wrong_boot_package_or_failed_suite(tmp_path_factory,change,boot):
    reference=tmp_path_factory/'prior.json'
    helper=Path('/sealed/helper')
    reference.write_text(json.dumps(dict(boot=OLD,helper=str(helper),bounded_suite_passed=True,helper_probe={'passed':True})|change))
    with pytest.raises(RuntimeError,match='successful reference'):
        stability.verify_reboot_reference(reference,boot,helper)


def test_reboot_reference_records_exact_evidence(tmp_path_factory):
    import hashlib
    reference=tmp_path_factory/'prior.json'
    helper=Path('/sealed/helper')
    reference.write_text(json.dumps(dict(boot=OLD,helper=str(helper),bounded_suite_passed=True,helper_probe={'passed':True})))
    actual=stability.verify_reboot_reference(reference,NEW,helper)
    assert actual==dict(previous_boot=OLD,reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest())


@pytest.mark.parametrize('name,source,policy,reason',boundary.fixtures(stability.NORMAL))
def test_generated_boundary_sources_compile(name,source,policy,reason):
    compile(source,name,'exec')


def cpu_case(observed):
    return dict(passed=True,measurements=dict(errors=[],workers=[dict(reparented=True,cpu_seconds=.125)]*12,
        orphan_cpu_seconds=1.5),state={'resource_observation':{'observed_cpu_ns':observed}})


def test_orphan_cpu_gap_is_reported_despite_successful_execution():
    case=cpu_case(500_000_000)
    assert boundary.assess('orphan_cpu_audit',case)
    assert case['proven_minimum_unobserved_cpu_seconds']==1
    assert not case['known_lower_bound_covered'] and not case['strict_aggregate_verified']


def test_covered_cpu_fixture_is_not_strict_aggregate_proof():
    case=cpu_case(2_000_000_000)
    assert boundary.assess('orphan_cpu_audit',case) and case['known_lower_bound_covered']
    assert not case['strict_aggregate_verified']


@pytest.mark.parametrize('kind',['not_orphan','incomplete','failed','invalid_cpu'])
def test_invalid_orphan_measurement_does_not_pass(kind):
    case=cpu_case(2_000_000_000)
    if kind=='not_orphan':case['measurements']['workers']=[dict(reparented=False,cpu_seconds=.125)]*12
    if kind=='incomplete':case['measurements']['workers']=[]
    if kind=='failed':case['passed']=False
    if kind=='invalid_cpu':case['measurements']['orphan_cpu_seconds']=float('nan')
    assert not boundary.assess('orphan_cpu_audit',case)


def test_memory_burst_gap_does_not_claim_enforcement():
    case=dict(passed=True,measurements=dict(errors=[],bursts=12,burst_seconds=[.01]*12,touched_bytes=192*1024**2),
        state={'resource_observation':{'peak_observed_memory':100*1024**2}})
    assert boundary.assess('memory_burst_audit',case)
    assert case['proven_minimum_unobserved_memory_bytes']==92*1024**2
    assert not case['known_lower_bound_covered'] and not case['enforcement_test']
    assert not case['strict_aggregate_verified']


def test_sustained_memory_requires_actual_resource_termination():
    case=dict(passed=True,resource_termination_confirmed=True,state=dict(reason='task_exit',resource_observation={'peak_observed_memory':200*1024**2}))
    assert not boundary.assess('memory_sustained',case)
    case['state']['reason']='resource_memory'
    assert boundary.assess('memory_sustained',case)


def test_cleanup_failure_stops_boundary_suite(tmp_path_factory):
    call=Mock(return_value=dict(passed=False,artifacts=str(tmp_path_factory)))
    report={};boundary.run_boundaries(None,SimpleNamespace(acceptance_case=call),stability.NORMAL,report)
    assert call.call_count==1
    assert not report['completed'] and not report['execution_and_cleanup_passed']


def test_valid_diagnostics_with_gap_do_not_claim_fixture_coverage(monkeypatch,tmp_path_factory):
    monkeypatch.setattr(boundary,'fixtures',lambda _:[('orphan_cpu_audit','source',{},None)])
    case=cpu_case(500_000_000);case['artifacts']=str(tmp_path_factory)
    call=Mock(return_value=case);report={}
    boundary.run_boundaries(None,SimpleNamespace(acceptance_case=call),stability.NORMAL,report)
    assert report['execution_and_cleanup_passed'] and report['completed']
    assert report['coverage_gaps']==['orphan_cpu_audit']
    assert not report['sampled_fixture_lower_bounds_covered'] and not report['strict_aggregate_verified']


def test_boundary_gap_makes_overall_suite_fail(monkeypatch):
    monkeypatch.setattr(stability,'install',lambda *a,**k:Path('/sealed/helper'))
    monkeypatch.setattr(stability,'verify_bundle_path',lambda _:None)
    monkeypatch.setattr(stability.identity,'boot_id',lambda:OLD)
    monkeypatch.setattr(stability,'probe_helper',lambda *a:None)
    monkeypatch.setattr(stability,'concurrent_case',lambda _:dict(passed=True))
    monkeypatch.setattr(stability.harness,'acceptance_case',lambda *a,**k:dict(passed=True,state={'resource_observation':{'observed_cpu_ns':2_000_000_000}}))
    values=iter([dict(compatible=True,commands=[{}]*6),dict(children_reaped=60,waited_child_cpu_seconds=.9)])
    monkeypatch.setattr(stability,'measurement',lambda _:next(values))
    monkeypatch.setattr(stability,'run_boundaries',lambda owner,harness,normal,report:report.update(
        execution_and_cleanup_passed=True,sampled_fixture_lower_bounds_covered=False))
    report=dict(bounded_suite_passed=False,node_rounds=[])
    stability.run(SimpleNamespace(bundle='bundle',allow_local_adhoc=True,after_reboot_report=None,rounds=1,resource_boundaries=True),None,report)
    assert not report['bounded_suite_passed'] and not report['resource_fixture_coverage_passed']
    assert not report['ready_for_production']


def ledger_case(cid):
    return {'final_state':{'resource_observation':dict(cpu_accounting='launchd_resource_coalition',observed_cpu_ns=1,
        coalition=dict(id=cid,cpu_ns=1,includes_retired_tasks=True,includes_supervisor=True))}}


@pytest.mark.parametrize('bad',['shared','missing','counter_mismatch'])
def test_coalition_acceptance_rejects_shared_or_inconsistent_ledgers(bad):
    report=dict(node_rounds=[ledger_case(2)],concurrent={'cases':{'limited':ledger_case(3),'survivor':ledger_case(4)}},
                short_children=ledger_case(5),short_cpu_limit=ledger_case(6))
    if bad=='shared':report['concurrent']['cases']['survivor']=ledger_case(3)
    if bad=='missing':report['short_cpu_limit']={}
    if bad=='counter_mismatch':report['short_cpu_limit']['final_state']['resource_observation']['coalition']['cpu_ns']=2
    assert not stability.verify_coalition_results(report)['passed']


def test_coalition_acceptance_covers_boundary_invocations():
    report=dict(node_rounds=[ledger_case(2)],concurrent={'cases':{'limited':ledger_case(3),'survivor':ledger_case(4)}},
                short_children=ledger_case(5),short_cpu_limit=ledger_case(6),resource_boundaries={'cases':[ledger_case(7)]})
    assert stability.verify_coalition_results(report)==dict(passed=True,task_count=6,distinct_coalitions=6)


def test_orphan_limit_cannot_pass_using_legacy_sampled_cpu():
    case=dict(passed=True,resource_termination_confirmed=True,state=dict(reason='resource_cpu',resource_observation={
        'observed_cpu_ns':1_100_000_000,'cpu_accounting':'max_self_sum_and_reaped_subtree'}))
    assert not boundary.assess('orphan_cpu_limit',case)
    case['state']['resource_observation']['cpu_accounting']='launchd_resource_coalition'
    assert boundary.assess('orphan_cpu_limit',case)


@pytest.mark.parametrize('bad',['old_helper','no_peak','no_kill','no_cleanup'])
def test_released_memory_limit_requires_peak_and_real_termination(bad):
    obs=dict(memory_accounting='sampled_live_total_and_single_process_lifetime_peak',
             peak_single_process_memory=200*1024**2,memory_limit_basis='single_process_lifetime_peak')
    case=dict(passed=True,resource_termination_confirmed=True,state=dict(reason='resource_memory',resource_observation=obs))
    assert boundary.assess('memory_released_peak_limit',case)
    if bad=='old_helper':del obs['memory_accounting']
    if bad=='no_peak':obs['peak_single_process_memory']=0
    if bad=='no_kill':case['state']['reason']='task_exit'
    if bad=='no_cleanup':case['passed']=False
    assert not boundary.assess('memory_released_peak_limit',case)


def test_released_memory_audit_requires_retained_peak_not_just_live_total():
    obs=dict(memory_accounting='sampled_live_total_and_single_process_lifetime_peak',
             peak_single_process_memory=200*1024**2,memory_limit_basis=None,peak_observed_memory=300*1024**2)
    case=dict(passed=True,measurements=dict(errors=[],touched_bytes=192*1024**2,released=True,burst_seconds=.02),
              state={'resource_observation':obs})
    assert boundary.assess('memory_released_peak_audit',case)
    del obs['peak_single_process_memory']
    assert not boundary.assess('memory_released_peak_audit',case)
