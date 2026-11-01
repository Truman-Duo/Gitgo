"""Regression evidence for stage/feedback races and bounded review inputs."""
import json
from copy import deepcopy
from types import SimpleNamespace

from tests.test_mechanism_interactions import runtime, enable
from backend.core.loop.delivery_runtime import DeliveryRuntime
from backend.core.loop.loop_bookkeeping import _apply_pending_instructions
from backend.core.loop.models import ProcessStatus
from backend.core.loop.task_maintenance import source_receipts


def test_feedback_arriving_in_review_precedes_return_limit_question(runtime):
    app, process, *_ = runtime
    raw = {"schema_version": 2, "id": "review-race", "intent": "Bound independent review returns", "source_refs": list(source_receipts(process)),
        "elements": [{"id": "event", "use": "runtime.event", "parameters": {"boundaries": ["completion"]}},
            {"id": "review", "use": "agent.review", "parameters": {"instructions": "Inspect evidence", "max_steps": 4}},
            {"id": "return", "use": "completion.return", "parameters": {"max_returns": 2,
                "instruction": "Repair the reported issue", "question": "What next?", "reason": "Return limit"}}],
        "bindings": [{"kind": "data", "from": {"node": "event", "port": "event"}, "to": {"node": "review", "port": "event"}},
            {"kind": "trigger", "from": {"node": "event", "port": "go"}, "to": {"node": "review", "port": "go"}},
            {"kind": "condition", "from": {"node": "review", "port": "changes_required"}, "to": {"node": "return", "port": "go"}, "when": True},
            {"kind": "data", "from": {"node": "review", "port": "evidence"}, "to": {"node": "return", "port": "evidence"}}]}
    app.services['agent.review'] = lambda *a: {'verdict': 'changes_required', 'evidence': ['fixture defect']}
    app.coordinator.capabilities = tuple(app.services)
    enable(app, process, raw)
    delivery = DeliveryRuntime(process, process.session, None, None, emit=lambda *a: None,
        apply_mailbox=lambda: _apply_pending_instructions(process, process.session), pause_blocked=lambda *a: None,
        capture_recovery=lambda *a: None, increment_nudge=lambda *a: None, nudge_count=lambda *a: 0)
    for i in range(2):
        assert delivery.finish(str(i), 0, {'action': 'none', 'usage_ratio': 0}, source='fixture').retry
    def old_review(ctx, payload):
        process.mailbox.enqueue_instruction('Revise the current policy after this review finishes.')
        return {'verdict': 'changes_required', 'evidence': ['old policy finding'], 'review_epoch': ctx.epoch}
    app.services['agent.review'] = old_review
    result = delivery.finish('third', 0, {'action': 'none', 'usage_ratio': 0}, source='fixture')
    assert result.retry and result.result is None
    assert process.pending_decision is None
    assert process.mailbox.snapshot()['pending'] == 0
    instance = app.repo.instance(app.task, 'review-race')[0]
    assert instance['return_counters'] == {'return': 2}
    assert all(run['phase'] == 'complete' for run in instance['runs'].values())
    assert any(m.get('mailbox_message_id') for m in process.session.messages)
    # Once that feedback has been interpreted, an unchanged failing candidate
    # still hits the SAME cap; deferral is neither approval nor a counter reset.
    app.services['agent.review'] = lambda *a: {'verdict': 'changes_required', 'evidence': ['fixture defect']}
    result = delivery.finish('fourth', 0, {'action': 'none', 'usage_ratio': 0}, source='fixture')
    assert result.result['status'] == 'awaiting_user'
    assert app.repo.instance(app.task, 'review-race')[0]['return_counters'] == {'return': 2}


def test_feedback_after_stage_preparation_cannot_publish_a_stale_wait(runtime, monkeypatch):
    app, process, *_ = runtime
    raw = {'schema_version': 2, 'id': 'late-feedback', 'intent': 'Bounded waiting', 'source_refs': list(source_receipts(process)),
        'elements': [{'id': 'event', 'use': 'runtime.event', 'parameters': {'boundaries': ['completion']}},
            {'id': 'return', 'use': 'completion.return', 'parameters': {'max_returns': 0,
                'instruction': 'Inspect', 'question': 'What next?', 'reason': 'Limit'}}],
        'bindings': [{'kind': 'trigger', 'from': {'node': 'event', 'port': 'go'}, 'to': {'node': 'return', 'port': 'go'}}]}
    enable(app, process, raw)
    original = app.dispatch
    def dispatch(*args, **kwargs):
        result = original(*args, **kwargs)
        assert process.pending_decision is None
        process.mailbox.enqueue_instruction('New input at the delivery boundary')
        return result
    monkeypatch.setattr(app, 'dispatch', dispatch)
    delivery = DeliveryRuntime(process, process.session, None, None, emit=lambda *a: None,
        apply_mailbox=lambda: _apply_pending_instructions(process, process.session), pause_blocked=lambda *a: None,
        capture_recovery=lambda *a: None, increment_nudge=lambda *a: None, nudge_count=lambda *a: 0)
    assert delivery.finish('candidate', 0, {'action': 'none', 'usage_ratio': 0}, source='fixture').retry
    assert process.pending_decision is None and not process.mailbox.snapshot()['pending']


