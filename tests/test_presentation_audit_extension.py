"""Post-v22 SDK component checks, not real API or human semantic labels."""
from pathlib import Path
import runpy
from types import SimpleNamespace
import pytest

component = runpy.run_path(str(Path(__file__).resolve().parent / 'fixtures/mechanism_extensions/postfreeze-presentation-component.py'))


def run(items, state=None, *, partial=False, task='t', include_received=False):
    calls = []
    def call(name, payload):
        calls.append((name, payload))
        return {'task_id': task, 'questions': {'items': items, 'partial': partial}} if name == 'decision.read' else {}
    ctx = SimpleNamespace(task_id='t', state=state or {}, check=lambda: None, call=call)
    return component['audit'](ctx, {'include_received': include_received}, {}), calls


def question(label='First', status='awaiting_user'):
    return {'task_id': 't', 'decision_id': 'q', 'kind': 'clarification', 'status': status,
        'question': 'Next?', 'options': [{'label': label}], 'materials': []}


def test_same_identity_presentation_changes_are_observed_without_answering():
    first, _ = run([question()])
    second, calls = run([question('Changed')], first.state)
    assert second.outputs['report']['changed_visible_presentations'] == ['q']
    assert not second.outputs['report']['authority_granted']
    assert [name for name, _ in calls] == ['decision.read', 'context.project']


def test_received_filter_and_partial_history_preserve_previous_complete_witness():
    first, _ = run([question()])
    partial, _ = run([question(status='received')], first.state, partial=True)
    assert partial.outputs['report']['records'] == [] and partial.state['versions'] == first.state['versions']
    full, _ = run([question(status='received')], include_received=True)
    assert full.outputs['report']['records'][0]['status'] == 'received'


@pytest.mark.parametrize('foreign', ['service', 'question'])
def test_foreign_task_is_rejected_before_projection(foreign):
    with pytest.raises(ValueError, match='SCOPE_CHANGED'):
        run([question() if foreign == 'service' else {**question(), 'task_id': 'foreign'}],
            task='foreign' if foreign == 'service' else 't')


def test_registration_without_projection_service_stays_blocked(tmp_path_factory):
    from backend.core.storage import StorageRuntime
    from backend.core.mechanism_runtime.registry import Registry
    from backend.core.mechanism_runtime.components import install
    from backend.core.mechanism_runtime.repository import Repository
    from backend.core.mechanism_runtime.coordinator import Coordinator
    registry = Registry()
    install(registry)
    component['install'](registry)
    work = tmp_path_factory / 'work'
    work.mkdir()
    with StorageRuntime(work, state_home=tmp_path_factory / 'state') as storage:
        repo = Repository(storage)
        compiler = Coordinator(registry, repo, capabilities=('decision.read', 'context.project'))
        program = {'schema_version': 2, 'id': 'unit-presentation', 'intent': 'SDK initialization failure fixture',
            'source_refs': ['unit:fixture'], 'elements': [
                {'id': 'event', 'use': 'runtime.event', 'parameters': {'boundaries': ['restored']}},
                {'id': 'audit', 'use': 'question.presentation_audit'}],
            'bindings': [{'kind': 'trigger', 'from': {'node': 'event', 'port': 'go'}, 'to': {'node': 'audit', 'port': 'go'}}]}
        definition, ref = repo.publish(compiler.preview(program)['compiled'], {'unit:fixture': {'kind': 'unit_fixture'}}, expected_head=None)
        repo.create_instance('task', 'unit', definition, ref, policy='pinned', sources=definition['sources'])
        owner = Coordinator(registry, repo, capabilities=('decision.read',))
        owner.request('task', 'unit', ref, expected_revision=1, sources=definition['sources'])
        with pytest.raises(ValueError, match='ACTIVATION_SERVICE_MISSING'):
            owner.settle('task', 'unit')
        instance = repo.instance('task', 'unit')[0]
        assert instance['phase'] == 'blocked' and instance['epoch'] == 0 and not instance['runs'] and not instance['states']
