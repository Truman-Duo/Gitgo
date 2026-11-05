"""Runtime-bound control through the canonical compiler, storage and pipeline."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from tests.test_mechanism_interactions import runtime
from tests.test_mechanism_capability import declaration, pipeline_tool
from backend.core.loop.task_maintenance import source_receipts


def enabled(runtime):
    app, *_ = runtime
    tool, execute = pipeline_tool(runtime)
    _, result = execute({'operation': 'apply', 'declaration': declaration()})
    assert not result.is_error, result.formatted
    return app, tool, execute


def test_simple_lifecycle_controls_keep_one_original_instance(runtime):
    app, tool, execute = enabled(runtime)
    original = app.repo.instance(app.task, 'guidance')[0]
    schema = tool.to_openai_function()['function']['parameters']
    for operation, phase in [('suspend', 'suspended'), ('activate', 'ready'), ('reconcile', 'ready'), ('detach', 'detached')]:
        args = {'operation': operation, 'instance_id': 'guidance'}
        Draft202012Validator(schema).validate(args)
        boundary, result = execute(args)
        assert len(boundary.contracts) == 1 and not result.is_error, result.formatted
        current = app.repo.instance(app.task, 'guidance')[0]
        assert current['phase'] == phase and current['definition_id'] == original['definition_id']
        assert current['sources'] == original['sources']
    assert len(app.repo.list_instances(app.task)) == 1 and not app.process.approval_grants


@pytest.mark.parametrize('change', ['source', 'contract', 'instance', 'owner'])
def test_prepared_lifecycle_rejects_concurrent_meaning_or_authority_change(runtime, change):
    app, _, _ = enabled(runtime)
    args = {'operation': 'suspend', 'instance_id': 'guidance'}
    app.lifecycle_control.prepare(args)
    if change == 'source':
        app.process.session.append_user('Keep it enabled.')
    elif change == 'contract':
        app.process.update_context_snapshot(lambda value: {**value, 'task_contract': {'revision': 2}})
    elif change == 'owner':
        app.repo.mutate(app.task, 'guidance', lambda value: value.update(managed_by={'task_id': 'owner'}))
    else:
        app.repo.mutate(app.task, 'guidance', lambda value: value.update(revision=value['revision'] + 1))
    with pytest.raises(ValueError, match='PREPARED_STATE_CHANGED|OWNER_ONLY'):
        app.lifecycle_control.commit(args)
    assert app.repo.instance(app.task, 'guidance')[0]['phase'] == 'ready'


def test_legacy_assertions_and_real_user_exit_are_not_replaced_by_automatic_binding(runtime):
    app, _, execute = enabled(runtime)
    args = {'operation': 'suspend', 'instance_id': 'guidance'}
    for extra, expected in [({'expected_revision': 99}, 'REVISION_CONFLICT'),
                            ({'source_refs': ['user:invented']}, 'ACTUAL_USER_SOURCE_REQUIRED')]:
        _, result = execute({**args, **extra})
        assert result.is_error and expected in result.formatted
    current = app.repo.instance(app.task, 'guidance')[0]
    app.coordinator.request(app.task, current['id'], current['desired_ref'], expected_revision=current['revision'],
        action='suspend', sources=source_receipts(app.process),
        user_control={'source_ref': 'decision:exit', 'authority_sequence': 50})
    app.coordinator.settle(app.task, current['id'])
    _, result = execute({'operation': 'activate', 'instance_id': 'guidance'})
    assert result.is_error and 'NEWER_USER_CONTROL' in result.formatted
    assert app.repo.instance(app.task, 'guidance')[0]['phase'] == 'suspended'


def test_require_binds_exact_definitions_and_rejects_head_changes_before_commit(runtime):
    app, tool, execute = enabled(runtime)
    args = {'operation': 'require', 'requirements': [{'definition_id': 'guidance', 'version_policy': 'pinned'}]}
    Draft202012Validator(tool.to_openai_function()['function']['parameters']).validate(args)
    app.lifecycle_control.prepare(args)
    _, revised = execute({'operation': 'revise', 'definition_id': 'guidance',
        'revision': {'intent': 'Preserve the interface and verify downstream consumers'}})
    assert not revised.is_error, revised.formatted
    with pytest.raises(ValueError, match='PREPARED_STATE_CHANGED'):
        app.lifecycle_control.commit(args)
    assert app.requirements.read(app.task) == (None, None)
    _, result = execute(args)
    assert not result.is_error, result.formatted
    required, _ = app.requirements.read(app.task)
    selected = app.repo.definition_ref(required['bindings'][0]['reference'])
    assert selected['revision'] == 2
    _, removed = execute({'operation': 'require', 'requirements': []})
    assert not removed.is_error and not app.requirements.read(app.task)[0]['bindings']


def test_lifecycle_keeps_semantic_admission_and_managed_owner_checks(runtime):
    from backend.core.loop.alignment_routing import REVIEW_POLICY
    app, _, execute = enabled(runtime)
    before = deepcopy(app.repo.instance(app.task, 'guidance')[0])
    app.process.context_snapshot['alignment_runtime'] = {'task_id': app.task, 'process_id': app.process.process_id,
        'admission_review': {'status': 'required'}, 'review_policy': REVIEW_POLICY}
    _, rejected = execute({'operation': 'suspend', 'instance_id': 'guidance'})
    assert rejected.is_error and rejected.diagnostics['code'] == 'ALIGNMENT_REVIEW_REQUIRED'
    assert app.repo.instance(app.task, 'guidance')[0] == before
