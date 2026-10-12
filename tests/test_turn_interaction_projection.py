"""Interaction truth and current policy survive task changes and late updates."""
from types import SimpleNamespace

from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.interaction_snapshot import interaction_snapshot
from backend.core.loop.alignment_evidence import interpret_answer
from backend.core.loop.turn_guidance import _build_current_turn_envelope
from backend.core.loop.context_compaction import ContextCompaction
from backend.core.loop.governance_runtime import GovernanceRuntime
from backend.core.loop.turn_observations import TurnObservations
from backend.core.loop.event_bus import EventBus, ToolEvent
from backend.core.loop.tool_surface_runtime import ToolSurfaceRuntime
from backend.core.loop.turn_guidance import _build_current_turn_envelope
import pytest


def make_process(tmp_path_factory):
    return AgentProcess(process_id='own', role='worker', ring_level=RingLevel.RING_3,
                        active_task_id='task', worktree_path=str(tmp_path_factory), session=AgentSession())


def test_prior_task_and_other_actor_cannot_invent_live_card(tmp_path_factory):
    process = make_process(tmp_path_factory)
    process.session.host_ledger.extend([
        {'event': 'user_decision_requested', 'task_id': 'old', 'process_id': 'own', 'decision_id': 'old-card'},
        {'event': 'user_decision_requested', 'task_id': 'task', 'process_id': 'other', 'decision_id': 'child-card'},
    ])
    snapshot = interaction_snapshot(process)
    assert snapshot['state'] == 'consistent'
    assert snapshot['pending_card'] is None
    process.pending_decision = {'task_id': 'old', 'process_id': 'own', 'decision_id': 'old-card'}
    assert interaction_snapshot(process)['state'] == 'inconsistent'
    envelope = _build_current_turn_envelope(process, [])
    assert 'inconsistent' in envelope
    _build_current_turn_envelope(process, [])
    assert len([e for e in process.session.host_ledger if e.get('code') == 'INTERACTION_STATE_CONFLICT']) == 1


def test_answer_receipt_is_not_interpretation_and_changed_answer_invalidates_frontier(tmp_path_factory):
    process = make_process(tmp_path_factory)
    workflow = EngineeringWorkflow(process)
    workflow.configure({'profiles': ['alignment'], 'nodes': [
        {'id': 'purpose', 'kind': 'decision', 'state_topic': 'purpose'},
        {'id': 'audience', 'kind': 'decision', 'state_topic': 'audience', 'depends_on': ['purpose']},
    ]})
    request = workflow.invoke({'operation': 'ask', 'node_id': 'purpose', 'request': {
        'question': '用途？', 'why_user_must_decide': '决定范围',
        'options': [], 'kind': 'clarification', 'state_topic': 'purpose'}})
    assert interaction_snapshot(process)['pending_card'] == request['decision_id']
    answer = {**request, 'event': 'user_decision_received', 'answer': '离线阅读'}
    process.session.host_ledger.append(answer)
    process.pending_decision = None
    before = interaction_snapshot(process)
    assert before['recorded_ready_questions'] == ['purpose']
    assert before['answers_needing_interpretation'] == ['purpose']
    node = workflow._load()['plan']['nodes'][0]
    interpret_answer(process, node, answer, {'disposition': 'confirmed', 'value': '离线阅读', 'quote': '离线阅读'})
    after = interaction_snapshot(process)
    assert after['recorded_ready_questions'] == ['audience']
    assert after['answers_needing_interpretation'] == []
    process.session.host_ledger.append({**answer, 'answer': '改为在线协作'})
    changed = interaction_snapshot(process)
    assert changed['recorded_ready_questions'] == ['purpose']
    assert changed['answers_needing_interpretation'] == ['purpose']


def test_current_compaction_uses_policy_getter_at_invocation(tmp_path_factory):
    process = make_process(tmp_path_factory)
    received = []
    window = SimpleNamespace(last_compaction_error='', compact=lambda *a, **k: received.append(k['harness_data']) or True)
    policy = {'version': 1}
    controller = ContextCompaction(process, process.session, None, lambda: window,
                                   lambda: policy, lambda *a, **k: None, None)
    policy = {'version': 2}
    assert controller.run('test') == (True, None)
    assert received == [{'version': 2}]


def test_removing_signals_disables_previous_bus(tmp_path_factory):
    process = make_process(tmp_path_factory)
    process.update_context_snapshot(lambda ctx: {**ctx, 'signals': []})
    governance = GovernanceRuntime(process, process.session, '', lambda *a, **k: None)
    assert governance.active_bus is not None
    process.update_context_snapshot(lambda ctx: {k: v for k, v in ctx.items() if k != 'signals'})
    governance.refresh()
    assert governance.active_bus is None


def test_observation_adapter_retains_origin_and_authoritative_errors(tmp_path_factory):
    process = make_process(tmp_path_factory)
    observed = []
    adapter = TurnObservations(process, observed.append)
    bus = EventBus(on_error=adapter.failed)
    adapter.wire(bus, SimpleNamespace(append_tool_call=lambda *a: None))
    signal = ToolEvent('CompositeStepCompleted', 'execution', tool_name='nested')
    bus.emit(signal)
    assert observed[0]['origin_signal'] == signal.signal
    assert observed[0]['task_id'] == 'task'
    def broken(_event):
        raise OSError('checkpoint failed')
    critical = TurnObservations(process, broken)
    with pytest.raises(OSError, match='checkpoint failed'):
        critical.publish({'event': 'agent_complete'})
    bus.subscribe('ToolNotice', broken)
    bus.emit(ToolEvent('ToolNotice', 'execution'))
    assert bus.delivery_failures[-1]['error_type'] == 'OSError'
    assert process.session.host_ledger[-1]['code'] == 'EVENT_DELIVERY_FAILED'


def test_discovery_is_bound_to_installed_catalog_and_does_not_grant_authority(tmp_path_factory):
    process = make_process(tmp_path_factory)
    process.actor_kind = 'supervisor'
    surface = ToolSurfaceRuntime(process, None, None, '', None)
    surface.known_tools = {'exec_command': object(), 'web_fetch': object()}
    before = process.context_version
    discovery = surface.discovery([], excluded={'web_fetch'})
    assert discovery['authority_granted'] is False
    assert process.capability_lease is None
    assert process.context_version == before
    names = {name for profile in discovery['requestable_profiles'] for name in profile['inactive_installed_tools']}
    assert names == {'exec_command'}
    envelope = _build_current_turn_envelope(process, [], discovery)
    assert 'exec_command' in envelope
    assert 'web_fetch' not in envelope

