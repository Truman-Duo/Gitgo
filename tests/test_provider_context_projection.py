"""Context reuse preserves exact state, revocations and native transactions."""
from copy import deepcopy
import json

import pytest

from backend.core.loop.provider_context import ContextSections, decode_records, durable_cache
from backend.core.loop.provider_protocol import deterministic_hash
from backend.core.loop.session import AgentSession


def view(data, scope='task-a'):
    return ContextSections('legacy complete snapshot', data, scope=scope)


def decode_wire(messages):
    # The Provider sees content, not the diagnostic metadata beside it.
    copied = deepcopy(messages)
    for m in copied:
        if m.get('message_type') == 'host_context_seed':
            parsed=json.loads(m['content'][m['content'].index('{'):])
            assert parsed['values'] == m['context_seed']
        if m.get('message_type') == 'host_context_update':
            start = m['content'].index('{')
            parsed = json.loads(m['content'][start:])
            assert parsed == m['context_snapshot']
            m['context_snapshot'] = parsed
    result = decode_records(copied)
    marker = json.loads(copied[-1]['content'].split('\n',1)[1])
    assert marker['snapshot_digest'] == deterministic_hash(result)
    return result


def test_unchanged_context_is_reused_before_actual_response_without_transcript_growth():
    s = AgentSession()
    s.append_system('stable')
    s.append_user('actual user')
    data = {'contract':{'goal':'保持所有要求', 'value':None}, 'mode':'auto'}
    first = s.to_provider_messages(dynamic_envelope=view(data))
    assert first == s.to_provider_messages(dynamic_envelope=view(data))
    before = deepcopy(s.messages)
    s.append_assistant('actual response')
    second = s.to_provider_messages(dynamic_envelope=view(data))
    assert second[:len(first)-1] == first[:-1]
    assert decode_wire(first) == decode_wire(second) == data
    assert s.messages == before + [{'role':'assistant','content':'actual response'}]
    assert len(s.provider_context_cache['records']) == 1


@pytest.mark.parametrize('value', [None,False,0,[],{},'中文\n~/',{'a/b':{'~x': [1,False,None]}}])
def test_changes_are_lossless_including_removals_and_type_changes(value):
    s = AgentSession()
    old = {'active':{'goal':'original','withdrawn':'not current'}, 'projections':{'old':'disabled'}, 'value':True}
    s.to_provider_messages(dynamic_envelope=view(old))
    s.append_assistant('previous result')
    current = {'active':{'goal':'revised'}, 'projections':{}, 'value':value}
    wire = s.to_provider_messages(dynamic_envelope=view(current))
    assert decode_wire(wire) == current
    assert len(s.provider_context_cache['records']) == 2
    assert current == {'active':{'goal':'revised'}, 'projections':{}, 'value':value}


def test_unstarted_boundary_replaces_intermediate_views_and_restore_retains_prefix():
    s = AgentSession()
    s.to_provider_messages(dynamic_envelope=view({'goal':'first'}),provider_route='r')
    wire = s.to_provider_messages(dynamic_envelope=view({'goal':'latest','permissions':[]}),provider_route='r')
    assert len(s.provider_context_cache['records']) == 1
    assert decode_wire(wire) == {'goal':'latest','permissions':[]}
    restored = AgentSession.from_durable_state({'session_id':s.session_id,
        'provider_context_cache':durable_cache(s.provider_context_cache),
        'provider_context_seed':deepcopy(s.provider_context_seed)})
    assert restored.to_provider_messages(dynamic_envelope=view({'goal':'latest','permissions':[]}),provider_route='r') == wire
    restored.append_assistant('real response')
    next_wire = restored.to_provider_messages(dynamic_envelope=view({'goal':'last'}),provider_route='r')
    assert next_wire[:len(wire)-1] == wire[:-1]
    assert decode_wire(next_wire) == {'goal':'last'}


@pytest.mark.parametrize('change',['scope','route','epoch','history','cache_corrupt'])
def test_changed_boundaries_rebase_from_current_inputs_not_stale_cache(change):
    s = AgentSession()
    s.append_user('task')
    s.to_provider_messages(dynamic_envelope=view({'mode':'manual','old':'value'}),provider_route='a')
    scope, route = 'task-a','a'
    if change=='scope':scope='new-task'
    if change=='route':route='b'
    if change=='epoch':s.context_epoch+=1
    if change=='history':s.messages[0]['content']='new clean input'
    if change=='cache_corrupt':s.provider_context_cache['records'][0]['message']['context_snapshot']['set'][0]['value']='corrupt'
    wire = s.to_provider_messages(dynamic_envelope=view({'mode':'auto'},scope),provider_route=route)
    assert decode_wire(wire) == {'mode':'auto'}
    assert len(s.provider_context_cache['records']) == 1
    assert s.provider_context_cache['records'][0]['message']['context_snapshot']['reset']


def test_parallel_tool_batch_remains_complete_before_context_delta():
    s = AgentSession()
    first = s.to_provider_messages(dynamic_envelope=view({'mode':'auto'}))
    calls=[{'id':k,'function':{'name':'read_file','arguments':'{}'}} for k in ('one','two')]
    s.append_assistant_provider('',tool_calls=calls)
    s.append_tool_result('one',tool_name='read_file',tool_call_id='one')
    s.append_host_steering('new actual control',steering_type='advancement')
    s.append_tool_result('two',tool_name='read_file',tool_call_id='two')
    wire=s.to_provider_messages(dynamic_envelope=view({'mode':'manual'}))
    assert wire[:len(first)-1] == first[:-1]
    assistant=next(i for i,m in enumerate(wire) if m['role']=='assistant')
    assert [m.get('tool_call_id') for m in wire[assistant+1:assistant+3]] == ['one','two']
    assert decode_wire(wire) == {'mode':'manual'}
    legacy=s.to_openai_messages(dynamic_envelope=view({'mode':'manual'}))
    assert [m['content'] for m in legacy] == [m['content'] for m in wire]


