"""Post-v20 component tests; no frozen core changes and no real API claims."""
from pathlib import Path
import runpy
from types import SimpleNamespace
import pytest

component = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'dev-artifacts/postfreeze-lineage-component.py'))


def run(values, state=None, *, partial=False, task='t'):
    calls = []
    def call(name, payload):
        calls.append((name, payload))
        return {'task_id': task, 'confirmed': values, 'next_offset': 32 if partial else None} if name == 'decision.read' else {}
    ctx = SimpleNamespace(task_id='t', state=state or {}, check=lambda: None, call=call)
    return component['audit'](ctx, {'max_pages': 1}, {}), calls


def test_lineage_uses_real_successor_identity_and_remembers_complete_snapshots():
    first, _ = run([{'decision_id': 'a', 'topic': 'code', 'value': 'A'}])
    second, calls = run([{'decision_id': 'b', 'topic': 'code', 'value': 'B', 'supersedes_decision_id': 'a'}], first.state)
    report = second.outputs['report']
    assert report['retired_since_previous_complete_snapshot'] == ['a']
    assert report['replacements'] == [{'previous': 'a', 'current': 'b'}]
    assert set(second.state['known']) == {'b'} and not report['authority_granted']
    assert [n for n, _ in calls] == ['decision.read', 'context.project']


def test_partial_page_does_not_retire_missing_records_or_replace_complete_snapshot():
    first, _ = run([{'decision_id': 'a', 'topic': 'code', 'value': 'A'}])
    second, _ = run([], first.state, partial=True)
    assert second.outputs['report']['partial'] and not second.outputs['report']['retired_since_previous_complete_snapshot']
    assert second.state['known'] == first.state['known']


@pytest.mark.parametrize('source', ['service', 'state'])
def test_foreign_task_fails_before_projection(source):
    with pytest.raises(ValueError, match='SCOPE_CHANGED'):
        run([], {'task_id': 'another'} if source == 'state' else {}, task='another' if source == 'service' else 't')


def test_missing_projection_service_blocks_initialization_without_callbacks(tmp_path_factory):
    from backend.core.storage import StorageRuntime
    from backend.core.mechanism_runtime.registry import Registry
    from backend.core.mechanism_runtime.components import install
    from backend.core.mechanism_runtime.repository import Repository
    from backend.core.mechanism_runtime.coordinator import Coordinator
    registry = Registry()
    install(registry)
    component['install'](registry)
    work = tmp_path_factory / 'lineage-work'
    work.mkdir()
    with StorageRuntime(work, state_home=tmp_path_factory / 'lineage-state') as storage:
        repo = Repository(storage)
        compiler = Coordinator(registry, repo, capabilities=('decision.read', 'context.project'))
        program = {'schema_version': 2, 'id': 'unit-lineage', 'intent': 'SDK initialization failure fixture',
            'source_refs': ['unit:fixture'], 'elements': [
                {'id': 'event', 'use': 'runtime.event', 'parameters': {'boundaries': ['restored']}},
                {'id': 'audit', 'use': 'decision.lineage_audit'}],
            'bindings': [{'kind': 'trigger', 'from': {'node': 'event', 'port': 'go'}, 'to': {'node': 'audit', 'port': 'go'}}]}
        definition, ref = repo.publish(compiler.preview(program)['compiled'], {'unit:fixture': {'kind': 'unit_fixture'}}, expected_head=None)
        repo.create_instance('task', 'unit', definition, ref, policy='pinned', sources=definition['sources'])
        owner = Coordinator(registry, repo, capabilities=('decision.read',))
        owner.request('task', 'unit', ref, expected_revision=1, sources=definition['sources'])
        with pytest.raises(ValueError, match='ACTIVATION_SERVICE_MISSING'):
            owner.settle('task', 'unit')
        result = repo.instance('task', 'unit')[0]
        assert result['phase'] == 'blocked' and result['epoch'] == 0 and not result['runs'] and not result['states']