def test_review_snapshot_keeps_command_body_across_epochs_without_claiming_a_write(runtime):
    from backend.core.loop.review_evidence import prepare_review_evidence, read_review_evidence
    app, owner, *_ = runtime
    receipt = {'receipt_id': 'actual-command', 'task_id': app.task, 'tool_name': 'exec_command',
        'effect': 'process', 'succeeded': True, 'committed': False,
        'effect_state': 'not_committed', 'command_completed': True, 'command_exit_code': 0}
    body = {'command_result': {'argv': ['python', '-m', 'unittest', 'test_feature'],
        'stdout': '', 'stderr': 'Ran 1 test\nOK\n', 'exit_code': 0, 'truncated': False}}
    message = {'message_type': 'tool_result', 'receipt': receipt, 'data': body, 'is_error': False}
    owner.tool_receipts = [deepcopy(receipt)]
    owner.session.epoch_archive = [{'messages': [deepcopy(message)]}]
    owner.session.messages.append(deepcopy(message))  # Retained overlap must not double the body.
    owner.session.messages.append({**message, 'receipt': {**receipt, 'task_id': 'foreign'},
        'data': {'command_result': {'stderr': 'Foreign outcome'}}})
    evidence = prepare_review_evidence(owner)
    pointer = evidence['receipts'][0]['result_json_pointers']
    assert pointer == ['/tool_results/0'] and evidence['receipts'][0]['committed'] is False
    child = SimpleNamespace(actor_kind='reviewer', capability_profile_id='review.independent', review_target_id=owner.process_id,
        parent_id=owner.process_id, _manager=owner._manager, runtime_preferences={'review_evidence_source': evidence['receipt_source']})
    owner.session.epoch_archive[0]['messages'][0]['data']['command_result']['stderr'] = 'Changed later'
    actual = read_review_evidence(child, {'locator': evidence['receipt_source']['locator'], 'json_pointer': pointer[0]})
    assert json.loads(actual['content'])['data'] == body
    assert not evidence['observed_diffs']


def test_review_snapshot_keeps_early_test_and_is_immutable_and_scoped(runtime):
    from backend.core.loop.review_evidence import prepare_review_evidence, read_review_evidence
    app, owner, *_, store = runtime
    owner.tool_receipts = [{'receipt_id': 'old-test', 'task_id': app.task, 'tool_name': 'run_test',
        'test_id': 'original', 'test_completed': True, 'test_passed': True, 'committed': True}]
    owner.tool_receipts += [{'receipt_id': f'query-{i}', 'task_id': app.task, 'tool_name': 'read_file'} for i in range(45)]
    evidence = prepare_review_evidence(owner)
    assert any(r['receipt_id'] == 'old-test' for r in evidence['receipts'])
    assert evidence['receipt_source']['total_receipts'] == 46
    child = SimpleNamespace(actor_kind='reviewer', capability_profile_id='review.independent', review_target_id=owner.process_id,
        parent_id=owner.process_id, _manager=owner._manager, runtime_preferences={'review_evidence_source': evidence['receipt_source']})
    source = evidence['receipt_source']
    args = {'locator': source['locator'], 'json_pointer': '/receipts/0'}
    owner.tool_receipts[0]['test_passed'] = False
    observed = read_review_evidence(child, args)
    assert json.loads(observed['content'])['test_passed'] is True
    assert read_review_evidence(child, {'locator': 'tool-result:sha256:unrelated'}) is None
    child.review_target_id = 'unrelated-owner'
    import pytest
    with pytest.raises(PermissionError):
        read_review_evidence(child, args)


def test_dynamic_guidance_uses_current_state_without_enabling_suspended_program(runtime):
    from tests.test_mechanism_interactions import checkpoint_program
    app, process, *_ = runtime
    current = enable(app, process, checkpoint_program(process))
    app.invoke({'operation': 'suspend', 'instance_id': current['id'], 'expected_revision': current['revision'],
        'source_refs': list(source_receipts(process))})
    before = deepcopy(app.repo.list_instances(app.task))
    sections = app.context_sections()
    guide = sections['harness.next_boundary']
    assert guide['task_id'] == app.task and guide['authority_granted'] is False
    assert guide['instances'][current['id']]['action'] == 'keep_inactive'
    assert guide['instances'][current['id']]['effective_ref'] == before[0]['effective_ref']
    assert 'harness.next_boundary' in app.envelope(sections=sections)
    assert app.repo.list_instances(app.task) == before


