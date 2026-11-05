"""Stable framing must preserve native replay and original policy failures."""
import json
from types import SimpleNamespace

import pytest

from backend.core.loop.provider_tool_transport import CALL, catalog, decode, wire_tools
from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType, ProviderRequest
from backend.core.loop.provider_adapters import OpenAIChatAdapter, OpenAIResponsesAdapter, AnthropicMessagesAdapter
from backend.core.loop.provider_response import ProviderResponse, ProviderResponseCollector
from backend.core.loop.provider_turn_runtime import _to_provider_tool_calls
from backend.core.loop.session import AgentSession


def tool(name):
    return {'type':'function','function':{'name':name,'description':name,
        'parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}}


@pytest.mark.parametrize('adapter',[OpenAIChatAdapter(),OpenAIResponsesAdapter(),AnthropicMessagesAdapter()])
def test_native_schema_stays_stable_but_current_catalog_tracks_exact_surface(adapter):
    first,last=[tool('read')],[tool('write'),tool('read')]
    assert wire_tools(first)==wire_tools(last)
    request=ProviderRequest(messages=({'role':'user','content':'actual task'},),tools=tuple(wire_tools(first)))
    body=adapter.build_body(request,'model')
    native=body['tools'][0]
    assert (native.get('function') or native)['name']==CALL
    assert catalog(first)=={'read':first[0]['function']}
    assert set(catalog(last))=={'read','write'}
    assert catalog([])=={} and wire_tools([]) is None


def collector():
    state=ProviderResponse();ui=[];trace=[]
    c=ProviderResponseCollector(SimpleNamespace(task_budget=None,process_id='p'),AgentSession(),
        SimpleNamespace(protocol='openai_chat'),lambda *a,**kw:trace.append((a,kw)),ui.append,None,'call:',state)
    return c,state,ui,trace


def test_stream_unwraps_once_and_retains_exact_native_call_for_replay():
    c,state,ui,trace=collector()
    raw='{ "name": "edit_file", "arguments": {"path":"中文.py", "new_string":"a\\nb"} }'
    c.consume(ProviderEvent(ProviderEventType.TOOL_CALL_STARTED,tool_call_id='actual',tool_name=CALL,output_index=0))
    for fragment in [raw[:31],raw[31:]]:
        c.consume(ProviderEvent(ProviderEventType.TOOL_CALL_DELTA,arguments_delta=fragment,output_index=0))
    assert not ui  # Transport framing is not a product tool event.
    c.decode_tool_calls()
    call=state.tool_calls[0]
    assert call['name']=='edit_file' and call['args']=={'path':'中文.py','new_string':'a\nb'}
    wire=_to_provider_tool_calls([call])[0]
    assert wire['id']=='actual' and wire['function']['name']==CALL and wire['function']['arguments']==raw
    assert ui[0]['tool_name']=='edit_file' and all(e.get('tool_name')!=CALL for e in ui)
    assert trace[-1][0][0]['tool_name']=='edit_file'


@pytest.mark.parametrize('arguments',[{}, {'name':'x','arguments':[]}, {'name':'','arguments':{}},
    {'name':'x','arguments':{},'permission':'allow'}, {'name':True,'arguments':{}}, []])
def test_invalid_transport_never_becomes_an_executable_call(arguments):
    c,state,ui,trace=collector()
    c.consume(ProviderEvent(ProviderEventType.TOOL_CALL_DONE,tool_call_id='bad',tool_name=CALL,
        arguments=json.dumps(arguments),output_index=0))
    c.decode_tool_calls()
    assert not state.tool_calls and len(state.invalid_tool_calls)==1


def test_partial_native_transport_is_not_executable_or_replayed_as_complete():
    c,state,ui,trace=collector()
    c.consume(ProviderEvent(ProviderEventType.TOOL_CALL_STARTED,tool_call_id='partial',tool_name=CALL,output_index=0))
    c.consume(ProviderEvent(ProviderEventType.TOOL_CALL_DELTA,arguments_delta='{"name":"edit_file",',output_index=0))
    state.provider_incomplete_reason='max_output_tokens'
    c.decode_tool_calls()
    assert not state.tool_calls


def test_native_and_legacy_fallback_preserve_original_target_and_call_id():
    from backend.core.loop.llm_adapter import parse_tool_calls
    direct={'name':'read_file','id':'one','args':{'path':'a'}}
    assert decode(direct) is direct
    raw=json.dumps({'name':'read_file','arguments':{'path':'a'}})
    parsed=parse_tool_calls({'tool_calls':[{'id':'wire','function':{'name':CALL,'arguments':raw}}]})[0]
    assert parsed['name']=='read_file' and parsed['id']=='wire' and parsed['args']=={'path':'a'}
    assert _to_provider_tool_calls([parsed])[0]['function']['name']==CALL
    parsed=parse_tool_calls('<tool_call><name>gitgo_call</name><args>'+raw+'</args></tool_call>')[0]
    assert parsed['name']=='read_file' and parsed['args']=={'path':'a'}


@pytest.mark.parametrize('case',['bad_schema','denied','withdrawn'])
def test_decoded_target_still_runs_original_pipeline_boundaries(tmp_path_factory,case):
    from backend.core.loop.agent_tool import AgentTool, ApprovalMode, ToolEffect
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.execution_contract import data_broker
    from backend.core.loop.event_bus import EventBus
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.tool_execution import ToolExecution
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry
    p=AgentProcess('transport-policy','worker',RingLevel.RING_3,active_task_id='task',
        session=AgentSession(),worktree_path=str(tmp_path_factory))
    p.tool_registry=ToolRegistry(['business'])
    executed=[]
    t=AgentTool(name='business',description='test business capability',
        parameters={'type':'object','properties':{'path':{'type':'string'}},'required':['path']},
        execute=lambda a:executed.append(a),effect=ToolEffect.READ,
        approval=ApprovalMode.DENY if case=='denied' else ApprovalMode.ALLOW,
        execution_contract=data_broker('test.fixture'))
    args={} if case=='bad_schema' else {'path':'file.txt'}
    call=decode({'name':CALL,'id':'original','args':{'name':'business','arguments':args}})
    ctx=ExecutionContext(process=p,session=p.session,workspace_path=str(tmp_path_factory),event_bus=EventBus())
    if case=='withdrawn':
        result=ToolExecution('withdrawn',ctx,[call]).execute_batch({})[0]
    else:
        result=ToolPipeline().execute(call,t,ctx,'actual-boundary')
    assert result.is_error and not executed
    assert not p.approval_grants


def test_live_effect_contracts_share_catalog_deltas_without_stale_capability_grants(tmp_path_factory):
    from backend.core.loop.agent_tool import AgentTool, ToolEffect, CancellationMode
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.tool_surface_runtime import ToolSurfaceRuntime
    from backend.core.loop.provider_route_runtime import ProviderRouteRuntime
    from backend.core.loop.prompt_compiler import PromptCompiler
    from tests.test_provider_context_projection import decode_wire
    p = AgentProcess('metadata-surface', 'worker', RingLevel.RING_3, active_task_id='task',
        session=AgentSession(), worktree_path=str(tmp_path_factory))
    surface = ToolSurfaceRuntime(p, None, None, str(tmp_path_factory), None)
    read = AgentTool('read', 'Read actual data', {}, lambda _: {}, effect=ToolEffect.READ)
    write = AgentTool('write', 'Modify actual data', {}, lambda _: {}, effect=ToolEffect.WORKSPACE_WRITE,
        cancellation=CancellationMode.COOPERATIVE)
    surface.known_tools = {'read': read, 'write': write}
    surface.tools = {'read': read}
    route = ProviderRouteRuntime(p, p.session, None, 'route', None, lambda: None)
    text, sections = PromptCompiler.compile(process=p, tools=surface.tools, workspace_path=str(tmp_path_factory))
    PromptCompiler.upsert(p.session, text, sections)
    route.prepare_envelope([tool('read')], surface.discovery(['read']))
    first = p.session.to_provider_messages(dynamic_envelope=route.envelope)
    initial = decode_wire(first)
    assert initial['runtime.tool_contracts']['read']['runtime_behavior'] == {
        'effect': read.effect.value, 'cancellation': read.cancellation.value}
    p.session.append_assistant('Observed business result')
    surface.tools = {'write': write}
    text, sections = PromptCompiler.compile(process=p, tools=surface.tools, workspace_path=str(tmp_path_factory))
    PromptCompiler.upsert(p.session, text, sections)
    route.prepare_envelope([tool('write')], surface.discovery(['write']))
    second = p.session.to_provider_messages(dynamic_envelope=route.envelope)
    latest = decode_wire(second)
    assert second[:len(first)-1] == first[:-1]
    assert latest['runtime.capability']['available_tools'] == ['write']
    assert latest['runtime.tool_contracts']['read'] == initial['runtime.tool_contracts']['read']
    assert latest['runtime.tool_contracts']['write']['runtime_behavior']['effect'] == 'workspace_write'
    # No second complete effect inventory in prompt steering; the exact
    # changed tool remains available once through the original context map.
    assert not [m for m in p.session.messages if m.get('message_type') == 'host_steering_delta'
        and '## Capabilities' in m['content']]
    assert not p.approval_grants
    surface.tools = {}
    route.prepare_envelope(None, surface.discovery([]))
    p.session.append_assistant('Tool scope revoked')
    revoked = decode_wire(p.session.to_provider_messages(dynamic_envelope=route.envelope))
    assert revoked['runtime.capability']['available_tools'] == []
    assert revoked['runtime.tool_contracts']['write'] == latest['runtime.tool_contracts']['write']
    assert not surface.tools and not p.approval_grants
