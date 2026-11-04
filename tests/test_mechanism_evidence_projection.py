"""Review business evidence without reconstructing internal bookkeeping."""
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys

from tests.test_mechanism_interactions import runtime, enable, checkpoint_program
from backend.core.loop.review_evidence import prepare_review_evidence, inline_review_evidence


def test_business_receipts_survive_large_control_history_and_keep_full_raw_body(runtime):
    app, p, *_ = runtime
    base = {'task_id': app.task, 'succeeded': True, 'committed': True, 'effect': 'process'}
    p.tool_receipts = [{**base, 'receipt_id': f'control-{i}', 'tool_name': 'declare_task_contract'} for i in range(100)]
    actual = {**base, 'receipt_id': 'test-command', 'tool_name': 'exec_command',
        'command_completed': True, 'command_exit_code': 1}
    failure = {**base, 'receipt_id': 'denial', 'tool_name': 'search_text', 'succeeded': False,
        'effect': 'read', 'error_code': 'RESOURCE_SCOPE_APPROVAL_REQUIRED'}
    p.tool_receipts += [actual, failure]
    body = {'argv': ['python', '-m', 'unittest', 'test_subject'], 'cwd': '.',
        'stdout': '', 'stderr': 'FAIL: behavior mismatch', 'exit_code': 1, 'success': False,
        'truncated': False, 'candidate_render': {'retained_control': 'opaque metadata ' * 1000}}
    p.session.append_tool_result('actual execution', tool_name='exec_command', data=body, receipt=actual)
    before = deepcopy(p.tool_receipts)
    result = prepare_review_evidence(p)
    assert p.tool_receipts == before
    assert [r['receipt_id'] for r in result['receipts']] == ['test-command', 'denial']
    visible = result['receipts'][0]['witnessed_results'][0]
    assert visible['data']['stderr'] == body['stderr'] and visible['data']['exit_code'] == 1
    assert 'data_projection' in visible and not visible['data']['truncated']
    stored = p.bound_storage.read_tool_result(result['receipt_source']['locator'],
        json_pointer=result['receipts'][0]['result_json_pointers'][0], max_chars=30000)
    assert json.loads(stored['content'])['data'] == body
    assert result['receipt_source']['total_receipts'] == 102
    assert result['receipt_coverage']['process'] == {'total':101,'inline':1,'complete':False}
    assert result['receipt_coverage']['read']['complete']


def test_current_contract_and_control_are_visible_without_replaying_review_prose(runtime):
    app, p, *_ = runtime
    enable(app, p, checkpoint_program(p))
    p.context_snapshot['task_contract'] = {'revision': 4, 'goal': 'Current semantic goal',
        'acceptance_criteria': ['Never claim a fixture was edited by the Agent'],
        'mechanism': {'compiler_internal': 'not a business requirement'}}
    def record(value):
        value['return_counters'] = {'retry': 2}
        value['reviews'] = {'attempt': {'epoch': value['epoch'], 'phase': 'complete',
            'created_at': 'now', 'candidate_digest': 'candidate',
            'result': {'verdict': 'changes_required', 'summary': 'long historical report ' * 200}}}
    app.repo.mutate(app.task, 'checkpoints', record)
    result = inline_review_evidence(prepare_review_evidence(p))
    assert result['task_basis']['current_contract']['acceptance_criteria'] == ['Never claim a fixture was edited by the Agent']
    assert 'mechanism' not in result['task_basis']['current_contract']
    instance = result['task_control']['instances'][0]
    assert instance['execution']['return_counters'] == {'retry': 2}
    assert instance['execution']['review_history']['available']
    assert 'reviews' not in instance['execution']
    assert 'program' not in instance and instance['configuration']['details_deferred']
    full = json.loads(p.bound_storage.read_tool_result(result['receipt_source']['locator'],
        json_pointer=instance['json_pointer'], max_chars=30000)['content'])
    assert full['execution']['reviews'][0]['summary'].startswith('long historical report')
    assert full['execution']['reviews'][0]['verdict'] == 'changes_required'
    program = json.loads(p.bound_storage.read_tool_result(result['receipt_source']['locator'],
        json_pointer=instance['configuration']['json_pointer'], max_chars=30000)['content'])
    assert program == full['program'] and program['elements']
    assert instance['configuration']['intent'] == program['intent']


def test_oversize_execution_body_is_deferred_not_misrepresented_as_complete(runtime):
    app, p, *_ = runtime
    receipt = {'task_id': app.task, 'receipt_id': 'large-output', 'tool_name': 'run_test',
        'succeeded': True, 'test_completed': True, 'test_passed': False}
    p.tool_receipts = [receipt]
    p.session.append_tool_result('test output', tool_name='run_test', receipt=receipt,
        data={'passed': False, 'seed_results': [{'output_tail': 'failing assertion\n' * 500}]})
    result = prepare_review_evidence(p)
    assert 'witnessed_results' not in result['receipts'][0]
    assert result['receipts'][0]['test_passed'] is False
    assert result['receipts'][0]['result_json_pointers']


