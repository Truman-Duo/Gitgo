"""Reuse must preserve revocations, evidence and independent workload accounting."""
import copy
import json
from types import SimpleNamespace

from backend.core.loop.session import AgentSession
from backend.core.loop.provider_cache import order_live_tools, prefix_intent
from backend.core.loop.models import AgentProcess, RingLevel


def tool(name, description='test'):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': {}}}}


def test_live_order_appends_new_tools_but_revokes_and_refreshes_definitions():
    session = AgentSession()
    first = order_live_tools(session, [tool('z'), tool('m')])
    second = order_live_tools(session, [tool('a'), tool('z'), tool('m')])
    assert second[:2] == first
    restored = AgentSession.from_durable_state({'provider_tool_order': session.provider_tool_order})
    assert order_live_tools(restored, [tool('a'), tool('z', 'changed')]) == [tool('z', 'changed'), tool('a')]
    assert order_live_tools(restored, []) == []


def test_wire_surface_never_keeps_withdrawn_or_disabled_web_tools():
    from backend.core.loop.provider_request_surface import prepare_provider_request
    from backend.core.loop.agent_tool import AgentTool
    session = AgentSession()
    process = SimpleNamespace(session=session, runtime_preferences={'web_search_mode': 'disabled'})
    surface = SimpleNamespace(tools={n: AgentTool(n, n, {}, lambda a: {}) for n in ('read', 'web_fetch')},
        discovery=lambda names, **kw: {'active': names})
    captured = []
    route = SimpleNamespace(route='', prepare_envelope=lambda *args: captured.append(args[0]))
    def prepare():
        return prepare_provider_request(process, SimpleNamespace(protocol='openai_chat'), surface, route, lambda e: None).tools
    from backend.core.loop.provider_tool_transport import CALL
    assert [t['function']['name'] for t in prepare()] == [CALL]
    assert [t['function']['name'] for t in captured[-1]] == ['read']
    surface.tools = {'new': AgentTool('new', 'new', {}, lambda a: {})}
    assert [t['function']['name'] for t in prepare()] == [CALL]
    assert [t['function']['name'] for t in captured[-1]] == ['new']


def test_auxiliary_usage_does_not_reset_main_cache_baseline():
    session = AgentSession()
    messages = [{'role': 'system', 'content': 'stable'}]
    main = prefix_intent(messages, [tool('read')], mode='automatic')
    grill = prefix_intent(messages, [], mode='automatic', purpose='alignment')
    assert main.stable_prefix_key != grill.stable_prefix_key
    for intent, purpose in ((main, 'main'), (grill, 'alignment'), (main, 'main'), (grill, 'alignment')):
        session.record_provider_usage({'input_tokens': 100, 'cache_read_tokens': 80, 'purpose': purpose}, cache_intent=intent)
    assert [e['miss_reason'] for e in session.cache_telemetry] == ['cold_start', 'cold_start', '', '']
    assert session.cache_summary()['raw_hit_ratio'] == .8
    changed = prefix_intent(messages, [tool('write')], mode='automatic')
    session.record_provider_usage({'input_tokens': 100}, cache_intent=changed)
    assert session.cache_telemetry[-1]['miss_reason'] == 'route_or_tool_schema_changed'


