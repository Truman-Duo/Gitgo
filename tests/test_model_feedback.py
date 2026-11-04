"""Model presentation must not remove authority, obligations or failure evidence."""
from copy import deepcopy
import json
import pytest

from tests.test_mechanism_interactions import runtime, enable, checkpoint_program
from backend.core.loop.model_feedback import contract_feedback, task_feedback, task_view, harness_feedback, instance_view
from backend.core.loop.tool_pipeline import ToolPipeline, _maybe_truncate


def test_assessor_view_keeps_unique_semantics_and_failures_without_mutating_authority():
    from backend.core.loop.model_feedback import alignment_task_view
    program = {'intent': 'Keep an actual semantic condition', 'elements': [{'condition': 'Only public interface changes'}]}
    original = {'contract': {'goal': 'Preserve the API', 'mechanism': program,
        'maintenance_intent': {'plan': 'An earlier distinct proposal'},
        'activation': {'phase': 'effective', 'revision': 4, 'contract_digest': 'hash'}},
        'state': {'definition': deepcopy(program), 'plan': {'summary': 'Current actual plan'},
            'changes': {'old': {'status': 'adopted', 'content': 'Preserve the added test'}},
            'source_receipts': {'user:a': {'content': 'Exact answer'}},
            'dependency_versions': {'source.py': 'actual input digest'},
            'manifest': {'phase': 'ready', 'contract_revision': 4, 'digest': 'compiled hash'},
            'contract_basis': 'compiled hash'},
        'extensions': [{'completion': {'reviews': [{'verdict': 'changes_required', 'findings': ['External condition still false']}]}}],
        'consumer_projection': {'prompt_digest': 'acknowledged hash'}, 'future_semantic_field': {'keep': True}}
    before = deepcopy(original)
    result = alignment_task_view(original)
    assert original == before
    assert result['contract']['goal'] == original['contract']['goal']
    assert 'mechanism' not in result['contract'] and result['state']['definition'] == program
    assert result['contract']['maintenance_intent'] == original['contract']['maintenance_intent']
    for field in ('plan', 'changes', 'source_receipts', 'dependency_versions'):
        assert result['state'][field] == original['state'][field]
    assert result['extensions'] == original['extensions']
    assert result['future_semantic_field'] == original['future_semantic_field']
    assert result['contract']['activation'] == {'phase': 'effective', 'revision': 4}
    assert result['state']['manifest'] == {'phase': 'ready', 'contract_revision': 4}
    assert 'consumer_projection' not in result
    different = deepcopy(original)
    different['contract']['mechanism']['intent'] = 'A conflicting definition must remain visible'
    assert alignment_task_view(different)['contract']['mechanism'] == different['contract']['mechanism']


def test_rework_feedback_keeps_findings_and_retains_scoped_full_evidence(runtime):
    from backend.core.loop.model_feedback import rework_feedback
    _, p, _, _, ctx, *_ = runtime
    assert p.bound_storage is ctx.storage
    directive = {'kind': 'rework', 'instructions': [{'instruction': 'Fix only the interface', 'epoch': 3,
        'return_count': 2, 'evidence': {'verdict': 'changes_required', 'summary': 'External prerequisite remains unmet',
            'findings': [{'location': 'api.py:3', 'impact': 'Caller breaks', 'evidence': 'exact failing signature'}],
            'scope_limit': 'Do not change the caller', 'authority_granted': False,
            'evidence': [{'detail': 'retained source body ' * 100}], 'read_only_tools': ['read_file']}}]}
    original = deepcopy(directive)
    view = rework_feedback(directive, p)
    assert directive == original and len(json.dumps(view)) < 1500
    item = view['instructions'][0]
    assert item['return_count'] == 2 and item['epoch'] == 3
    for key in ('findings', 'summary', 'verdict', 'scope_limit', 'authority_granted'):
        assert item['evidence'][key] == original['instructions'][0]['evidence'][key]
    restored = ctx.storage.read_tool_result(view['details']['locator'], owner_process_id=p.process_id)
    assert json.loads(restored['content']) == original
    with pytest.raises(KeyError):
        ctx.storage.read_tool_result(view['details']['locator'], owner_process_id='unrelated')


def test_rework_feedback_storage_failure_returns_full_rejection_without_replaying():
    from types import SimpleNamespace
    from backend.core.loop.model_feedback import rework_feedback
    class Broken:
        def put_tool_result(self, *args, **kwargs): raise OSError('fixture unavailable')
    original = {'kind': 'rework', 'instructions': [{'evidence': {'evidence': ['retained fact'], 'findings': ['must repair']}}]}
    view = rework_feedback(original, SimpleNamespace(bound_storage=Broken(), process_id='p', active_task_id='t'))
    assert view['instructions'] == original['instructions']
    assert view['model_presentation_notice']['original_result_retained']
    assert rework_feedback(original, SimpleNamespace()) is original


