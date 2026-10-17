"""Finite checkpoint input and retained-history admission before file effects."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.core.storage import StorageBlocked, StorageRuntime
from backend.core.storage.checkpoint_limits import CheckpointLimits, capture_footprint


def limits():
    return CheckpointLimits(input_nodes=256,input_depth=16,input_bytes=8192,
        scalar_bytes=1024,sql_statements=100,sql_parameter_bytes=16384,
        cas_objects=8,cas_bytes=16384,history_rows=32,reference_rows=128,replacement_rows=64)


def test_copy_is_detached_and_shared_subtrees_are_charged_each_time():
    common = {'role':'user','content':'hello'}
    snapshot = {'messages':[common,common]}
    copied = limits().copy_snapshot(snapshot)
    assert copied == snapshot
    assert copied['messages'][0] is not copied['messages'][1]
    common['content'] = 'changed'
    assert copied['messages'][0]['content'] == 'hello'
    with pytest.raises(StorageBlocked,match='input_nodes'):
        replace(limits(),input_nodes=10).copy_snapshot(snapshot)


def test_empty_container_fanout_is_not_free():
    with pytest.raises(StorageBlocked,match='input_nodes'):
        limits().copy_snapshot({'messages':[{}]*257})
    with pytest.raises(StorageBlocked,match='input_bytes'):
        replace(limits(),input_bytes=64,scalar_bytes=32).copy_snapshot({'messages':[{}]*10})


def test_cycles_and_deep_aliases_fail_without_recursion_failure():
    cycle = {}
    cycle['cycle'] = cycle
    with pytest.raises(StorageBlocked,match='input_cycle'): limits().copy_snapshot(cycle)
    value = []
    for _ in range(18): value = [value]
    with pytest.raises(StorageBlocked,match='input_depth'): limits().copy_snapshot({'x':value})


@pytest.mark.parametrize('value',[float('nan'),float('inf'),1 << 63,-(1 << 63)-1,b'bytes',object(),{'set'},'\ud800'])
def test_invalid_json_or_unbounded_scalar_fails_before_callbacks(value):
    with pytest.raises(StorageBlocked): limits().copy_snapshot({'value':value})


def test_no_adapters_deepcopy_iterators_or_mapping_subclasses_are_called():
    calls = []
    class Custom(dict):
        def __deepcopy__(self,memo): calls.append('copy'); return {}
        def items(self): calls.append('items'); return []
    with pytest.raises(StorageBlocked): limits().copy_snapshot(Custom())
    with pytest.raises(StorageBlocked): limits().copy_snapshot({'nested':Custom()})
    with pytest.raises(StorageBlocked): limits().copy_snapshot({1:'key'})
    assert not calls


def test_utf8_bytes_and_integer_boundaries_are_exact():
    profile = replace(limits(),scalar_bytes=12)
    assert profile.copy_snapshot({'x':'界'*4}) == {'x':'界'*4}
    with pytest.raises(StorageBlocked,match='scalar_bytes'): profile.copy_snapshot({'x':'界'*5})
    values = [None,False,True,-(1 << 63),(1 << 63)-1,1.25]
    assert limits().copy_snapshot({'x':tuple(values)}) == {'x':tuple(values)}


@pytest.mark.parametrize('field',list(CheckpointLimits.__dataclass_fields__))
@pytest.mark.parametrize('value',[False,0,-1,1 << 31])
def test_each_limit_must_be_a_finite_positive_integer(field,value):
    with pytest.raises(StorageBlocked): replace(limits(),**{field:value})


def test_depth_and_scalar_budget_cannot_escape_enclosing_bounds():
    with pytest.raises(StorageBlocked): replace(limits(),input_depth=65)
    with pytest.raises(StorageBlocked): replace(limits(),scalar_bytes=8193)


@pytest.mark.parametrize('kind',['statements','parameters','objects','cas','scalar'])
def test_publication_has_independent_limits_even_after_input_validation(kind):
    profile = limits()
    statement = SimpleNamespace(parameters=('ok',))
    sql = SimpleNamespace(statements=(statement,),parameter_bytes=2)
    objects = SimpleNamespace(members=(object(),),byte_length=2)
    if kind == 'statements': sql.statements = (statement,)*101
    if kind == 'parameters': sql.parameter_bytes = 16385
    if kind == 'objects': objects.members = (object(),)*9
    if kind == 'cas': objects.byte_length = 16385
    if kind == 'scalar': statement.parameters = ('界'*400,)
    with pytest.raises(StorageBlocked): profile.require_publication(sql,objects)


def test_actual_schema_old_rows_and_history_are_checked_without_changing_them(tmp_path_factory):
    root = tmp_path_factory/'work'
    root.mkdir()
    with StorageRuntime(root,state_home=tmp_path_factory/'state') as runtime:
        runtime.save_agent_checkpoint(dict(process_id='p',session_id='s',task_id='t',status='running',
            messages=[{'role':'user','content':str(i)} for i in range(8)]))
        before = runtime._state.total_changes
        footprint = capture_footprint(runtime._state,limits(),'p','s')
        assert footprint.messages == 8 and footprint.total_facts == 1
        assert footprint.checkpoint_references == 12 and footprint.total_references == 24
        for field,value in [('replacement_rows',7),('reference_rows',23)]:
            with pytest.raises(StorageBlocked,match='existing_rows'):
                capture_footprint(runtime._state,replace(limits(),**{field:value}),'p','s')
        # Unrelated histories still consume the shared store envelope.
        for index in range(33): runtime.append_session_event('other','progress',{'n':index})
        changed = runtime._state.total_changes
        with pytest.raises(StorageBlocked,match='existing_rows'):
            capture_footprint(runtime._state,limits(),'p','s')
        assert runtime._state.total_changes == changed and changed > before
        assert runtime._state.execute('SELECT COUNT(*) FROM session_events').fetchone()[0] == 33