def test_full_evidence_remains_retrievable_when_inline_inventory_is_full(runtime):
    from backend.core.loop.review_evidence import prepare_review_evidence
    app, owner, *_, store = runtime
    owner.tool_receipts = [{'receipt_id': str(i), 'task_id': app.task, 'tool_name': 'run_test',
        'test_completed': True, 'test_passed': i != 99} for i in range(100)]
    owner.tool_receipts.append({'receipt_id': 'foreign', 'task_id': 'another-task', 'tool_name': 'run_test'})
    evidence = prepare_review_evidence(owner)
    assert len(evidence['receipts']) <= 64 and evidence['receipt_source']['inline_partial']
    assert evidence['receipt_source']['total_receipts'] == 100
    complete = json.loads(store._storage.read_tool_result(evidence['receipt_source']['locator'],
        json_pointer='/receipts/99')['content'])
    assert complete['test_passed'] is False


def test_review_receives_archived_patch_after_real_checkpoint_restore(runtime):
    from backend.core.loop.review_evidence import prepare_review_evidence
    from backend.core.mechanism_runtime.reviews import review_description
    app, owner, *_, store = runtime
    receipt = {'receipt_id': 'committed-edit', 'task_id': app.task, 'tool_name': 'edit_file',
        'effect': 'workspace_write', 'effect_state': 'committed', 'succeeded': True, 'committed': True,
        'files': ['api.py'], 'before_sha256': 'a' * 64, 'after_sha256': 'b' * 64}
    patch = '--- a/api.py\n+++ b/api.py\n@@ -1 +1 @@\n-return old_value\n+return new_value\n'
    message = {'message_type': 'tool_result', 'receipt': deepcopy(receipt), 'data': {'diff': patch}, 'is_error': False}
    owner.tool_receipts = [receipt]
    owner.session.epoch_archive = [{'epoch': 0, 'messages': [message]}]
    owner.session.messages = []
    store.save_process_checkpoint(owner)
    restored = store.load_session_state(owner.process_id)
    owner.session.epoch_archive = restored['epoch_archive']
    owner.session.messages = restored['messages']
    evidence = prepare_review_evidence(owner)
    assert evidence['observed_diffs'][0]['diff'] == patch
    assert evidence['observed_diffs'][0]['full_before_file'] is False
    ctx = SimpleNamespace(task_id=app.task, instance_id='review', epoch=2, event_id='review-event')
    description = review_description(ctx, {'instructions': 'Review observable interface changes', 'event': evidence})
    assert json.dumps(patch)[1:-1] in description
    bundle = json.loads(store._storage.read_tool_result(evidence['receipt_source']['locator'],
        json_pointer='/observed_diffs')['content'])
    assert bundle[0]['receipt_id'] == receipt['receipt_id'] and bundle[0]['diff'] == patch


def test_diff_preview_is_bounded_and_excludes_foreign_failed_or_unwitnessed_changes(runtime):
    from backend.core.loop.review_evidence import prepare_review_evidence
    app, owner, *_, store = runtime
    base = {'receipt_id': 'valid', 'task_id': app.task, 'effect': 'workspace_write', 'effect_state': 'committed',
        'succeeded': True, 'committed': True, 'tool_name': 'edit_file'}
    failed = {**base, 'receipt_id': 'failed', 'succeeded': False}
    foreign = {**base, 'receipt_id': 'foreign', 'task_id': 'other-task'}
    owner.tool_receipts = [base, failed, foreign]
    patch = '-old\n+new\n' * 1500
    owner.session.epoch_archive = [{'messages': [
        {'message_type': 'tool_result', 'receipt': r, 'data': {'diff': patch}} for r in (base, failed, foreign)]}]
    owner.session.messages = [{'message_type': 'tool_result', 'receipt': {**base, 'committed': False}, 'data': {'diff': 'forged'}}]
    evidence = prepare_review_evidence(owner)
    assert len(evidence['observed_diffs']) == 1 and not evidence['observed_diffs'][0]['complete']
    assert len(evidence['observed_diffs'][0]['diff']) <= 4000
    full = json.loads(store._storage.read_tool_result(evidence['receipt_source']['locator'],
        json_pointer='/observed_diffs/0/diff', max_chars=24000)['content'])
    assert full == patch
