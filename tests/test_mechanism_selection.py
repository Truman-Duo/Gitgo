"""Regression for exact saved-version selection and readable deployment previews.

Real storage and pipeline, synthetic engineering fixtures; not semantic eval.
"""
from copy import deepcopy
import json

from tests.test_mechanism_interactions import runtime
from tests.test_mechanism_capability import pipeline_tool, declaration


def test_apply_preview_cannot_read_as_a_committed_deployment(runtime):
    app, *_ = runtime
    tool, execute = pipeline_tool(runtime)
    args = {'operation': 'preview', 'declaration': declaration()}
    _, result = execute(args)
    assert not result.is_error
    view = tool.model_result(args, result.data)
    assert view['operation'] == 'preview' and view['state'] == 'preview_only'
    assert view['committed'] is False and 'no definition is published or active' in view['next_action']
    assert view['next_call'] == {'operation': 'commit', 'preview_ref': result.data['preview_ref']}
    assert view['declaration'] == declaration()  # meaning once; submission needs no copy
    assert app.repo.definition('guidance') == (None, None)
    assert not app.repo.list_instances(app.task)


def versions(runtime):
    app, *_ = runtime
    tool, execute = pipeline_tool(runtime)
    _, first = execute({'operation': 'apply', 'declaration': declaration()})
    assert not first.is_error, first.formatted
    _, second = execute({'operation': 'revise', 'definition_id': 'guidance',
        'revision': {'calls': [{'id': 'note', 'parameters': {'content': 'Second saved behavior'}}]}})
    assert not second.is_error, second.formatted
    return app, tool, execute


def test_preview_exact_older_version_is_not_current_head_or_active_task_version(runtime):
    app, tool, execute = versions(runtime)
    args = {'operation': 'preview', 'definition_id': 'guidance', 'definition_revision': 1,
        'instance_id': 'reused', 'version_policy': 'pinned'}
    before = deepcopy(app.repo.list_instances(app.task))
    _, result = execute(args)
    assert not result.is_error, result.formatted
    assert result.data['definition_revision'] == 1 and result.data['definition_head_revision'] == 2
    assert result.data['selection'] == 'explicit_revision'
    assert result.data['previous'] is None and result.data['previous_definition_revision'] is None
    view = tool.model_result(args, result.data)
    assert view['next_call'] == {'operation': 'commit', 'preview_ref': result.data['preview_ref']}
    assert view['declaration']['calls'][0]['parameters']['content'] == 'Preserve the public interface.'
    assert not {'source_evidence', 'expected_definition_head', 'component_contracts', 'instance_basis'} & view.keys()
    assert len(json.dumps(view)) < len(json.dumps(result.data)) * .7
    assert app.repo.list_instances(app.task) == before  # preview never commits
    _, newer = execute({'operation': 'revise', 'definition_id': 'guidance',
        'revision': {'calls': [{'id': 'note', 'parameters': {'content': 'Third saved behavior'}}]}})
    assert not newer.is_error
    stale_boundary, stale = execute(view['next_call'])
    assert not stale_boundary.contracts and stale.is_error and 'PREVIEW_STALE' in stale.formatted
    # A fresh explicit selection is still available; the old draft cannot
    # silently acquire a different comparison basis after publication.
    boundary, selected = execute({**args, 'operation': 'use'})
    assert boundary.contracts[0]['definition_revision'] == 1
    assert 'declaration' not in boundary.contracts[0]
    assert boundary.contracts[0]['target'] == result.data['target']
    assert selected.data['definition_revision'] == 1 and selected.data['instance']['phase'] == 'ready'
    assert app.repo.definition('guidance')[0]['revision'] == 3
    assert not runtime[1].approval_grants


def test_preview_default_pins_observed_revision_in_suggestion_without_binding_future_calls(runtime):
    app, tool, execute = versions(runtime)
    args = {'operation': 'preview', 'definition_id': 'guidance', 'instance_id': 'reused'}
    _, result = execute(args)
    view = tool.model_result(args, result.data)
    assert view['selection'] == 'latest_saved_revision' and view['definition_revision'] == 2
    assert view['next_call']['operation'] == 'commit'
    _, explicit_old = execute({**args, 'definition_revision': 1})
    assert explicit_old.data['definition_revision'] == 1
    # A separate call without a revision has documented latest semantics;
    # previews cannot silently change later arguments or become authorization.
    _, latest = execute({'operation': 'use', 'definition_id': 'guidance', 'instance_id': 'reused'})
    assert latest.data['definition_revision'] == 2


def test_preview_replacing_instance_compares_its_actual_selected_version(runtime):
    app, tool, execute = versions(runtime)
    _, used = execute({'operation': 'use', 'definition_id': 'guidance', 'definition_revision': 1, 'instance_id': 'old'})
    assert not used.is_error
    args = {'operation': 'preview', 'definition_id': 'guidance', 'definition_revision': 2, 'instance_id': 'old'}
    _, result = execute(args)
    assert result.data['previous_definition_revision'] == 1
    view = tool.model_result(args, result.data)
    assert view['changes']['changed_calls_before'][0]['parameters']['content'] == 'Preserve the public interface.'
    assert view['declaration']['calls'][0]['parameters']['content'] == 'Second saved behavior'


def test_status_reads_exact_history_without_mutation_or_fallback(runtime):
    app, tool, execute = versions(runtime)
    before = deepcopy(app.repo.list_instances(app.task))
    args = {'operation': 'status', 'definition_id': 'guidance', 'definition_revision': 1}
    _, result = execute(args)
    assert not result.is_error
    item = result.data['definitions'][0]
    assert item['revision'] == 1 and item['selected_ref'] == app.repo.definition('guidance', 1)[1]
    assert item['head_ref'] == app.repo.definition('guidance')[1] != item['selected_ref']
    assert tool.model_result(args, result.data)['definitions'][0]['declaration'] == declaration()
    _, missing = execute({**args, 'definition_revision': 98})
    assert missing.is_error and 'no fallback to latest' in missing.formatted
    assert app.repo.list_instances(app.task) == before


def test_preview_full_subject_remains_readable_when_model_projection_is_compact(runtime):
    app, tool, execute = versions(runtime)
    args = {'operation': 'preview', 'definition_id': 'guidance', 'definition_revision': 1}
    _, result = execute(args)
    start = result.formatted.index('{')
    presented = json.loads(result.formatted[start:])
    descriptor = presented['details']
    retained = json.loads(app.repo.storage.read_tool_result(descriptor['locator'], max_chars=100000)['content'])
    assert retained == result.data
    assert retained['component_contracts'] and retained['source_evidence']
    assert presented['definition_revision'] == 1
