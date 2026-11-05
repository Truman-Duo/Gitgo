"""Framing diagnostics and stable descriptions never grant execution rights."""
from copy import deepcopy
from types import SimpleNamespace

from backend.core.loop.provider_context import ContextSections, decode_records, durable_cache
from backend.core.loop.provider_tool_transport import reference_catalog, argument_error_guidance
from backend.core.loop.provider_progress import ProviderProgress
from backend.core.loop.session import AgentSession
from tests.test_provider_tool_transport import tool, collector
from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType


def frame(session, tools, scope='task'):
    names = [(t.get('function') or t)['name'] for t in tools]
    context = ContextSections('', {'runtime.tool_contracts':reference_catalog(session, tools, scope=scope),
        'runtime.capability':{'available_tools':names}}, scope=scope)
    return session.to_provider_messages(dynamic_envelope=context)


def test_descriptions_persist_while_availability_retires_and_reactivation_does_not_resend_schema():
    session = AgentSession()
    a, b = tool('read'), tool('write')
    frame(session, [a, b])
    session.append_assistant('actual prior model result')
    retired = frame(session, [a])
    current = decode_records(retired)
    assert current['runtime.capability']['available_tools'] == ['read']
    assert set(current['runtime.tool_contracts']) == {'read', 'write'}
    session.append_assistant('another actual result')
    restored = frame(session, [a, b])
    patch = session.provider_context_cache['records'][-1]['message']['context_snapshot']
    assert not any('/runtime.tool_contracts' in item['path'] for item in patch['set'])
    assert decode_records(restored)['runtime.capability']['available_tools'] == ['read','write']
    saved = AgentSession.from_durable_state({'messages':deepcopy(session.messages),
        'provider_context_cache':durable_cache(session.provider_context_cache),
        'provider_context_seed':deepcopy(session.provider_context_seed)})
    assert frame(saved,[a,b]) == restored
    replacement = deepcopy(a)
    replacement['function']['parameters']['properties']['new_field'] = {'type':'boolean'}
    session.append_assistant('before schema update')
    changed = decode_records(frame(session,[replacement]))
    assert changed['runtime.tool_contracts']['read'] == replacement['function']
    assert changed['runtime.capability']['available_tools'] == ['read']
    assert reference_catalog(session, [a], scope='other-task') == {'read':a['function']}


def test_inactive_descriptions_are_bounded_without_dropping_any_live_schema():
    session = AgentSession()
    old = [tool('old'+str(i)) for i in range(200)]
    frame(session, old)
    value = reference_catalog(session, [tool('live')], scope='task')
    assert len(value) == 129 and 'live' in value


def test_transport_error_is_not_reported_as_json_syntax_or_silently_executed():
    c,state,_,_=collector()
    c.consume(ProviderEvent(ProviderEventType.TOOL_CALL_DONE, tool_call_id='bad', tool_name='gitgo_call',
        arguments='{"arguments":{"verdict":"changes_required"}}', output_index=0))
    c.decode_tool_calls()
    assert not state.tool_calls and state.invalid_tool_calls[0]['error_code'] == 'INVALID_TOOL_TRANSPORT'
    session=AgentSession()
    guard_calls=[]
    guard=SimpleNamespace(record_tool_result=lambda *a,**kw:guard_calls.append((a,kw)),
        record_tool_error=lambda *a,**kw:guard_calls.append((a,kw)))
    progress=ProviderProgress(SimpleNamespace(),session,guard,lambda *a:None)
    assert progress.handle(state,1).retry
    message=session.messages[-1]['content']
    assert 'Both name and arguments are required' in message
    assert 'malformed JSON' not in message
    assert any('INVALID_TOOL_TRANSPORT' in str(c) for c in guard_calls)


def test_redundant_wrapper_guidance_never_normalizes_business_arguments():
    args={'name':'complete_review','arguments':{'verdict':'changes_required'}}
    original=deepcopy(args)
    schema={'properties':{'verdict':{'type':'string'}},'required':['verdict']}
    assert 'extra transport envelope' in argument_error_guidance('complete_review',args,schema)
    assert args == original
    assert not argument_error_guidance('another_tool',args,schema)
    assert not argument_error_guidance('complete_review',args,{'properties':{'name':{},'arguments':{}}})