def contract_result():
    return {'accepted': True, 'authority_granted': False, 'host_validated': True,
        'contract': {'revision': 3, 'goal': 'Do not change the public interface',
            'execution_mode': 'self_execute', 'acceptance_criteria': ['original test must pass'],
            'deliverables': [{'path': 'module.py', 'required': True}], 'uncertainties': ['external readiness'],
            'mechanism': {'compiler_bookkeeping': 'x' * 20000}},
        'activation': {'phase': 'active', 'journal': 'y' * 10000},
        'routing_advice': {'next_action': 'continue_current_task'}}


def test_pipeline_ack_retains_full_result_and_scoped_evidence(runtime):
    _, p, _, tool, ctx, *_ = runtime
    original = contract_result()
    tool.execute = lambda args: deepcopy(original)
    tool._bound_executor = tool.execute
    tool.model_result = contract_feedback
    result = ToolPipeline().execute({'name': 'unit', 'args': {'value': 'test'}}, tool, ctx, 'feedback', 0)
    assert not result.is_error, result.formatted
    assert result.data == original
    inline = json.loads(result.formatted.split('\n', 1)[1])
    assert inline['contract_revision'] == 3 and inline['authority_granted'] is False
    assert len(result.formatted) < 1500
    full = ctx.storage.read_tool_result(inline['details']['locator'], owner_process_id=p.process_id,
        json_pointer='/contract', max_chars=40000)
    assert json.loads(full['content']) == original['contract']
    with pytest.raises((PermissionError, ValueError, KeyError)):
        ctx.storage.read_tool_result(inline['details']['locator'], owner_process_id='another-process')
    assert result.receipt['tool_result_locator'] == inline['details']['locator']


def test_no_storage_never_discards_full_result():
    original = contract_result()
    original['contract']['mechanism'] = {'small': 'kept'}
    original['activation'].pop('journal')
    text, *_ = _maybe_truncate('declare_task_contract', original, '', model_result=contract_feedback)
    assert 'Do not change the public interface' in text and 'kept' in text


def test_large_actionable_view_keeps_output_bound_and_full_business_obligations(runtime):
    _, p, _, _, ctx, *_ = runtime
    original = {'contract': {'acceptance_criteria': ['required detail ' * 4000]}, 'state': {}}
    text, truncated, _, spill = _maybe_truncate('task_mechanism', original, '',
        storage=ctx.storage, process_id=p.process_id, task_id=p.active_task_id, model_result=task_feedback)
    assert truncated and len(text) < 4000
    notice = json.loads(text.split('[TOOL_RESULT_SPILL]\n', 1)[1])
    assert notice['model_view_partial'] and notice['locator'] == spill['locator']
    parts, offset = [], 0
    while offset is not None:
        page = ctx.storage.read_tool_result(spill['locator'], owner_process_id=p.process_id,
            json_pointer='/contract', offset=offset, max_chars=24000)
        parts.append(page['content'])
        offset = page['next_offset']
    assert len(parts) > 1
    assert json.loads(''.join(parts)) == original['contract']


def test_presentation_error_does_not_turn_committed_operation_into_failure(runtime):
    _, _, _, tool, ctx, *_ = runtime
    tool.model_result = lambda *_: (_ for _ in ()).throw(ValueError('invalid presentation'))
    result = ToolPipeline().execute({'name': 'unit', 'args': {'value': 'one'}}, tool, ctx, 'fallback', 0)
    assert not result.is_error and result.data == {'ok': True}
    assert 'MODEL_VIEW_UNAVAILABLE' in result.formatted


@pytest.mark.parametrize('project', [contract_feedback, task_feedback, harness_feedback])
def test_errors_and_repair_guidance_are_not_pruned(project):
    data = {'error': 'STALE_REVISION', 'detail': 'Re-read current revision and reconcile',
        'effect_state': 'not_committed', 'next_actions': ['inspect current state'], 'authority_granted': False}
    assert project({'operation': 'revise'}, data) == data


