from types import SimpleNamespace

import pytest

from backend.core.mechanism_runtime.extensions.context_audit import audit, install
from backend.core.mechanism_runtime.registry import Registry


def test_scope_stamp_uses_public_assembly_inputs_without_copying_business_content():
    effects = []
    def call(service, payload):
        if service == 'context.inspect':
            return {'task_id': 'task-a', 'context_epoch': 3, 'stage': 'pre_request_assembly_input',
                'assembly_digest': 'real-input-digest', 'total': 19, 'messages': [{'content': 'do not copy me'}]}
        effects.append((service, payload))
        return {}
    ctx = SimpleNamespace(task_id='task-a', epoch=2, boundary='restored', event_id='real-checkpoint', state={'checks': 4}, call=call)
    result = audit(ctx, {'label': 'fixture'}, {})
    assert result.state['checks'] == 5
    assert result.outputs['report']['assembly_digest'] == 'real-input-digest'
    assert result.outputs['report']['context_epoch'] == 3
    assert effects[0][0] == 'context.project' and 'do not copy me' not in effects[0][1]['content']
    assert result.outputs['report']['authority_granted'] is False


def test_wrong_scope_fails_before_projection():
    ctx = SimpleNamespace(task_id='task-a', call=lambda *a: {'task_id': 'task-b'})
    with pytest.raises(ValueError, match='SCOPE_MISMATCH'):
        audit(ctx, {'label': 'fixture'}, {})


def test_registration_requires_only_existing_declared_capabilities():
    registry = Registry()
    for name, schema in {'trigger': {'const': True}, 'record': {'type': 'object'}}.items():
        registry.register_type(name, schema)
    handle = install(registry)
    descriptor = registry.resolve('context.boundary_audit', 1)
    assert set(descriptor['capabilities']) == {'context.inspect', 'context.project'}
    assert set(descriptor['boundaries']) == {'restored', 'after_compaction'}
    assert callable(handle)
