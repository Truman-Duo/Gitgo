"""Actual protocol/storage boundaries, not API or independent semantic evidence."""
from copy import deepcopy
from types import SimpleNamespace
import json
import pytest

from tests.test_task_maintenance import runtime, raw, plan, publish
from backend.core.loop.task_maintenance import state_for, observe_result
from backend.core.loop.executor import _build_internal_tools
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.completion_protocol import CompletionClaim, HostCompletionEvaluator
from backend.core.loop.validation_evidence import workspace_version


def tools_for(runtime):
    _, p, _, root = runtime
    return _build_internal_tools(p, {}, str(root), object(), SimpleNamespace(_executors={}))


def test_semantic_plan_is_lowered_once_with_exact_version_and_no_duplicate_graph(runtime):
    _, p, _, root = runtime
    p.runtime_preferences['task_maintenance_enabled'] = True
    tools = tools_for(runtime)
    tool = tools['declare_task_contract']
    schema = tool.to_openai_function()['function']['parameters']
    assert 'mechanism' not in schema['properties'] and 'plan' in schema['properties']
    assert 'mechanism' in tool.parameters['properties']  # saved compatibility calls
    assert 'catalog' not in tools['task_mechanism'].to_openai_function()['function']['parameters']['properties']['operation']['enum']
    args = raw(p)
    args.pop('mechanism')
    update = args.pop('maintenance')
    args.update(plan=update['plan'], related_review=update['related_review'])
    prepared = tool.prepare_args(args)
    assert 'plan' not in prepared and prepared['maintenance']['expected_revision'] == 0
    assert args['plan'] == update['plan']
    accepted = tool.execute(prepared)
    assert accepted['accepted'] and state_for(p)['phase'] == 'ready'
    changed = tool.prepare_args({'operation': 'revise', 'plan': plan(),
        'related_review': {'summary': 'Consumers unchanged; tested scope retained', 'candidates': []}})
    assert changed['maintenance']['expected_revision'] == state_for(p)['revision']
    assert tool.execute(changed)['accepted']
    rejected = tool.execute(changed)
    assert rejected.get('error') == 'TASK_CONTRACT_REVISION_CONFLICT'
    assert p.approval_grants == []
    with pytest.raises(ValueError, match='Conflicting task update'):
        tool.prepare_args({'operation': 'revise', 'plan': plan(), 'maintenance': {'plan': {'summary': 'conflict'}}})
    with pytest.raises(ValueError, match='maintenance must be an object'):
        tool.prepare_args({'operation': 'revise', 'maintenance': []})


@pytest.mark.parametrize('ref', ['sha256:' + 'a'*64, 'context:x/y@sha256:bad', 'context:x/../y@latest'])
def test_bad_reference_stays_argument_diagnostic_without_plan_maintenance(runtime, ref):
    _, p, store, root = runtime
    publish(runtime, raw(p))
    tool = tools_for(runtime)['context_open']
    before = deepcopy(state_for(p))
    ctx = ExecutionContext(p, p.session, str(root), storage=store._storage)
    result = ToolPipeline().execute({'name': tool.name, 'args': {'ref': ref}}, tool, ctx, 'bad-ref', 0)
    assert result.is_error and result.diagnostics['code'] == 'INVALID_TOOL_ARGUMENTS'
    assert result.diagnostics['effect_state'] == 'not_committed'
    observe_result(p, result)
    assert state_for(p) == before and p.approval_grants == []


def test_business_failure_still_requires_maintenance(runtime):
    _, p, _, _ = runtime
    publish(runtime, raw(p))
    result = SimpleNamespace(data={}, is_error=True, tool_name='run_test',
        receipt={'receipt_id': 'real-failed-test'}, diagnostics={'nature': 'business', 'code': 'TEST_FAILED'})
    observe_result(p, result)
    assert state_for(p)['maintenance_items']['failure-real-failed-test']['status'] == 'pending'


def test_query_is_short_paged_and_original_evidence_is_complete(runtime):
    _, p, store, root = runtime
    body = 'first needle ' + 'a' * 8000 + 'second needle ' + 'b' * 9000
    descriptor = store._storage.put_tool_result(body, process_id=p.process_id, task_id=p.active_task_id, tool_name='fixture')
    tool = tools_for(runtime)['tool_result_open']
    first = tool.execute({'locator': descriptor['locator'], 'query': 'needle'})
    assert len(first['content']) <= 2400 and first['truncated']
    second = tool.execute({'locator': descriptor['locator'], 'query': 'needle', 'offset': first['next_offset']})
    assert second['match'] > first['match'] and 'second needle' in second['content']
    assert store._storage.read_tool_result(descriptor['locator'], max_chars=30000)['content'] == body
    complete = tool.execute({'locator': descriptor['locator'], 'query': 'needle', 'max_chars': 30000})
    assert complete['content'] == body


