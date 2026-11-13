"""Durable supervisor-to-worker delivery; no model or external execution."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from backend.core.daemon.task_observations import TaskObservations
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.executor import _build_internal_tools
from backend.core.loop.loop_bookkeeping import _apply_pending_instructions
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import ProcessStatus
from backend.core.loop.native_invocation_scope import native_invocation_scope
from backend.core.loop.recovery import restore_incomplete_processes
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.storage import StorageCommitUnknown
from tests.test_task_contract_recovery import _supervisor


@pytest.fixture
def delivery(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    with closing(SessionStore(tmp_path_factory, state_home=tmp_path_factory / 'state')) as store:
        manager.storage, manager.session_store = store._storage, store
        with patch.object(manager, 'start'):
            response = tools['delegate_task'].execute({'task_description': 'Inspect facts',
                'capability_profile_id': 'governance.observe', 'task_kind': 'answer',
                'acceptance_criteria': ['Report the observed facts']})
        child = manager.get(response['process_id'])
        for process in (root, child):
            store.save_process_checkpoint(process)
        ctx = ExecutionContext(root, root.session, str(tmp_path_factory), storage=store._storage)
        def send(execution='feedback-call', text='Also inspect the consumer case', index=0):
            return ToolPipeline().execute({'name': 'send_feedback', 'args': {
                'process_id': child.process_id, 'message': text}}, tools['send_feedback'], ctx, execution, index)
        yield root, child, tools, store, ctx, send


def test_tool_acceptance_checkpoints_recipient_before_owner_observation_and_restart(delivery, tmp_path_factory):
    root, child, tools, store, ctx, send = delivery
    response = send()
    assert not response.is_error, response.formatted
    assert response.data['accepted'] and response.data['persistence'] == 'checkpoint'
    message_id = response.data['message_id']
    assert store.load_process_state(child.process_id)['runtime_state']['mailbox']['pending_ids'] == [message_id]
    observer = TaskObservations(root, root._manager, store,
        SimpleNamespace(append=lambda event, **kwargs: event), Queue())
    observer.persist({'event': 'tool_result', 'process_id': root.process_id, 'tool_name': 'send_feedback'})
    cold = AgentProcessManager()
    restore_incomplete_processes(store, cold, tmp_path_factory, include_process_ids=[child.process_id])
    restored = cold.get(child.process_id)
    assert restored.mailbox.get(message_id).content == 'Also inspect the consumer case'
    assert restored.mailbox.snapshot()['pending'] == 1
    assert not cold._threads  # Delivery/recovery grants no execution.


def test_same_host_call_replay_does_not_enqueue_twice_but_new_calls_do(delivery):
    root, child, tools, store, ctx, send = delivery
    first, repeated = send(), send()
    assert first.data['message_id'] == repeated.data['message_id']
    assert child.mailbox.snapshot()['pending'] == 1
    different = send(execution='another-feedback-call')
    assert different.data['message_id'] != first.data['message_id']
    assert child.mailbox.snapshot()['pending'] == 2
    conflict = send(text='Different arguments cannot reuse the same delivery identity')
    assert conflict.is_error and 'IDENTITY_CONFLICT' in conflict.formatted
    assert child.mailbox.snapshot()['pending'] == 2


def test_returning_existing_delivery_receipt_needs_no_second_checkpoint(delivery):
    root, child, tools, store, ctx, send = delivery
    first = send()
    with patch.object(store, 'save_process_checkpoint', side_effect=OSError('currently unavailable')) as save:
        repeated = send()
    save.assert_not_called()
    assert repeated.data['accepted'] and repeated.data['message_id'] == first.data['message_id']
    assert child.mailbox.snapshot()['pending'] == 1


def test_known_checkpoint_failure_retracts_message_and_exact_retry_is_safe(delivery):
    root, child, tools, store, ctx, send = delivery
    with patch.object(store, 'save_process_checkpoint', side_effect=OSError('disk unavailable')):
        response = send()
    assert response.is_error and 'disk unavailable' in response.formatted
    assert child.mailbox.snapshot()['pending'] == 0
    assert child.mailbox.snapshot()['messages'] == []
    assert store.load_process_state(child.process_id)['runtime_state']['mailbox']['pending_ids'] == []
    assert send().data['accepted']
    assert child.mailbox.snapshot()['pending'] == 1


@pytest.mark.parametrize('commit_first', [False, True])
def test_unknown_checkpoint_commit_retains_message_and_suppresses_replay(delivery, commit_first):
    root, child, tools, store, ctx, send = delivery
    original = store._storage.save_agent_checkpoint
    def uncertain(*args, **kwargs):
        if commit_first:
            original(*args, **kwargs)
        raise StorageCommitUnknown('unknown checkpoint acknowledgment')
    with patch.object(store._storage, 'save_agent_checkpoint', uncertain):
        response = send()
    assert response.is_error
    assert response.data['code'] == 'FEEDBACK_COMMIT_UNKNOWN'
    assert response.data['automatic_replay_suppressed']
    assert child.mailbox.snapshot()['pending'] == 1
    assert child.recovery['resume_forbidden'] and child.recovery['checkpoint_commit'] == 'unknown'
    replay = send()
    assert replay.is_error and replay.data['error'] == 'FEEDBACK_RECOVERY_REQUIRED'
    assert child.mailbox.snapshot()['pending'] == 1
    pending = store.load_process_state(child.process_id)['runtime_state']['mailbox']['pending_ids']
    assert len(pending) == int(commit_first)


@pytest.mark.parametrize('fails', [False, True])
def test_consumer_cannot_apply_uncommitted_feedback(delivery, fails):
    root, child, tools, store, ctx, send = delivery
    entered, release, attempted = Event(), Event(), Event()
    original = store.save_process_checkpoint
    def checkpoint(process, **kwargs):
        if process is child:
            entered.set()
            assert release.wait(3)
            if fails:
                raise OSError('known checkpoint failure')
        return original(process, **kwargs)
    def consume():
        attempted.set()
        return _apply_pending_instructions(child, child.session)
    with ThreadPoolExecutor(2) as pool, patch.object(store, 'save_process_checkpoint', checkpoint):
        sending = pool.submit(send)
        assert entered.wait(3)
        consuming = pool.submit(consume)
        try:
            assert attempted.wait(3)
            assert not consuming.done()
        finally:
            release.set()
        result = sending.result(timeout=3)
        assert consuming.result(timeout=3) == (0 if fails else 1)
    assert result.is_error is fails


def test_cold_applied_message_replay_returns_durable_receipt_without_reapplication(delivery, tmp_path_factory):
    root, child, tools, store, ctx, send = delivery
    first = send()
    assert _apply_pending_instructions(child, child.session) == 1
    for process in (root, child):
        process.status = ProcessStatus.COMPLETED
        process.result = {'status': 'completed'}
        process.lifecycle_finalized = True
        store.save_process_checkpoint(process)
        store.append_event(process.process_id, 'agent_complete', {'status': 'completed'})
    cold = AgentProcessManager()
    cold.storage, cold.session_store = store._storage, store
    restore_incomplete_processes(store, cold, tmp_path_factory, include_process_ids=[child.process_id])
    restored_root, restored_child = cold.get(root.process_id), cold.get(child.process_id)
    restored_tools = _build_internal_tools(restored_root, {}, str(tmp_path_factory), object(), SimpleNamespace(_executors={}))
    restored_ctx = ExecutionContext(restored_root, restored_root.session, str(tmp_path_factory), storage=store._storage)
    with native_invocation_scope(restored_ctx, 'feedback-call', 0):
        replay = restored_tools['send_feedback'].execute({'process_id': child.process_id, 'message': 'Also inspect the consumer case'})
    assert replay['accepted'] and replay['status'] == 'applied'
    assert replay['message_id'] == first.data['message_id']
    assert _apply_pending_instructions(restored_child, restored_child.session) == 0
    assert len([m for m in restored_child.session.messages if m.get('mailbox_message_id') == replay['message_id']]) == 1


def test_memory_manager_api_preserved_but_protocol_tool_does_not_claim_durability(delivery):
    root, child, tools, store, ctx, send = delivery
    del root._manager.session_store
    assert root._manager.send_instruction(child.process_id, 'Legacy in-memory instruction')['accepted']
    before = child.mailbox.durable_snapshot()
    result = send()
    assert result.is_error and 'FEEDBACK_PERSISTENCE_UNAVAILABLE' in result.formatted
    assert child.mailbox.durable_snapshot() == before


def test_user_instruct_retains_its_own_single_checkpoint_and_requirement_admission(delivery, tmp_path_factory):
    from backend.core.daemon.dispatch import _cmd_task
    from backend.core.loop.task_maintenance import state_for
    root, child, tools, store, ctx, send = delivery
    emitted = []
    with patch.object(store, 'save_process_checkpoint', wraps=store.save_process_checkpoint) as save:
        _cmd_task({'action': 'instruct', 'process_id': child.process_id, 'instruction': 'Add a consumer check'},
            SimpleNamespace(workspace_path=tmp_path_factory), SimpleNamespace(name='fixture'),
            {'apm': root._manager, 'session_store': store}, emitted.append)
    assert emitted[-1]['result']['status'] == 'accepted'
    assert save.call_count == 1 and save.call_args.args[0] is child
    assert child.mailbox.snapshot()['pending'] == 1
    assert len(state_for(child)['changes']) == 1
    assert store.load_process_state(child.process_id)['runtime_state']['mailbox']['pending_ids']


@pytest.mark.parametrize('commit_first', [False, True])
def test_active_turn_waiting_at_delivery_fence_never_exposes_unknown_message_to_provider(delivery, commit_first):
    from backend.core.loop.mechanism_lifecycle import fence
    from backend.core.loop.turn_preparation import TurnPreparation
    root, child, tools, store, ctx, send = delivery
    child.status = ProcessStatus.RUNNING
    entered, release, attempted = Event(), Event(), Event()
    provider = Mock()
    original = store._storage.save_agent_checkpoint
    prior_receipt = {'receipt_id': 'already-observed', 'committed': True}
    child.tool_receipts.append(prior_receipt)
    def unknown(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        if commit_first:
            original(*args, **kwargs)
        raise StorageCommitUnknown('lost checkpoint acknowledgement')
    def apply():
        attempted.set()
        with fence(child), child.session._mechanism_lock:
            return _apply_pending_instructions(child, child.session)
    preparation = TurnPreparation(child, child.session, provider,
        lambda: SimpleNamespace(check=lambda session: {'action': 'none'}),
        SimpleNamespace(refresh=lambda: None), None, apply, lambda event: None)
    def turn():
        result = preparation.prepare()
        if result.result is None:
            provider(list(child.session.messages))
        return result
    with ThreadPoolExecutor(2) as pool, patch.object(store._storage, 'save_agent_checkpoint', unknown):
        sending = pool.submit(send)
        assert entered.wait(3)
        running = pool.submit(turn)
        try:
            assert attempted.wait(3)
            assert not running.done()
        finally:
            release.set()
        response = sending.result(timeout=3)
        prepared = running.result(timeout=3)
    assert response.data['code'] == 'FEEDBACK_COMMIT_UNKNOWN'
    provider.assert_not_called()
    assert prepared.result is not None
    assert prior_receipt in child.tool_receipts
    assert child.status == ProcessStatus.RECOVERY_REVIEW_REQUIRED
    assert child.mailbox.snapshot()['pending'] == 1
    assert not any(message.get('mailbox_message_id') for message in child.session.messages)


@pytest.mark.parametrize('tool_call', [False, True])
def test_provider_already_inflight_does_not_complete_after_unknown_delivery(delivery, tool_call):
    from backend.core.loop.executor import agent_step
    root, child, tools, store, ctx, send = delivery
    from backend.core.loop.tools import ToolRegistry
    child.capability_profile_id = 'text.only'
    child.tool_registry = ToolRegistry([])
    class Provider:
        calls = 0
        def stream_chat(self, *args, **kwargs):
            self.calls += 1
            with patch.object(store._storage, 'save_agent_checkpoint',
                    side_effect=StorageCommitUnknown('unknown while provider was in flight')):
                assert send().data['code'] == 'FEEDBACK_COMMIT_UNKNOWN'
            delta = {'content': 'Observed facts.\nTASK_COMPLETE'}
            if tool_call:
                delta = {'tool_calls': [{'index': 0, 'id': 'complete-call', 'type': 'function',
                    'function': {'name': 'complete_task', 'arguments': '{"result": "Observed facts"}'}}]}
            yield {'choices': [{'delta': delta}]}
    provider = Provider()
    result = agent_step(child, provider, instruction='Inspect the facts', workspace_path=ctx.workspace_path)
    assert result['error']['code'] == 'PROCESS_RECOVERY_REQUIRED'
    assert child.status == ProcessStatus.RECOVERY_REVIEW_REQUIRED
    assert provider.calls == 1
    assert child.mailbox.snapshot()['pending'] == 1


def test_unknown_delivery_blocks_unstarted_batch_suffix_without_rolling_back_prefix(delivery, tmp_path_factory):
    from backend.core.loop.agent_tool import AgentTool, ToolEffect
    from backend.core.loop.execution_contract import data_broker
    from backend.core.loop.tool_execution import ToolExecution
    from backend.core.loop.tools import ToolRegistry
    root, child, tools, store, ctx, send = delivery
    child.capability_profile_id = 'development.workspace'
    workspace = tmp_path_factory / 'consumer'
    workspace.mkdir()
    child.worktree_path = str(workspace)
    effects = []
    retained = workspace / 'effect.txt'
    retained.write_text('before batch')
    def effect(args):
        effects.append(args['label'])
        retained.write_text(args['label'])
        return {'ok': True}
    tool = AgentTool(name='feedback_fixture_write', description='Fixture effect',
        parameters={'type': 'object', 'properties': {'label': {'type': 'string'}}, 'required': ['label']},
        execute=effect, read_only=False, effect=ToolEffect.WORKSPACE_WRITE,
        execution_contract=data_broker('test.fixture'), timeout=0.1)
    child.tool_registry = ToolRegistry([tool.name])
    child_ctx = ExecutionContext(child, child.session, str(workspace), storage=store._storage,
        artifacts={'tool_catalog': {tool.name: tool}})
    def boundary(call, tool, execution, index):
        if index == 1:
            with patch.object(store._storage, 'save_agent_checkpoint',
                    side_effect=StorageCommitUnknown('unknown between calls')):
                assert send().data['code'] == 'FEEDBACK_COMMIT_UNKNOWN'
    execution = ToolExecution('feedback-suffix', child_ctx,
        [{'name': tool.name, 'args': {'label': label}} for label in ('committed-prefix', 'unstarted-suffix')],
        before_call=boundary)
    execution.begin()
    results = execution.execute_batch({tool.name: tool})
    assert not results[0].is_error, results[0].formatted
    assert effects == ['committed-prefix']
    assert results[1].diagnostics['code'] == 'PROCESS_RECOVERY_REQUIRED'
    assert results[1].diagnostics['nature'] == 'governance'
    assert results[1].receipt['effect_state'] == 'not_committed'
    assert not execution._rolled_back
    assert retained.read_text() == 'committed-prefix'
    assert child.mailbox.snapshot()['pending'] == 1


@pytest.mark.parametrize('status, expected', [
    (ProcessStatus.CANCELLED, 'cancelled'), (ProcessStatus.TIMED_OUT, 'timed_out'),
])
def test_explicit_termination_keeps_existing_outcome_even_with_recovery_evidence(delivery, status, expected):
    from backend.core.loop.runtime_outcomes import _status_result
    root, child, tools, store, ctx, send = delivery
    send()
    child.recovery = {'resume_forbidden': True, 'checkpoint_commit': 'unknown'}
    child.status = status
    result = _status_result(child, child.session)
    assert result['status'] == expected
    assert child.recovery['checkpoint_commit'] == 'unknown'
