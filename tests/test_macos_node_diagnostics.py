import copy
import itertools
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from scripts import sample_macos_trial_node as sample
from scripts.check_macos_resource_fixes import can_continue


def clean_node_failure():
    return dict(case='node_npm_managed',passed=False,execution_checks_passed=True,
        final_state=dict(phase='clean',cleanup=dict(observed_empty=True,issues=[])),
        final_remaining=[],broker_unloaded=True,recovery_job_unloaded=True)


def test_node_failure_can_collect_other_results_but_remains_failed():
    case=clean_node_failure();before=copy.deepcopy(case)
    assert can_continue(case) and case==before and case['passed'] is False


@pytest.mark.parametrize('fault',['other_case','execution','pending','unobserved','issues','remaining','broker','supervisor'])
def test_independent_collection_stops_on_uncertain_safety(fault):
    case=clean_node_failure()
    if fault=='other_case':case['case']='memory_children'
    if fault=='execution':case['execution_checks_passed']=False
    if fault=='pending':case['final_state']['phase']='cleanup_pending'
    if fault=='unobserved':case['final_state']['cleanup']['observed_empty']=False
    if fault=='issues':case['final_state']['cleanup']['issues']=['failed']
    if fault=='remaining':case['final_remaining']=[{'pid':12}]
    if fault=='broker':case['broker_unloaded']=False
    if fault=='supervisor':case['recovery_job_unloaded']=False
    assert not can_continue(case)


@pytest.fixture
def sampling(monkeypatch,tmp_path_factory):
    monkeypatch.setattr(sample, 'os', SimpleNamespace(**{**vars(sample.os), 'getuid':lambda:0, 'geteuid':lambda:0}))
    monkeypatch.setattr(sample,'load_state',lambda p:dict(uid=200000001))
    monkeypatch.setattr(sample.identity,'owned_rows',lambda uid:[{'pid':777}])
    clock=itertools.count();monkeypatch.setattr(sample.time,'monotonic',lambda:next(clock))
    monkeypatch.setattr(sample.time,'sleep',lambda t:None)
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv);Path(argv[-1]).write_text('private stack evidence')
        return SimpleNamespace(returncode=0,stderr='')
    monkeypatch.setattr(sample.subprocess,'run',run)
    return calls


def test_stack_is_retained_only_for_unchanged_exact_identity(sampling,monkeypatch,tmp_path_factory):
    key=(777,123,456);snapshot=Mock(return_value=key);monkeypatch.setattr(sample,'snapshot',snapshot)
    result=sample.collect(tmp_path_factory,Path('/sealed/runtime/gitgo-host'))
    assert result['collected'] and result['trace']=='private stack evidence'
    assert len(sampling)==1 and snapshot.call_count==3
    assert snapshot.call_args.args==(777,200000001,Path('/sealed/runtime/developer-tools/bin/node'))


def test_changed_pid_discards_sample(sampling,monkeypatch,tmp_path_factory):
    key=(777,123,456);monkeypatch.setattr(sample,'snapshot',Mock(side_effect=[key,key,None]))
    result=sample.collect(tmp_path_factory,Path('/sealed/runtime/gitgo-host'))
    assert not result['collected'] and 'trace' not in result


def test_unmatched_process_is_never_sampled(sampling,monkeypatch,tmp_path_factory):
    monkeypatch.setattr(sample,'snapshot',lambda *a:None)
    assert not sample.collect(tmp_path_factory,Path('/sealed/runtime/gitgo-host'))['collected']
    assert not sampling
