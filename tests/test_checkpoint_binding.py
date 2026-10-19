"""Actual repository plans reject detached nested values before any effects."""
from dataclasses import replace
import hashlib

import pytest

from backend.core.storage import StorageBlocked, StorageRuntime
from backend.core.storage.checkpoint_plan import (
    CheckpointBasis, CheckpointObject, CheckpointObjects, ProcessBasis, TerminalBasis,
)
from backend.core.storage.checkpoint_sql import CheckpointSqlBuilder, CheckpointStatement
from backend.core.storage.repositories.checkpoints import plan_agent_checkpoint
from tests.test_checkpoint_work import profile, snapshot


@pytest.fixture
def planned(tmp_path_factory, monkeypatch):
    workspace = tmp_path_factory/'work'
    workspace.mkdir()
    with StorageRuntime(workspace, state_home=tmp_path_factory/'state') as runtime:
        plan = plan_agent_checkpoint(runtime, snapshot(), limits=profile())
        before = runtime._state.total_changes
        def forbidden(*args, **kwargs):
            pytest.fail('invalid publication reached file or SQL effects')
        monkeypatch.setattr(runtime, '_write_blob_file', forbidden)
        yield runtime, plan
        assert runtime._state.total_changes == before
        assert not list(runtime.paths.cas_dir.rglob('*'))


@pytest.mark.parametrize('change', ['basis', 'object_bytes', 'object_time', 'object_media',
                                  'object_order', 'sql', 'parameters', 'expected_rows',
                                  'process_id', 'parent_process_id', 'claimed_digest'])
def test_detached_values_cannot_reuse_a_repository_digest(planned, change):
    _, plan = planned
    updates = {}
    if change == 'basis':
        updates['basis'] = replace(plan.basis, event_sequence=plan.basis.event_sequence+1)
    elif change.startswith('object'):
        members = list(plan.objects.members)
        member = members[0]
        if change == 'object_bytes':
            data = member.data+b' '
            members[0] = replace(member, data=data, digest=hashlib.sha256(data).hexdigest())
        elif change == 'object_time':
            members[0] = replace(member, created_at='different')
        elif change == 'object_media':
            members[0] = replace(member, media_type='text/plain')
        else:
            members.reverse()
        updates['objects'] = CheckpointObjects(tuple(members), sum(len(m.data) for m in members))
    elif change in {'sql', 'parameters', 'expected_rows'}:
        statements = list(plan.writes.statements)
        if change == 'sql':
            statements[0] = replace(statements[0], sql=statements[0].sql+' ')
        elif change == 'parameters':
            # Equal UTF-8 length prevents a byte-cost mismatch from masking the
            # required content binding. The first real session insert owns p/s.
            parameters = list(statements[0].parameters)
            index = next(i for i,v in enumerate(parameters) if type(v) is str and v)
            parameters[index] = 'x'*len(parameters[index].encode('utf8'))
            assert tuple(parameters) != statements[0].parameters
            statements[0] = replace(statements[0], parameters=tuple(parameters))
        else:
            statements[0] = replace(statements[0], expected_rows=0)
        updates['writes'] = replace(plan.writes, statements=tuple(statements))
    elif change == 'claimed_digest':
        updates['writes'] = replace(plan.writes, digest='0'*64)
    else:
        updates[change] = 'different'
    with pytest.raises(StorageBlocked, match='checkpoint_sql_binding_mismatch'):
        replace(plan, **updates)


@pytest.mark.parametrize('change', [
    {'data':bytearray(b'{}')}, {'data':memoryview(b'{}')}, {'digest':'0'*64},
    {'data':b'[]'}, {'created_at':['mutable']}, {'media_type':['mutable']},
    {'created_at':'\ud800'},
])
def test_object_constructor_requires_exact_immutable_bytes_and_hash(change):
    values = dict(data=b'{}', digest=hashlib.sha256(b'{}').hexdigest(),
                  created_at='time', media_type='application/json')
    values.update(change)
    with pytest.raises(StorageBlocked, match='checkpoint_object_invalid'):
        CheckpointObject(**values)