def test_grill_serialization_keeps_every_field_and_stable_task_prefix():
    from backend.core.loop.alignment_runtime import assess_with_provider
    from backend.core.loop.llm import LLMProvider
    class Transport:
        bodies = []
        def post_json(self, url, body, headers, **kw):
            self.bodies.append(copy.deepcopy(body))
            return {'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': '{"request_frame":{}}'}]}],
                'usage': {'input_tokens': 100, 'output_tokens': 5, 'input_tokens_details': {'cached_tokens': 80}}}
    transport = Transport()
    provider = LLMProvider('https://example.invalid', 'unused', 'model', protocol='openai_responses', transport=transport)
    payload = {'candidate': '', 'request': 'goal', 'original_request': 'original',
        'task_constraints': ['read only'], 'stage': 'admission', 'workflow': {'nodes': []}}
    result = assess_with_provider(provider, payload)
    assert result['_provider_cache_intent']['stable_prefix_key'] == transport.bodies[0]['prompt_cache_key']
    assert json.loads(transport.bodies[0]['input'][-1]['content']) == payload
    assess_with_provider(provider, {**payload, 'stage': 'question_proposal', 'proposed_questions': ['real change']})
    assert transport.bodies[0]['prompt_cache_key'] == transport.bodies[1]['prompt_cache_key']
    first = transport.bodies[0]['input'][-1]['content']
    second = transport.bodies[1]['input'][-1]['content']
    prefix = first.split(',"candidate":', 1)[0]
    assert second.startswith(prefix)


def test_mechanism_projection_is_ephemeral_and_observer_changes_do_not_grow_history():
    from backend.core.loop.prompt_admission import _refresh_compiled_prompt
    from backend.core.loop.turn_guidance import _build_current_turn_envelope
    process = AgentProcess('p', 'supervisor', RingLevel.RING_0, actor_kind='supervisor',
        session=AgentSession(), active_task_id='t', context_snapshot={})
    process.runtime_preferences['task_maintenance_enabled'] = True
    process.context_snapshot['task_maintenance'] = {'task_id': 't', 'revision': 1, 'epoch': 1,
        'phase': 'ready', 'changes': {}, 'related': {'candidates': {}},
        'plan': {'summary': 'preserve this plan'}, 'source_receipts': {'ref': {'content': 'audit-only'}}}
    tools = {'task_mechanism': SimpleNamespace(description='mechanisms', parameters={}, ring_required=RingLevel.RING_3)}
    _refresh_compiled_prompt(process.session, process, tools, '.')
    before = copy.deepcopy(process.session.messages)
    state_before = copy.deepcopy(process.context_snapshot)
    for index in range(5):
        process._mechanism_projection = {'prompt_digest': str(index)}
        _refresh_compiled_prompt(process.session, process, tools, '.')
    assert process.session.messages == before
    process.context_snapshot['task_maintenance']['phase'] = 'needs_review'
    envelope = _build_current_turn_envelope(process, [tool('task_mechanism')])
    assert 'needs_review' in envelope and 'preserve this plan' in envelope
    assert 'audit-only' not in envelope and 'consumer_projection' not in envelope
    assert process.session.messages == before
    assert process.context_snapshot['task_maintenance']['source_receipts'] == state_before['task_maintenance']['source_receipts']


def test_live_guidance_exposes_newer_user_mode_without_overwriting_pinned_definition():
    from backend.core.loop.turn_guidance import _build_current_turn_envelope
    from backend.core.loop import mechanisms
    from backend.core.loop.mechanism_lifecycle import compile_revision
    process = AgentProcess('p', 'supervisor', RingLevel.RING_0, actor_kind='supervisor',
        session=AgentSession(), active_task_id='t', context_snapshot={})
    process.runtime_preferences['task_maintenance_enabled'] = True
    process.context_snapshot['task_maintenance'] = {'task_id': 't', 'revision': 1, 'phase': 'ready',
        'definition': {'elements': [{'use': 'task.advancement', 'parameters': {'mode': 'manual'}}]}}
    active = compile_revision(mechanisms.snapshot(process), mechanisms.DEFINITION, {'mode': 'auto'}, 0)
    active['user_control'] = {'origin': 'user_command', 'authority_sequence': 9, 'parameters': {'mode': 'auto'}}
    process.session.mechanism_instances[mechanisms.ADVANCEMENT] = active
    before = copy.deepcopy(process.context_snapshot)
    envelope = _build_current_turn_envelope(process, [])
    assert '"effective_mode": "auto"' in envelope
    assert '"authority_sequence": 9' in envelope
    assert process.context_snapshot == before and process.session.messages == []
