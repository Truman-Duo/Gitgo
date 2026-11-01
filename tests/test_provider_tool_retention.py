"""Native transaction preservation; fixtures do not count as API acceptance."""
from copy import deepcopy
from contextlib import closing

import pytest

from backend.core.loop.session import AgentSession


def batch():
    session = AgentSession()
    session.active_provider_route = 'route-a'
    session.append_user('Read the actual evidence.')
    session.append_assistant_provider('', tool_calls=[
        {'id': f'call-{n}', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}
        for n in range(4)])
    for n in range(4):
        if n == 2:
            session.append_host_steering('A new constraint.', steering_type='constraint')
        session.append_tool_result(f'actual output {n}', tool_name='read', tool_call_id=f'call-{n}')
    return session


def assert_batch(wire):
    head = next(m for m in wire if m.get('provider_state', {}).get('tool_calls'))
    ids = {c['id'] for c in head['provider_state']['tool_calls']}
    start = wire.index(head)
    assert [m.get('tool_call_id') for m in wire[start + 1:start + 5]] == [f'call-{n}' for n in range(4)]
    assert ids == {m['tool_call_id'] for m in wire if m.get('role') == 'tool'}
    assert all(m['content'] == 'actual output ' + m['tool_call_id'][-1] for m in wire if m.get('role') == 'tool')


def test_compaction_tail_keeps_complete_actual_batch_and_state():
    session = batch()
    original = deepcopy(session.messages)
    session.begin_context_epoch(summary='Current work remains.', retained_messages=session.messages[-3:], reason='compact')
    assert_batch(session.to_provider_messages(provider_route='route-a'))
    assert session.epoch_archive[0]['messages'] == original
    assert sum(m.get('steering_type') == 'constraint' for m in session.messages) == 1
    # A route switch must not replay another Provider's outputs.
    assert not any(m.get('role') == 'tool' for m in session.to_provider_messages(provider_route='route-b'))


def test_legacy_broken_epoch_recovers_only_recorded_pair_without_mutating_audit():
    session = batch()
    session.epoch_archive = [{'epoch': 0, 'messages': deepcopy(session.messages), 'provider_state': deepcopy(session.provider_state)}]
    session.messages = deepcopy(session.messages[-2:])
    session.provider_state = {}
    before = deepcopy(session.messages)
    assert_batch(session.to_provider_messages(provider_route='route-a'))
    assert session.messages == before
    assert session.provider_state == {}


def test_unknown_orphan_is_visible_failure_never_fabricated_call():
    session = AgentSession()
    session.append_tool_result('effect already happened', tool_name='write', tool_call_id='unknown')
    with pytest.raises(ValueError, match='PROVIDER_TOOL_TRANSACTION_SOURCE_MISSING'):
        session.to_provider_messages()


def test_public_completed_task_tail_does_not_resurrect_native_history():
    session = batch()
    public = [{'role': 'user', 'content': 'Earlier task complete.'}, {'role': 'assistant', 'content': 'Done.'}]
    session.begin_context_epoch(summary='New unrelated task.', retained_messages=public, reason='completed_task')
    assert not session.provider_state
    assert not any(m.get('role') == 'tool' for m in session.to_provider_messages())


def test_failed_provider_turn_is_not_resurrected_from_archive():
    session = batch()
    session.epoch_archive = [{'messages': deepcopy(session.messages), 'provider_state': deepcopy(session.provider_state)}]
    assert session.quarantine_failed_provider_turn('fixture failure')
    assert not any(m.get('role') == 'tool' for m in session.to_provider_messages())


def test_responses_native_call_items_retain_actual_pair():
    session = AgentSession()
    session.active_provider_route = 'responses'
    session.append_assistant_provider('', continuation_state={'response_output_items': [
        {'type': 'function_call', 'id': 'item-1', 'call_id': 'actual-call', 'name': 'read', 'arguments': '{}'}]})
    session.append_host_steering('Current policy', steering_type='policy')
    session.append_tool_result('Recorded only', tool_name='read', tool_call_id='actual-call')
    session.begin_context_epoch(summary='Still current', retained_messages=session.messages[-1:], reason='compact')
    wire = session.to_provider_messages(provider_route='responses')
    assert wire[-2]['provider_state']['response_output_items'][0]['call_id'] == wire[-1]['tool_call_id']


def test_recorded_transactions_survive_actual_session_store_reopen(tmp_path_factory):
    from backend.core.loop.manager import SessionStore
    session = batch()
    session.begin_context_epoch(summary='Preserve the actual batch', retained_messages=session.messages[-2:], reason='compact')
    workspace, state_home = tmp_path_factory / 'work', tmp_path_factory / 'state'
    workspace.mkdir()
    with closing(SessionStore(workspace, state_home=state_home, migrate_legacy=False)) as store:
        store.save_checkpoint('fixture-process', session)
    with closing(SessionStore(workspace, state_home=state_home, migrate_legacy=False)) as store:
        saved = store.load_session_state('fixture-process')
    restored = AgentSession()
    restored.messages = saved['messages']
    restored.provider_state = saved['provider_state']
    assert_batch(restored.to_provider_messages(provider_route='route-a'))
