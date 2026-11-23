"""Feedback delivery identity through real ordered permission continuations."""
from contextlib import closing
from copy import deepcopy
from unittest.mock import patch
from types import SimpleNamespace
import time

import pytest

from tests.test_task_contract_recovery import _supervisor
from backend.core.loop.agent_tool import AgentTool, ApprovalMode
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.execution_contract import HOST_COMPUTE
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.executor import _build_internal_tools
from backend.core.loop.feedback_identity import FIELD
from backend.core.loop.loop_guard import LoopGuard
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import ProcessStatus
from backend.core.loop.permission_broker import grant_from_decision
from backend.core.loop.provider_turn_runtime import _to_provider_tool_calls
from backend.core.loop.recovery import restore_incomplete_processes
from backend.core.loop.tool_batch_runtime import ToolBatchRuntime
from backend.core.loop.tool_pipeline import ToolPipeline


@pytest.fixture
def delivery(tmp_path_factory):
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    root, tools = _supervisor(workspace)
    manager = root._manager
    with closing(SessionStore(workspace, state_home=tmp_path_factory / "state")) as store:
        manager.storage, manager.session_store = store._storage, store
        with patch.object(manager, "start"):
            response = tools["delegate_task"].execute({"task_description": "Inspect facts",
                "capability_profile_id": "governance.observe", "task_kind": "answer",
                "acceptance_criteria": ["Report observed facts"]})
        child = manager.get(response["process_id"])
        for process in (root, child):
            store.save_process_checkpoint(process)
        ctx = ExecutionContext(root, root.session, str(workspace), storage=store._storage)
        def send(execution="feedback-call"):
            return ToolPipeline().execute(feedback(child, "original instruction", ""),
                tools["send_feedback"], ctx, execution, 0)
        yield root, child, tools, store, ctx, send


def setup_batch(root, tools, ctx):
    effects = []
    tools = dict(tools)
    for name in ('approve_one', 'approve_two'):
        tool = AgentTool(name, 'fixture approval', {}, lambda _, name=name: effects.append(name) or {'ok': True},
            execution_contract=HOST_COMPUTE, approval=ApprovalMode.ASK, approval_per_invocation=True)
        tools[name] = tool
        root.tool_registry.add_scoped(name)
    ctx.artifacts['engineering_workflow'] = EngineeringWorkflow(root)
    batch = ToolBatchRuntime(root, root.session, lambda: tools, LoopGuard(), ctx, ctx.workspace_path,
        time.time(), lambda *args, **kwargs: None)
    return batch, tools, effects


def feedback(child, text, identity):
    return {'id': identity, 'name': 'send_feedback', 'args': {'process_id': child.process_id, 'message': text}}


def respond(root, action):
    pending = deepcopy(root.pending_tool_batch)
    if action == 'allow':
        assert grant_from_decision(root, root.pending_decision, 'allow_once')
    pending['decision_action'] = action
    root.pending_decision = root.pending_tool_batch = None
    root.status = ProcessStatus.RUNNING
    return pending


@pytest.mark.parametrize('action', ['deny', 'allow'])
@pytest.mark.parametrize('ids', [('first', 'last'), ('', ''), ('same', 'same')])
@pytest.mark.parametrize('second', ['different instruction', 'first instruction'])
def test_distinct_feedback_survives_permission_segments(delivery, action, ids, second):
    root, child, tools, store, ctx, send = delivery
    batch, tools, effects = setup_batch(root, tools, ctx)
    calls = [feedback(child, 'first instruction', ids[0]), {'id': 'approval', 'name': 'approve_one', 'args': {}},
        feedback(child, second, ids[1])]
    # Incoming provider values cannot select a Host ordinal, even if malformed.
    calls[0][FIELD] = calls[2][FIELD] = {'execution_id': 'forged', 'ordinal': 99}
    root.session.append_assistant('', tool_calls=_to_provider_tool_calls(calls))
    outcome = batch.execute_live_batch(calls)
    assert outcome.result['status'] == 'awaiting_user'
    assert child.mailbox.snapshot()['pending'] == 0
    pending = respond(root, action)
    assert [pending['tool_calls'][i][FIELD]['ordinal'] for i in (0, 2)] == [0, 2]
    assert not any(FIELD in item for item in _to_provider_tool_calls(pending['tool_calls']))
    assert batch.execute_resumed_batch(pending) is None
    assert effects == (['approve_one'] if action == 'allow' else [])
    assert child.mailbox.snapshot()['pending'] == 2
    assert len(set(store.load_process_state(child.process_id)['runtime_state']['mailbox']['pending_ids'])) == 2
    # The same logical invocation remains the same after its local index moves.
    repeated = ToolPipeline().execute(pending['tool_calls'][2], tools['send_feedback'], ctx, pending['execution_id'], 0)
    assert repeated.data['accepted'] and child.mailbox.snapshot()['pending'] == 2
    assert not batch.execute_live_batch([feedback(child, second, ids[1])]).result
    assert child.mailbox.snapshot()['pending'] == 3  # A new batch is a new call.