def add_test(p, root, *, name='original', receipt_id='test-receipt', **fields):
    (root / 'subject.py').write_text('original behavior', encoding='utf-8')
    receipt = dict(receipt_id=receipt_id, task_id=p.active_task_id, tool_name='run_test',
        test_id=name, test_target='test_subject.py', succeeded=True, committed=True, test_passed=True,
        validation_version=workspace_version(root, input_files=['subject.py']))
    receipt.update(fields)
    p.tool_receipts.append(receipt)
    return receipt


def test_completion_binds_current_test_without_copying_ids_and_rechecks_mutation(runtime):
    _, p, _, root = runtime
    receipt = add_test(p, root)
    tool = tools_for(runtime)['complete_task']
    args = {'result': 'Implemented', 'verification': [{'kind': 'test', 'detail': 'Observed test result'}]}
    original = deepcopy(args)
    result = tool.execute(args)
    assert result['accepted'] and args == original
    assert p.completion_claim.verification[0]['receipt_id'] == receipt['receipt_id']
    reasons = []
    HostCompletionEvaluator._verify_receipt_references(p, p.completion_claim, reasons)
    assert reasons == []
    (root / 'subject.py').write_text('new untested behavior', encoding='utf-8')
    HostCompletionEvaluator._verify_receipt_references(p, p.completion_claim, reasons)
    assert any('recheck' in reason for reason in reasons)


@pytest.mark.parametrize('problem', ['foreign', 'failed', 'stale', 'ambiguous', 'later_failure', 'explicit_wrong'])
def test_completion_never_invents_or_substitutes_proof(runtime, problem):
    _, p, _, root = runtime
    receipt = add_test(p, root)
    item = {'kind': 'test'}
    if problem == 'foreign': receipt['task_id'] = 'other-task'
    if problem == 'failed': receipt['test_passed'] = False
    if problem == 'stale': (root / 'subject.py').write_text('changed', encoding='utf-8')
    if problem == 'ambiguous': add_test(p, root, name='other-test', receipt_id='other-receipt')
    if problem == 'later_failure': add_test(p, root, receipt_id='new-failure', test_passed=False)
    if problem == 'explicit_wrong': item['receipt_id'] = 'invented'
    claim = CompletionClaim.from_args({'result': 'Candidate', 'verification': [item]}, step=1, process=p)
    assert claim.verification[0].get('receipt_id') == item.get('receipt_id')
    reasons = []
    HostCompletionEvaluator._verify_receipt_references(p, claim, reasons)
    assert reasons


def test_exact_test_selector_resolves_ambiguity(runtime):
    _, p, _, root = runtime
    add_test(p, root)
    add_test(p, root, name='other', receipt_id='other-receipt')
    claim = CompletionClaim.from_args({'result': 'Candidate', 'verification': [{'kind': 'test', 'test_id': 'other'}]}, step=1, process=p)
    assert claim.verification[0]['receipt_id'] == 'other-receipt'


def test_readiness_uses_stage_entry_but_final_delivery_keeps_alignment(runtime):
    from backend.core.mechanism_runtime.application import Application
    from backend.core.loop.task_maintenance import source_receipts
    from backend.core.loop.decision_support import collect_decision_evidence
    from tests.test_mechanism_interactions import enable
    _, p, _, _ = runtime
    app = Application(p)
    p._harness_application = app
    enable(app, p, {'schema_version': 2, 'id': 'completion-note', 'intent': 'Keep completion context',
        'source_refs': list(source_receipts(p)), 'elements': [
            {'id': 'event', 'use': 'runtime.event', 'parameters': {'boundaries': ['completion']}},
            {'id': 'note', 'use': 'context.project', 'parameters': {'content': 'Retain requirements'}}],
        'bindings': [{'kind': 'trigger', 'from': {'node': 'event', 'port': 'go'}, 'to': {'node': 'note', 'port': 'go'}}]})
    p.update_context_snapshot(lambda c: {**c, 'alignment_runtime': {'enforced': True,
        'task_id': p.active_task_id, 'process_id': p.process_id}})
    view = collect_decision_evidence(p, 'completion')
    gates = view['outstanding_gates']
    assert gates['boundary'] == 'registered_completion_stage'
    assert not any(g['code'] == 'ALIGNMENT_REVIEW_REQUIRED' for g in gates['gates'])
    assert any(g['code'] == 'ALIGNMENT_REVIEW_REQUIRED' for g in HostCompletionEvaluator.evaluate(p, 'Candidate').gates)
