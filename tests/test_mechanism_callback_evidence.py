"""Engineering regressions for real Q1 failures, not semantic holdout cases."""
from copy import deepcopy
import json

import pytest

from tests.test_mechanism_interactions import runtime, enable, checkpoint_program, fixture_answer
from backend.core.mechanism_runtime.observations import checkpoint_observations, recent_tool_observations
from backend.core.mechanism_runtime.application import Application
from backend.core.mechanism_runtime.semantic_projection import snapshot


def test_superseded_prediction_retains_real_answer_without_covering_changed_effect(runtime):
    app, process, calls, _, _, batch, _ = runtime
    enable(app, process, checkpoint_program(process))
    assert batch.execute_live_batch([{'id': 'one', 'name': 'unit', 'args': {'value': 'one'}}]).result
    pending = fixture_answer(process, text='Prediction for one, not two')
    original = pending['harness_checkpoint']['unit_id']
    pending['tool_calls'][0]['args']['value'] = 'two'
    batch.execute_resumed_batch(pending)
    assert calls == [] and process.approval_grants == []
    # Reload from the existing state, not from a cached model explanation.
    restored = Application(process)
    instance = restored.repo.instance(app.task, 'checkpoints')[0]
    observed = checkpoint_observations(process, instance)[0]
    assert observed['unit_id'] == original and observed['phase'] == 'superseded'
    assert observed['answer_received'] and observed['answer'] == 'Prediction for one, not two'
    assert observed['reason'] == 'invocation_changed_before_resume'
    assert observed['actual_result'] == {'execution_state': 'not_started'}
    assert observed['authority_granted'] is False
    assert snapshot(process, observations=True)['instances'][0]['execution']['checkpoints'] == [observed]
    assert 'Prediction for one, not two' in restored.envelope()
    # Same user answer never silently releases the new invocation.
    batch.execute_live_batch([{'id': 'two', 'name': 'unit', 'args': {'value': 'two'}}])
    assert calls == [] and process.pending_decision['decision_id'] != observed['answer_id']


def test_failure_observations_survive_compaction_and_distinguish_nonexecution(runtime):
    app, process, *_ = runtime
    failure = {'task_id': app.task, 'receipt_id': 'actual-failure', 'tool_name': 'fixture-test',
        'succeeded': False, 'effect_state': 'committed', 'effect': 'process'}
    body = {'target': 'example.py', 'passed': False, 'seed_results': [
        {'output_tail': 'AssertionError: the old operation produced X, expected Y'}]}
    process.tool_receipts.append(failure)
    process.session.append_tool_result('actual failure', tool_name='fixture-test', is_error=True,
        receipt=failure, data=body)
    process.session.epoch_archive.append({'messages': deepcopy(process.session.messages)})
    process.session.messages.clear()
    process.tool_receipts += [
        {'task_id': app.task, 'receipt_id': 'rejected', 'tool_name': 'fixture-write',
         'succeeded': False, 'effect_state': 'not_committed', 'execution_state': 'not_started'},
        {'task_id': app.task, 'receipt_id': 'control', 'tool_name': 'fixture-control',
         'succeeded': True, 'effect_state': 'committed', 'execution_contract': {'broker': 'host.runtime.fixture'}},
        {'task_id': 'another-task', 'receipt_id': 'foreign', 'succeeded': False, 'effect_state': 'committed'}]
    before = deepcopy(process.tool_receipts)
    observed = recent_tool_observations(process)
    assert [x['receipt_id'] for x in observed] == ['actual-failure', 'rejected', 'control']
    assert json.loads(observed[0]['result_evidence']['preview']) == body
    assert observed[1]['execution_state'] == 'not_started' and 'result_evidence' not in observed[1]
    assert observed[2]['runtime_control'] is True
    assert process.tool_receipts == before
    # Text from a different receipt must not fill an evidence gap.
    process.session.epoch_archive[0]['messages'][-1]['receipt'] = {'receipt_id': 'forged-copy'}
    assert 'result_evidence' not in recent_tool_observations(process)[0]


def callback_declaration():
    return {'id': 'scoped-callbacks', 'intent': 'Judge proposed operations only; project separate guidance at context assembly',
        'on': ['before_context', 'before_tool', 'after_tool'], 'calls': [
            {'id': 'judge', 'use': 'agent.condition', 'on': ['before_tool'],
             'parameters': {'criterion': 'Does this actual proposed operation require a prediction?'}},
            {'id': 'question', 'use': 'interaction.checkpoint', 'when': 'judge.matches',
             'parameters': {'question': 'Predict this actual operation.', 'reason': 'Requested teaching', 'hint': 'Inspect the parameters.'}},
            {'id': 'note', 'use': 'context.project', 'on': ['before_context'],
             'parameters': {'content': 'Continue the requested business task.'}}]}


def test_call_subscriptions_route_real_callbacks_and_survive_named_and_legacy_revision(runtime):
    app, process, *_ = runtime
    seen = []
    app.services['agent.judge'] = lambda ctx, value: seen.append(deepcopy(value)) or {
        'matches': False, 'reason': 'Unit fixture', 'evidence_refs': [], 'authority_granted': False}
    app.coordinator.capabilities = tuple(app.services)
    def execute(args):
        app.capability_runtime.prepare(args)
        return app.invoke(args)
    applied = execute({'operation': 'apply', 'declaration': callback_declaration()})
    assert applied['committed']
    app.dispatch('before_context', 'context:first', {})
    app.dispatch('after_tool', 'result:first', {'tool_name': 'unit'})
    assert seen == []
    app.before_tool('actual-proposal', {'tool_name': 'unit', 'effect': 'process'})
    assert len(seen) == 1 and seen[0]['boundary'] == 'before_tool'
    revised = execute({'operation': 'revise', 'definition_id': 'scoped-callbacks',
        'revision': {'calls': [{'id': 'note', 'parameters': {'content': 'Updated real guidance.'}}]}})
    assert revised['committed']
    compiled = app.repo.instance(app.task, 'scoped-callbacks')[0]['compiled']
    from backend.core.mechanism_runtime.program_revision import revise_program
    raw = revise_program(compiled, [{'op': 'replace', 'path': '/intent', 'value': 'Preserve callback subscriptions'}], ['fixture-source'])
    assert next(n for n in raw['elements'] if n['id'] == 'judge')['on'] == ['before_tool']
    restored = Application(process)
    node = next(n for n in restored.repo.instance(app.task, 'scoped-callbacks')[0]['compiled']['elements'] if n['id'] == 'judge')
    assert node['boundaries'] == ['before_tool']


@pytest.mark.parametrize('target,on', [('judge', ['completion']), ('question', ['after_tool']),
    ('judge', []), ('judge', ['before_tool', 'before_tool'])])
def test_invalid_call_subscription_never_publishes(runtime, target, on):
    app, *_ = runtime
    app.services['agent.judge'] = lambda ctx, value: pytest.fail('Invalid subscription must never dispatch')
    app.coordinator.capabilities = tuple(app.services)
    value = callback_declaration()
    next(c for c in value['calls'] if c['id'] == target)['on'] = on
    with pytest.raises(ValueError, match='BOUNDARY_INVALID|uniqueItems|minItems'):
        app.capability_runtime.prepare({'operation': 'apply', 'declaration': value})
    assert app.repo.definition(value['id']) == (None, None)