def test_live_context_preserves_obligations_and_pending_work_without_journals():
    data = {'contract': contract_result()['contract'], 'source_refs': ['user:original'],
        'extensions': [{'program': 'duplicate compiler graph'}], 'consumer_projection': {'journal': 'internal'},
        'state': {'revision': 4, 'phase': 'needs_review', 'suspended': True,
            'source_receipts': {'old': 'large transcript'},
            'plan': {'steps': [{'goal': 'verify dependencies', 'verification': 'original tests'}]},
            'changes': {'new': {'status': 'pending', 'text': 'retain compatibility'}, 'old': {'status': 'acknowledged'}},
            'maintenance_items': {'dependency': {'status': 'pending', 'reason': 'dependency changed'}},
            'related': {'candidates': {'one': {'status': 'adopted', 'work': 'adjust consumer',
                'consequence': 'broken integration otherwise', 'owner': 'main', 'execution': {'receipt_ids': ['large']}}}}}}
    original = deepcopy(data)
    view = task_view(data)
    assert data == original
    assert view['contract']['acceptance_criteria'] == original['contract']['acceptance_criteria']
    assert view['contract']['uncertainties'] == ['external readiness']
    assert view['state']['related']['candidates']['one']['work'] == 'adjust consumer'
    assert view['state']['related']['candidates']['one']['consequence'] == 'broken integration otherwise'
    assert view['state']['suspended'] and view['state']['changes']['new']['text'] == 'retain compatibility'
    assert view['state']['maintenance_items']['dependency']['reason'] == 'dependency changed'
    assert all(k not in view for k in ('extensions', 'consumer_projection'))
    assert 'source_receipts' not in view['state']


def test_main_lifecycle_view_keeps_intent_and_control_without_compiler_graph(runtime):
    app, p, *_ = runtime
    current = enable(app, p, checkpoint_program(p))
    sections = app.context_sections()
    item = sections['harness.lifecycle']['checkpoints']
    assert item['phase'] == 'ready' and item['intent'] == checkpoint_program(p)['intent']
    assert 'program' not in item
    assert app.repo.instance(app.task, 'checkpoints')[0]['compiled']['elements']
    from backend.core.loop.task_maintenance import source_receipts
    app.invoke({'operation': 'suspend', 'instance_id': current['id'], 'expected_revision': current['revision'],
        'source_refs': list(source_receipts(p))})
    assert app.context_sections()['harness.lifecycle']['checkpoints']['phase'] == 'suspended'


def test_review_inline_excludes_successful_read_journal_but_keeps_failure_and_command(runtime):
    from backend.core.loop.review_evidence import prepare_review_evidence, inline_review_evidence
    _, p, *_ = runtime
    base = {'task_id': p.active_task_id, 'effect': 'read', 'succeeded': True, 'committed': False}
    p.tool_receipts = [{**base, 'receipt_id': 'read-1', 'tool_name': 'read_file'},
        {**base, 'receipt_id': 'failed', 'tool_name': 'search_text', 'succeeded': False, 'error_code': 'SEARCH_ENGINE_REQUIRED'},
        {**base, 'receipt_id': 'test', 'tool_name': 'exec_command', 'effect': 'process',
            'command_completed': True, 'command_exit_code': 0}]
    command = {'argv': ['python', '-m', 'unittest', '-v', 'test_status.py'], 'exit_code': 0,
        'stderr': 'Ran 1 test\nOK', 'truncated': False}
    p.session.append_tool_result('actual command output', tool_name='exec_command',
        data={'command_result': command}, receipt=p.tool_receipts[-1])
    evidence = prepare_review_evidence(p)
    assert [r['receipt_id'] for r in evidence['receipts']] == ['test', 'failed']
    assert evidence['receipts'][0]['witnessed_results'][0]['data']['command_result'] == command
    assert evidence['receipt_source']['inline_partial']
    assert list(inline_review_evidence(evidence))[0] == 'task_basis'


def test_only_latest_current_review_is_active_guidance_and_deploy_does_not_replay_it():
    instance = {'id': 'policy', 'epoch': 2, 'phase': 'ready', 'program_digest': 'current-version',
        'execution': {'checkpoints': [{'phase': 'skipped'}], 'reviews': [
            {'epoch': 1, 'summary': 'old criterion', 'verdict': 'changes_required'},
            {'epoch': 2, 'candidate_digest': 'older-candidate', 'summary': 'already reported'},
            {'epoch': 2, 'candidate_digest': 'current-candidate', 'summary': 'required repair', 'verdict': 'changes_required'}]}}
    original = deepcopy(instance)
    view = instance_view(instance)
    assert instance == original
    assert view['execution']['reviews'] == [original['execution']['reviews'][-1]]
    assert view['execution']['checkpoints'] == [{'phase': 'skipped'}]
    assert view['execution']['review_history_available_on_request']
    assert 'execution' not in harness_feedback({'operation': 'revise'}, {'committed': True, 'instance': instance})['instance']