def test_bounded_cache_rebases_without_losing_current_context_and_is_accounted():
    s=AgentSession()
    for n in range(132):
        wire=s.to_provider_messages(dynamic_envelope=view({'value':n}))
        s.append_assistant(str(n))
    assert decode_wire(wire) == {'value':131}
    assert len(s.provider_context_cache['records']) < 128
    breakdown=s.context_breakdown()
    assert any(row['name']=='Runtime context updates' for row in breakdown['sections'])
    assert s.estimate_tokens() > sum(len(m['content']) for m in s.messages)//4


def seeded_view(catalog,goal='business',scope='task-a'):
    return view({'runtime.tool_contracts':catalog,'runtime.tool_transport':'stable framing',
        'runtime.maintenance':{'goal':goal,'mode':'auto'}},scope)


def test_catalog_baseline_is_shared_before_unique_task_data_without_hiding_current_state():
    sessions=[]
    for scope in ['task-a','task-b']:
        s=AgentSession()
        s.append_system('same immutable policy',message_type='compiled_system_prompt')
        s.append_user('different actual request '+scope)
        wire=s.to_provider_messages(dynamic_envelope=seeded_view({'read':{'name':'read'}},goal=scope,scope=scope))
        assert wire[1]['message_type']=='host_context_seed'
        assert set(wire[1]['context_seed']) == {'runtime.tool_contracts','runtime.tool_transport'}
        assert decode_wire(wire)['runtime.maintenance']['goal']==scope
        assert s.messages[1]['content']=='different actual request '+scope
        sessions.append(wire)
    assert sessions[0][:2] == sessions[1][:2]
    assert sessions[0][2:] != sessions[1][2:]


def test_compaction_reuses_baseline_and_updates_live_catalog_with_exact_removal():
    s=AgentSession()
    s.append_system('policy',message_type='compiled_system_prompt')
    s.append_user('goal')
    first=s.to_provider_messages(dynamic_envelope=seeded_view({'old':{'name':'old'},'read':{'name':'read'}}))
    s.begin_context_epoch(summary='actual checkpoint',retained_messages=[],reason='test')
    second=s.to_provider_messages(dynamic_envelope=seeded_view({'read':{'name':'read'},'new':{'name':'new'}}))
    assert first[1]==second[1]
    assert set(decode_wire(second)['runtime.tool_contracts']) == {'read','new'}
    assert 'old' not in decode_wire(second)['runtime.tool_contracts']


def test_durable_cache_stores_patch_text_once_and_restores_the_same_provider_prefix():
    from backend.core.loop.manager import SessionStore
    s=AgentSession()
    s.append_system('policy',message_type='compiled_system_prompt')
    s.append_user('request')
    catalog={'read':{'name':'read','description':'unchanged definition '*1000}}
    s.to_provider_messages(dynamic_envelope=seeded_view(catalog))
    s.append_assistant('real result')
    wire=s.to_provider_messages(dynamic_envelope=seeded_view(catalog,goal='updated'))
    metadata=SessionStore._session_metadata(s)
    cache=metadata['provider_context_cache']
    assert 'snapshot' not in cache and 'seed_message' not in cache
    assert all('context_snapshot' not in r['message'] for r in cache['records'])
    assert len(json.dumps(cache))+len(json.dumps(metadata['provider_context_seed'])) < len(json.dumps(s.provider_context_cache))
    restored=AgentSession.from_durable_state({**metadata,'messages':deepcopy(s.messages)})
    assert restored.to_provider_messages(dynamic_envelope=seeded_view(catalog,goal='updated'))==wire
    assert decode_wire(wire)==decode_wire(restored.to_provider_messages(dynamic_envelope=seeded_view(catalog,goal='updated')))


def test_corrupt_optional_cache_rebuilds_from_actual_inputs_and_never_changes_authority():
    from backend.core.loop.manager import SessionStore
    s=AgentSession()
    data=seeded_view({'read':{'name':'read'}})
    s.to_provider_messages(dynamic_envelope=data)
    metadata=deepcopy(SessionStore._session_metadata(s))
    metadata['provider_context_cache']['records'][0]['message']['content']='damaged derived cache'
    restored=AgentSession.from_durable_state(metadata)
    assert restored.provider_context_cache=={}
    wire=restored.to_provider_messages(dynamic_envelope=data)
    assert decode_wire(wire)==data.sections
    assert restored.host_ledger==s.host_ledger==[]


def test_no_business_values_can_be_inherited_via_a_reused_baseline():
    s=AgentSession()
    s.to_provider_messages(dynamic_envelope=seeded_view({'read':{'name':'read'}},goal='old private answer'))
    s.append_user('new unrelated task')
    wire=s.to_provider_messages(dynamic_envelope=seeded_view({'read':{'name':'read'}},goal='new task',scope='task-b'))
    assert decode_wire(wire)['runtime.maintenance']=={'goal':'new task','mode':'auto'}
    assert 'old private answer' not in json.dumps(wire,ensure_ascii=False)