@pytest.mark.parametrize('change', ['mutable', 'duplicate', 'cost', 'boolean', 'member'])
def test_closure_cost_and_unique_members_are_derived_from_actual_bytes(planned, change):
    _, plan = planned
    if change == 'mutable': updates = {'members':list(plan.objects.members)}
    elif change == 'duplicate':
        member = plan.objects.members[0]
        updates = {'members':(member,member), 'byte_length':2*len(member.data)}
    elif change == 'cost': updates = {'byte_length':0}
    elif change == 'boolean': updates = {'byte_length':True}
    else: updates = {'members':({'data':b'{}'},)}
    with pytest.raises(StorageBlocked, match='checkpoint_objects_invalid'):
        replace(plan.objects, **updates)


@pytest.mark.parametrize('changes', [
    {'process':[]}, {'terminal':[]}, {'event_sequence':True}, {'event_count':-1},
    {'terminal_count':1<<63}, {'deleted':0}, {'parent_task_id':[]},
    {'process':ProcessBasis('p','s',None,None,[], 'running',None)},
    {'terminal':TerminalBasis('e',[], 'sha256:ref','time')},
    {'process':ProcessBasis('p','s',None,None,0,[],None)},
])
def test_basis_cannot_hide_mutable_or_unrepresentable_dependencies(changes):
    values = dict(process=None,event_sequence=0,event_count=0,terminal=None,
                  terminal_count=0,deleted=False,parent_task_id=None)
    values.update(changes)
    with pytest.raises(StorageBlocked, match='checkpoint_basis_invalid'):
        CheckpointBasis(**values)


@pytest.mark.parametrize('parameters', [[], ([],), ({},), (bytearray(b'x'),),
                                       (True,), (float('nan'),), (float('inf'),), (1<<63,)])
def test_statement_requires_adapter_free_immutable_sql_values(parameters):
    with pytest.raises(StorageBlocked, match='checkpoint_sql_'):
        CheckpointStatement('SELECT ?', parameters)


@pytest.mark.parametrize('expected_rows', [True, -1, 1<<63, [], '1'])
def test_statement_row_expectation_is_an_exact_count(expected_rows):
    with pytest.raises(StorageBlocked, match='checkpoint_sql_statement_invalid'):
        CheckpointStatement('SELECT 1', (), expected_rows)


@pytest.mark.parametrize('change', ['statements', 'parameter_bytes', 'boolean_cost',
                                  'footprint', 'outer_basis', 'outer_objects', 'outer_sql',
                                  'outer_process'])
def test_nested_containers_and_costs_cannot_bypass_publication_binding(planned, change):
    _, plan = planned
    with pytest.raises(StorageBlocked):
        if change == 'statements': replace(plan.writes, statements=list(plan.writes.statements))
        elif change == 'parameter_bytes': replace(plan.writes, parameter_bytes=0)
        elif change == 'boolean_cost': replace(plan.writes, parameter_bytes=True)
        elif change == 'footprint': replace(plan.footprint, total_events=[])
        elif change == 'outer_basis': replace(plan, basis={})
        elif change == 'outer_objects': replace(plan, objects=[])
        elif change == 'outer_sql': replace(plan, writes={})
        else: replace(plan, process_id=[])


def test_identical_reconstruction_preserves_binding_but_new_basis_requires_reseal(planned):
    _, plan = planned
    clone = replace(plan, basis=replace(plan.basis),
                    objects=replace(plan.objects), writes=replace(plan.writes))
    assert clone.digest == plan.digest
    changed = replace(plan.basis, event_sequence=1)
    builder = CheckpointSqlBuilder()
    for statement in plan.writes.statements:
        builder._append(statement.sql, statement.parameters, statement.expected_rows)
    rebound = builder.seal(changed, plan.objects, plan.process_id, plan.parent_process_id)
    assert replace(plan, basis=changed, writes=rebound).digest != plan.digest


def test_subclass_adapters_are_rejected_without_invoking_user_callbacks():
    calls = []
    class Adapted(str):
        def encode(self, *args, **kwargs):
            calls.append('encode')
            raise AssertionError('untrusted callback')
        def __conform__(self, protocol):
            calls.append('adapt')
            raise AssertionError('untrusted callback')
    with pytest.raises(StorageBlocked, match='checkpoint_sql_scalar_invalid'):
        CheckpointStatement('SELECT ?', (Adapted('value'),))
    with pytest.raises(StorageBlocked, match='checkpoint_object_invalid'):
        CheckpointObject(b'{}', hashlib.sha256(b'{}').hexdigest(), Adapted('time'))
    assert calls == []
