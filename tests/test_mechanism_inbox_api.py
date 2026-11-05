"""Real storage/pipeline regressions, not independent semantic evaluation."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from tests.test_mechanism_interactions import runtime, enable, inbox_program, fixture_discussion
from tests.test_mechanism_capability import pipeline_tool
from backend.core.loop.task_maintenance import state_for, source_receipts


def candidates(runtime):
    app, process, *_ = runtime
    enable(app, process, inbox_program(process))
    for sidecar, text in [('first', 'Preserve compatibility.'), ('second', 'Test Chinese fields.')]:
        app.receive_discussion(fixture_discussion(app, text, sidecar))
    return list(app.repo.instance(app.task, 'discussions')[0]['inbox'])


def test_simple_disposition_api_uses_existing_inbox_and_keeps_adoption_explicit(runtime):
    app, process, *_ = runtime
    first, second = candidates(runtime)
    tool, execute = pipeline_tool(runtime)
    schema = tool.to_openai_function()['function']['parameters']
    args = {'operation': 'inbox_revise', 'candidate_id': first, 'text': 'Preserve the old public signature.'}
    Draft202012Validator(schema).validate(args)
    boundary, result = execute(args)
    assert len(boundary.contracts) == 1 and not result.is_error, result.formatted
    assert result.data['candidate']['revision'] == 2
    boundary, result = execute({'operation': 'inbox_merge', 'candidates': [{'id': first}, {'id': second}],
        'text': 'Preserve the old public signature and test Chinese fields.'})
    assert not result.is_error, result.formatted
    merged = result.data['candidate']
    assert merged['status'] == 'candidate' and not state_for(process).get('changes')
    args = {'operation': 'inbox_adopt', 'candidate_id': merged['id'], 'quote': 'Adopt these requirements.'}
    _, invalid = execute(args)
    assert invalid.is_error and 'ACTUAL_USER_QUOTE_REQUIRED' in invalid.formatted
    process.session.append_user('Adopt these requirements.')
    _, adopted = execute(args)
    assert not adopted.is_error, adopted.formatted
    assert adopted.data['candidate']['status'] == 'adopted'
    assert len(state_for(process)['changes']) == 1 and not process.approval_grants


@pytest.mark.parametrize('change', ['candidate', 'source', 'contract', 'instance'])
def test_prepared_disposition_cannot_change_between_review_and_commit(runtime, change):
    app, process, *_ = runtime
    first, _ = candidates(runtime)
    args = {'operation': 'inbox_discard', 'candidate_id': first}
    app.inbox.prepare(args)
    if change == 'candidate':
        app.inbox.control({'operation': 'inbox_revise', 'instance_id': 'discussions', 'candidate_id': first,
            'candidate_revision': 1, 'source_refs': list(source_receipts(process)), 'text': 'Changed concurrently.'})
    elif change == 'source':
        process.session.append_user('Keep that candidate for now.')
    elif change == 'contract':
        process.update_context_snapshot(lambda value: {**value, 'task_contract': {'revision': 2}})
    else:
        app.repo.mutate(app.task, 'discussions', lambda value: value.update(epoch=value['epoch'] + 1))
    with pytest.raises(ValueError, match='PREPARED_STATE_CHANGED'):
        app.inbox.commit(args)
    assert app.repo.instance(app.task, 'discussions')[0]['inbox'][first]['status'] == 'candidate'
    assert not state_for(process).get('changes') and not process.approval_grants


def test_ambiguous_selection_and_forged_legacy_assertions_do_not_rebind(runtime):
    app, process, *_ = runtime
    first, _ = candidates(runtime)
    other = inbox_program(process)
    other['id'] = 'other'
    enable(app, process, other)
    # The same authentic discussion may feed two explicitly enabled instances.
    app.receive_discussion(fixture_discussion(app, 'Shared candidate.', 'shared'))
    shared = next(item for item in app.repo.instance(app.task, 'other')[0]['inbox'].values())
    with pytest.raises(ValueError, match='SCOPE_REQUIRED'):
        app.inbox.prepare({'operation': 'inbox_discard', 'candidate_id': shared['id']})
    args = {'operation': 'inbox_discard', 'candidate_id': first, 'instance_id': 'discussions'}
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        app.inbox.prepare({**args, 'candidate_revision': 9})
    with pytest.raises(ValueError, match='ACTUAL_USER_SOURCE_REQUIRED'):
        app.inbox.prepare({**args, 'source_refs': ['user:invented']})
    app.inbox.prepare(args)
    assert app.inbox.commit(args)['candidate']['status'] == 'discarded'


def test_status_gives_actionable_candidate_without_provenance_dump(runtime):
    from backend.core.loop.model_feedback import harness_feedback
    app, process, *_ = runtime
    first, _ = candidates(runtime)
    _, execute = pipeline_tool(runtime)
    args = {'operation': 'status'}
    _, result = execute(args)
    assert not result.is_error
    full = deepcopy(result.data)
    view = harness_feedback(args, full)
    candidate = next(item for instance in view['instances'] for item in instance.get('inbox', []) if item['id'] == first)
    assert candidate['text'] == 'Preserve compatibility.' and not candidate['text_truncated']
    assert 'source_refs' not in candidate and 'revision' not in candidate
    assert full == result.data  # full evidence remains retrievable


def test_preparation_never_bypasses_existing_semantic_admission(runtime):
    from backend.core.loop.alignment_routing import REVIEW_POLICY
    app, process, *_ = runtime
    first, _ = candidates(runtime)
    _, execute = pipeline_tool(runtime)
    process.context_snapshot['alignment_runtime'] = {'task_id': app.task, 'process_id': process.process_id,
        'admission_review': {'status': 'required'}, 'review_policy': REVIEW_POLICY}
    _, result = execute({'operation': 'inbox_discard', 'candidate_id': first})
    assert result.is_error and result.diagnostics['code'] == 'ALIGNMENT_REVIEW_REQUIRED'
    assert app.repo.instance(app.task, 'discussions')[0]['inbox'][first]['status'] == 'candidate'