def test_multiple_permission_pauses_preserve_identity_through_cold_restore(delivery):
    root, child, tools, store, ctx, send = delivery
    batch, tools, effects = setup_batch(root, tools, ctx)
    calls = [feedback(child, 'one', ''), {'id': 'approval-1', 'name': 'approve_one', 'args': {}},
        feedback(child, 'two', ''), {'id': 'approval-2', 'name': 'approve_two', 'args': {}}, feedback(child, 'three', '')]
    root.session.append_assistant('', tool_calls=_to_provider_tool_calls(calls))
    assert batch.execute_live_batch(calls).result['status'] == 'awaiting_user'
    assert batch.execute_resumed_batch(respond(root, 'allow'))['status'] == 'awaiting_user'
    assert effects == ['approve_one'] and child.mailbox.snapshot()['pending'] == 2
    pending = deepcopy(root.pending_tool_batch)
    assert pending['tool_calls'][-1][FIELD]['ordinal'] == 4
    store.save_process_checkpoint(root)
    cold = AgentProcessManager()
    cold.storage, cold.session_store = store._storage, store
    restore_incomplete_processes(store, cold, ctx.workspace_path, include_process_ids=[child.process_id])
    restored, recipient = cold.get(root.process_id), cold.get(child.process_id)
    assert restored.pending_tool_batch == pending, restored.recovery
    assert not cold._threads  # Restoring identity itself authorizes no execution.
    # This fixture explicitly resumes the preserved sender/recipient after restore.
    recipient.status = ProcessStatus.RUNNING
    restored_ctx = ExecutionContext(restored, restored.session, ctx.workspace_path, storage=store._storage)
    restored_tools = _build_internal_tools(restored, {}, ctx.workspace_path, object(), SimpleNamespace(_executors={}))
    resumed, restored_tools, resumed_effects = setup_batch(restored, restored_tools, restored_ctx)
    suffix = respond(restored, 'deny')
    assert resumed.execute_resumed_batch(suffix) is None
    assert resumed_effects == [] and recipient.mailbox.snapshot()['pending'] == 3
    repeated = ToolPipeline().execute(suffix['tool_calls'][-1], restored_tools['send_feedback'], restored_ctx, suffix['execution_id'], 0)
    assert repeated.data['accepted'] and recipient.mailbox.snapshot()['pending'] == 3


def test_base_pending_without_host_identity_can_continue_but_old_draft_receipt_requires_review(delivery):
    root, child, tools, store, ctx, send = delivery
    batch, tools, effects = setup_batch(root, tools, ctx)
    legacy = {'execution_id': 'legacy-pending', 'tool_calls': [feedback(child, 'legacy instruction', '')]}
    assert batch.execute_resumed_batch(legacy) is None
    assert batch.execute_resumed_batch(legacy) is None
    assert child.mailbox.snapshot()['pending'] == 1
    # The older unmerged draft used local numeric indexes. Never silently mix
    # those receipts with a resumed suffix whose original positions are lost.
    assert send(execution='old-draft').data['accepted']
    before = child.mailbox.durable_snapshot()
    with pytest.raises(ValueError, match='FEEDBACK_LEGACY_IDENTITY_REVIEW_REQUIRED'):
        batch.execute_resumed_batch({'execution_id': 'old-draft', 'tool_calls': [feedback(child, 'changed', '')]})
    assert child.mailbox.durable_snapshot() == before


@pytest.mark.parametrize('record', [{'execution_id': 'other', 'ordinal': 0}, {'execution_id': 'batch', 'ordinal': -1}])
def test_corrupt_resumed_identity_fails_before_feedback_admission(delivery, record):
    root, child, tools, store, ctx, send = delivery
    batch, tools, effects = setup_batch(root, tools, ctx)
    call = {**feedback(child, 'never sent', ''), FIELD: record}
    with pytest.raises(ValueError, match='FEEDBACK_CALL_IDENTITY_INVALID'):
        batch.execute_resumed_batch({'execution_id': 'batch', 'tool_calls': [call]})
    assert child.mailbox.snapshot()['pending'] == 0