def test_source_reads_preserve_conflicting_versions_and_exact_origin(runtime):
    app,p,*_=runtime
    for n,sha in enumerate(('before','after','before')):
        receipt={'task_id':app.task,'receipt_id':str(n),'tool_name':'read_file','effect':'read','succeeded':True}
        p.tool_receipts.append(receipt)
        p.session.append_tool_result('read',tool_name='read_file',receipt=receipt,
            data={'path':'untouched.py','sha256':sha,'size':8,'content':'the witnessed body'})
    result=prepare_review_evidence(p)
    reads=result['source_reads']
    assert not reads['partial'] and reads['total']==3
    assert [item['sha256'] for item in reads['items']]==['before','after','before']
    assert [item['observed_order'] for item in reads['items']]==[0,1,2]
    assert reads['items'][0]['witnessed_content']=='the witnessed body'
    assert 'witnessed_content' not in reads['items'][2]  # identical old page once
    for item in reads['items']:
        full=json.loads(p.bound_storage.read_tool_result(result['receipt_source']['locator'],json_pointer=item['json_pointer'])['content'])
        assert item['sha256']==full['data']['sha256']


def test_business_coverage_does_not_require_rereading_runtime_control_or_hide_unknown_effects():
    from backend.core.loop.review_packet import operation_coverage
    controls=[{'receipt_id':str(i),'effect':'process','execution_contract':
        {'kind':'data_broker','broker':'host.runtime.some_control'}} for i in range(100)]
    work={'receipt_id':'write','effect':'workspace_write','files':['app.py']}
    check={'receipt_id':'test','effect':'process','test_completed':True,'test_passed':False}
    read={'receipt_id':'read','effect':'read'}
    coverage=operation_coverage([*controls,work,check,read],[work,check])
    assert coverage['business']=={'total':2,'inline':2,'complete':True,'missing':[],'missing_index_partial':False}
    assert coverage['runtime_control_count']==100 and coverage['observation_count']==1
    unknown={'receipt_id':'unknown','tool_name':'future_tool'}
    coverage=operation_coverage([*controls,work,check,unknown],[work,check])
    assert not coverage['business']['complete'] and coverage['business']['missing'][0]['json_pointer']=='/receipts/102'
    inconsistent={**controls[0],'command_completed':True}
    assert not operation_coverage([inconsistent],[])['business']['complete']


def test_source_read_content_stays_bounded_and_never_claims_truncated_page_is_whole():
    from backend.core.loop.review_evidence import read_observations
    values=[{'receipt_id':str(i),'tool_name':'read_file','is_error':False,
        'data':{'path':'large.py','sha256':str(i),'content':'x'*1500,'offset':i*100,
            'returned_lines':100,'truncated':True}} for i in range(20)]
    result=read_observations(values)
    assert sum(len(i.get('witnessed_content','')) for i in result['items'])<=6000
    assert all(i['truncated'] for i in result['items'])


def test_native_runner_preserves_scope_denial_instead_of_claiming_tool_crash(tmp_path_factory):
    tmp_path = tmp_path_factory
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (tmp_path / 'private.txt').write_text('must not leak', encoding='utf-8')
    request = {'tool_name': 'search_text', 'args': {'_workspace': str(workspace), 'path': '..', 'pattern': 'private'}}
    child = subprocess.run([sys.executable, '-m', 'backend.core.tools.runner'],
        cwd=Path(__file__).resolve().parents[1], input=json.dumps(request),
        capture_output=True, text=True, timeout=30)
    result = json.loads(child.stdout)
    assert child.returncode == 0 and not child.stderr
    assert result['success'] is True
    assert result['data']['error'] == 'RESOURCE_SCOPE_APPROVAL_REQUIRED'
    assert result['data']['execution_state'] == 'not_started'
    assert 'must not leak' not in child.stdout


def test_unrelated_runner_failure_is_not_reclassified_as_scope_denial(monkeypatch):
    from backend.core.tools import runner
    from backend.core.loop.execution_contract import NATIVE_PROCESS
    def fail(args):
        raise PermissionError('unrelated operating system failure')
    monkeypatch.setattr(runner, '_auto_import_registrations', lambda: None)
    monkeypatch.setitem(runner._TOOL_REGISTRY, 'fixture', runner.HandlerBinding(fail, NATIVE_PROCESS))
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"tool_name":"fixture","args":{}}'))
    monkeypatch.setattr(sys, 'stdout', out)
    monkeypatch.setattr(sys, 'stderr', err)
    runner.main()
    assert json.loads(out.getvalue())['success'] is False
    assert 'unrelated operating system failure' in err.getvalue()
