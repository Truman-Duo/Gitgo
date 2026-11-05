"""Synthetic SDK component fixtures; not real API or human semantic grading."""
from pathlib import Path
import sys
import json
import time
import pytest

from backend.core.mechanism_runtime.extensions.decision_recovery import recover
from backend.core.mechanism_runtime.runner import Context
from backend.core.mechanism_runtime.registry import Registry
from backend.core.mechanism_runtime.components import install as builtins
from backend.core.mechanism_runtime.compiler import compile_program


def test_registration_uses_existing_typed_graph_and_no_extra_runtime_service():
    registry = Registry()
    builtins(registry)
    program = compile_program({'schema_version': 2, 'id': 'component-fixture', 'intent': 'Synthetic fixture only',
        'source_refs': ['unit-fixture:not-user-evidence'], 'elements': [
            {'id': 'event', 'use': 'runtime.event', 'parameters': {'boundaries': ['restored']}},
            {'id': 'recovery', 'use': 'decision.recovery'}],
        'bindings': [{'kind': 'trigger', 'from': {'node': 'event', 'port': 'go'}, 'to': {'node': 'recovery', 'port': 'go'}}]},
        registry, capabilities=('decision.read', 'context.inspect', 'context.project'))
    assert program['order'] == ['event', 'recovery']


def test_current_decisions_project_once_and_stale_links_never_restore_old_values():
    current = [{'topic': 'unit.compatibility', 'decision_id': 'unit-decision-one', 'value': 'Keep the signature',
                'disposition': 'confirmed', 'conditions': 'Unit fixture', 'kind': 'preference'}]
    messages, projected = [], []
    def decisions(ctx, payload):
        return {'task_id': 'unit-task', 'confirmed': current, 'next_offset': None}
    def inspect(ctx, payload):
        return {'task_id': 'unit-task', 'messages': messages, 'next_offset': None}
    def project(ctx, payload):
        projected.append(payload)
        messages[:] = [{'message_type': 'host_dynamic_envelope', 'content': json.dumps({'projections': [payload]}, ensure_ascii=False)}]
        return {'unit_fixture': True}
    def context(state, boundary='restored'):
        return Context('unit-project', 'unit-task', 'unit-instance', 1, 'unit-event', boundary, 'unit-node', state,
            {'decision.read': decisions, 'context.inspect': inspect, 'context.project': project}, lambda: False, time.monotonic() + 5)
    first = recover(context({}), {'topics': []}, {'go': True})
    assert len(projected) == 1 and first.outputs['report']['decision_count'] == 1
    second = recover(context(first.state, 'after_compaction'), {'topics': []}, {'go': True})
    assert len(projected) == 1 and second.outputs['report']['projection_was_present']
    current[0] = {**current[0], 'value': 'Retain the function name, revise its documented condition'}
    revised = recover(context(second.state), {'topics': []}, {'go': True})
    assert len(projected) == 2 and revised.outputs['report']['outdated_count'] == 1
    assert 'Keep the signature' not in projected[-1]['content']
    current[:] = []
    third = recover(context(revised.state), {'topics': []}, {'go': True})
    assert len(projected) == 3 and third.outputs['report']['outdated_count'] == 1
    assert 'unit-decision-one' in projected[-1]['content'] and 'Keep the signature' not in projected[-1]['content']
    assert 'current_confirmed_decisions":[]' in projected[-1]['content']


def test_scope_mismatch_fails_without_projection():
    def decisions(ctx, payload):
        return {'task_id': 'another-task', 'confirmed': [], 'next_offset': None}
    ctx = Context('unit-project', 'unit-task', 'unit-instance', 1, 'unit-event', 'restored', 'unit-node', {},
        {'decision.read': decisions}, lambda: False, time.monotonic() + 5)
    with pytest.raises(ValueError, match='RECOVERY_SCOPE_CHANGED'):
        recover(ctx, {'topics': []}, {'go': True})
