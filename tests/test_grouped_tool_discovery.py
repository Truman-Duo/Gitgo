"""Real local pipeline/discovery sequences; no paid model/API experiment."""
from copy import deepcopy
import json

from backend.core.loop.agent_tool import AgentTool, ApprovalMode
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.executor import _build_internal_tools
from backend.core.loop.provider_route_runtime import ProviderRouteRuntime
from backend.core.loop.provider_context import decode_records
from backend.core.loop.tool_context import required_tool_context, tool_definition
from backend.core.loop.tool_discovery import INITIAL_CONTRACT_CHARS, INITIAL_CATALOG_CHARS, discover, encode
from backend.core.loop.tool_surface import _select_authorized_tools
from tests.test_mechanism_interactions import runtime


def test_compact_question_contract_is_not_crowded_out_by_file_interfaces(runtime):
    from backend.core.loop.tool_discovery import project_catalog
    _, process, _, _, _, batch, _ = runtime
    internal = _build_internal_tools(process, {}, str(batch.workspace_path))
    live = {name: tool_definition(internal[name]) for name in ('capability_status', 'request_user_decision')}
    for name in ('read_file', 'list_files', 'search_text'):
        live[name] = {'name': name, 'description': 'File interface details. ' * 60,
            'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}
    view = project_catalog(live)
    assert view['runtime.tool_contracts']['request_user_decision'] == live['request_user_decision']
    assert 'detail_state' not in view['runtime.tool_contracts']['request_user_decision']
    assert len(encode(view['runtime.tool_contracts'])) <= INITIAL_CONTRACT_CHARS
    assert len(encode(view)) <= INITIAL_CATALOG_CHARS


def test_live_completion_contract_needs_no_discovery_round_before_delivery(runtime):
    from backend.core.loop.tool_discovery import project_catalog
    from tests.test_alignment_semantics import process, pending_reply
    _, _, _, _, _, batch, _ = runtime
    p = process(batch.workspace_path)
    from backend.core.loop.tools import ToolRegistry
    p.task_kind = 'answer'
    p.tool_registry = ToolRegistry(['capability_status', 'request_user_decision'])
    pending_reply(p, 'Only summarize the current rules.')
    internal = _build_internal_tools(p, {}, str(batch.workspace_path))
    active = _select_authorized_tools(p, internal, issue_protocol_scopes=False)[0]
    live = {name: tool_definition(internal[name]) for name in (
        'capability_status', 'request_user_decision')}
    live['complete_task'] = tool_definition(active['complete_task'])
    view = project_catalog(live)
    assert view['runtime.tool_contracts']['complete_task'] == live['complete_task']
    assert 'detail_state' not in view['runtime.tool_contracts']['complete_task']
    assert len(encode(view['runtime.tool_contracts'])) <= INITIAL_CONTRACT_CHARS


def test_workspace_tools_can_use_their_first_call(runtime):
    from backend.core.loop.tool_discovery import project_catalog
    from backend.core.tools.catalog import build_workspace_tools
    from tests.test_alignment_semantics import process as supervisor_process

    _, _, _, _, _, batch, _ = runtime
    process = supervisor_process(batch.workspace_path)
    available = build_workspace_tools(batch.workspace_path)
    internal = _build_internal_tools(process, {}, str(batch.workspace_path))
    tools = {name: internal[name] for name in ('capability_status', 'request_user_decision')}
    tools.update({name: available[name] for name in ('read_file', 'list_files', 'search_text', 'run_test')})
    route = ProviderRouteRuntime(process, process.session, None, 'fixture', None, lambda: None)
    route.prepare_envelope([tool.to_openai_function() for tool in tools.values()], {
        'tool_behaviors': {name: tool_definition(tool)['runtime_behavior'] for name, tool in tools.items()}})
    wire = process.session.to_provider_messages(dynamic_envelope=route.envelope, provider_route='fixture')
    catalog = decode_records(wire)['runtime.tool_contracts']
    assert catalog == project_catalog({name: tool_definition(tool) for name, tool in tools.items()})['runtime.tool_contracts']
    for name in ('read_file', 'list_files', 'search_text', 'run_test'):
        assert catalog[name]['runtime_behavior']['immediate_interface']
        assert catalog[name].get('detail_state') is None
        assert not required_tool_context(process, [{'name': name, 'args': {}}], tools)
    assert 'ranges' in catalog['read_file']['parameters']['properties']
    for name in ('wait_agents', 'list_agents', 'read_child_artifact', 'decision_evidence'):
        assert internal[name].immediate_interface
        assert not required_tool_context(process, [{'name': name, 'args': {}}], internal)


def discovery_surface(runtime, count=35):
    _, process, effects, unit, _, batch, _ = runtime
    tools = {'unit': unit}
    for index in range(count):
        name = f'extension_{index:03d}'
        tools[name] = AgentTool(name, 'Inspect extension evidence.\n' + 'Unrelated full manual. ' * 140,
            {'type': 'object', 'properties': {'value': {'type': 'string'}}},
            lambda args: {}, execution_contract=data_broker('test.discovery'))
        process.tool_registry.add_scoped(name)
    process.tool_registry.add_scoped('capability_status')
    internal = _build_internal_tools(process, tools, str(batch.workspace_path))
    catalog = {**tools, **internal}
    batch.get_tools = lambda: _select_authorized_tools(process, catalog, issue_protocol_scopes=False)[0]
    active = batch.get_tools()
    route = ProviderRouteRuntime(process, process.session, None, 'fixture', None, lambda: None)
    route.prepare_envelope([tool.to_openai_function() for tool in active.values()], {
        'tool_behaviors': {name: tool_definition(tool)['runtime_behavior'] for name, tool in active.items()}})
    wire = process.session.to_provider_messages(dynamic_envelope=route.envelope, provider_route='fixture')
    return process, effects, unit, batch, active, route, wire


def execute(batch, name, args, call_id):
    call = {'name': name, 'args': args, 'id': call_id}
    batch.session.append_assistant('', tool_calls=[call])
    result = batch.execute_live_batch([call])
    return result, batch.session.messages[-1]


def test_initial_grouped_catalog_is_bounded_and_every_dynamic_tool_is_discoverable(runtime):
    process, _, _, _, active, _, wire = discovery_surface(runtime, 75)
    current = decode_records(wire)
    assert len(encode(current['runtime.tool_contracts'])) <= INITIAL_CONTRACT_CHARS
    assert len(encode({key: current[key] for key in ('runtime.tool_contracts', 'runtime.tool_groups')})) <= INITIAL_CATALOG_CHARS
    assert set(current['runtime.tool_contracts']) == {'capability_status'}
    assert current['runtime.tool_groups']['other']['count'] == 76
    assert 'Unrelated full manual' not in json.dumps(wire)
    cursor, discovered = '', []
    original_grants = deepcopy(process.approval_grants)
    while True:
        result = active['capability_status'].execute({'operation': 'catalog', 'cursor': cursor, 'limit': 7})
        assert len(result['items']) <= 7 and not result['authority_granted']
        discovered += [row['name'] for row in result['items']]
        cursor = result['next_cursor']
        if not cursor:
            break
    assert sorted(discovered) == sorted(active) and len(discovered) == len(set(discovered))
    assert process.approval_grants == original_grants and process.capability_lease is None


def test_describe_then_execute_uses_exact_contract_once_and_original_effect_pipeline(runtime):
    process, effects, unit, batch, active, route, wire = discovery_surface(runtime)
    sent = deepcopy(wire)
    result, message = execute(batch, 'capability_status', {'operation': 'describe', 'name': 'unit'}, 'inspect')
    assert not result.retry and not effects and not message['is_error']
    assert message['host_authority'] and message['context_purpose'] == 'tool_contract'
    assert message['data']['definition'] == tool_definition(unit)
    assert 'Fixture effect' in message['content']
    invocation = {'name': 'unit', 'id': 'run', 'args': {'value': 'one real effect'}}
    assert not required_tool_context(process, [invocation], active)
    result, message = execute(batch, 'unit', invocation['args'], 'run')
    assert not result.retry and not message['is_error'] and effects == ['one real effect']
    assert not process.approval_grants and sent == wire
    # Original call/output IDs and transactions remain paired after discovery.
    latest = process.session.to_provider_messages(dynamic_envelope=route.envelope, provider_route='fixture')
    for name in ('inspect', 'run'):
        assert len([m for m in latest if m.get('tool_call_id') == name]) == 1


def test_described_tool_still_obeys_deny_and_schema_validation(runtime):
    process, effects, unit, batch, active, _, _ = discovery_surface(runtime)
    unit.approval = ApprovalMode.DENY
    result, _ = execute(batch, 'capability_status', {'operation': 'describe', 'name': 'unit'}, 'inspect-denied')
    assert not result.retry
    result, message = execute(batch, 'unit', {'value': 'must not execute'}, 'denied')
    assert not result.retry and message['is_error'] and not effects and not process.approval_grants


def test_current_registry_revocation_and_changed_contract_invalidate_discovery(runtime):
    process, _, unit, batch, active, _, _ = discovery_surface(runtime)
    result, _ = execute(batch, 'capability_status', {'operation': 'describe', 'name': 'unit'}, 'old')
    assert not result.retry
    unit.description += '\nNew current procedure'
    assert required_tool_context(process, [{'name': 'unit'}], active)
    process.tool_registry.remove_scoped('extension_000')
    value = active['capability_status'].execute({'operation': 'describe', 'name': 'extension_000'})
    assert value['error'] == 'TOOL_NOT_FOUND' and 'definition' not in value
    assert not value['authority_granted']


def test_search_miss_has_exhaustive_fallback_and_scope_bound_cursor(runtime):
    process, _, _, _, active, _, _ = discovery_surface(runtime)
    search = active['capability_status'].execute({'operation': 'search', 'query': '无对应词语'})
    assert search['total'] == 0 and 'catalog' in search['instruction']
    first = active['capability_status'].execute({'operation': 'catalog', 'limit': 2})
    cursor = first['next_cursor']
    assert cursor
    process.tool_registry.remove_scoped('extension_000')
    stale = active['capability_status'].execute({'operation': 'catalog', 'cursor': cursor})
    assert stale['error'] == 'TOOL_CATALOG_CURSOR_STALE' and 'items' not in stale
    unknown = active['capability_status'].execute({'operation': 'catalog', 'group': 'not-mounted'})
    assert unknown['error'] == 'TOOL_GROUP_NOT_ACTIVE'


def test_quoted_discovery_is_not_a_provided_contract_and_restore_retains_genuine_view(runtime):
    process, _, _, batch, active, _, _ = discovery_surface(runtime)
    selected = 'extension_001'
    value = discover(process, active, {'operation': 'describe', 'name': selected})
    process.session.append_user(json.dumps(value))
    assert required_tool_context(process, [{'name': selected}], active)
    result, message = execute(batch, 'capability_status', {'operation': 'describe', 'name': selected}, 'genuine')
    assert not result.retry and 'Unrelated full manual' in message['content']
    from backend.core.loop.session import AgentSession
    restored = AgentSession.from_durable_state({key: deepcopy(getattr(process.session, key)) for key in (
        'session_id', 'messages', 'provider_context_cache', 'provider_context_seed')})
    original = process.session
    process.session = restored
    # Restore the actual current projected catalog through its normal reader.
    from backend.core.loop.provider_context import ContextSections
    restored.to_provider_messages(dynamic_envelope=ContextSections('', original.provider_context_cache['snapshot'],
        scope=original.provider_context_cache['scope']), provider_route='fixture')
    assert not required_tool_context(process, [{'name': selected}], active)
    message['provider_visible'] = False
    restored.messages[-1]['provider_visible'] = False
    assert required_tool_context(process, [{'name': selected}], active)


def test_group_scope_change_keeps_native_history_and_current_surface_exact(runtime):
    process, _, _, batch, _, route, wire = discovery_surface(runtime)
    old = deepcopy(wire)
    process.tool_registry.remove_scoped('capability_status')
    active = batch.get_tools()
    route.prepare_envelope([tool.to_openai_function() for tool in active.values()])
    current = process.session.to_provider_messages(dynamic_envelope=route.envelope, provider_route='fixture')
    decoded = decode_records(current)
    assert 'runtime.tool_groups' not in decoded
    assert set(decoded['runtime.tool_contracts']) == set(active)
    assert wire == old


def test_spilled_long_contract_is_not_marked_as_fully_provided(runtime):
    process, effects, _, batch, active, _, _ = discovery_surface(runtime)
    selected = 'extension_001'
    active[selected].description += '\n' + 'Long exact contract text. ' * 12000
    result, message = execute(batch, 'capability_status', {'operation': 'describe', 'name': selected}, 'large')
    assert not result.retry and not message['is_error'] and not effects
    assert '[TOOL_RESULT_SPILL]' in message['content']
    assert message.get('context_purpose') != 'tool_contract'
    assert required_tool_context(process, [{'name': selected}], active)
    # The existing unexecuted-intent path supplies exact details before effects.
    result, message = execute(batch, selected, {}, 'refine')
    assert result.retry and message['data']['executed'] is False
    assert message['data']['definition'] == tool_definition(active[selected])


def test_oversized_custom_discovery_contract_retains_a_discoverable_compatibility_surface(runtime):
    process, _, _, _, active, route, _ = discovery_surface(runtime)
    active['capability_status'].description += '\n' + 'Custom discovery instructions. ' * 500
    route.prepare_envelope([tool.to_openai_function() for tool in active.values()])
    wire = process.session.to_provider_messages(dynamic_envelope=route.envelope, provider_route='fixture')
    current = decode_records(wire)
    assert 'runtime.tool_groups' not in current
    assert set(current['runtime.tool_contracts']) == set(active)
    assert current['runtime.tool_contracts']['capability_status']['detail_state'] == 'on_demand'
