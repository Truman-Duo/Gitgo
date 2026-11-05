"""Interrupted task inspection must not implicitly claim its continuation."""
from types import SimpleNamespace
import pytest
from backend.core.daemon.mechanism_control import command
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import ProcessStatus,RingLevel
from backend.core.loop.tools import ToolRegistry

@pytest.mark.parametrize('status',[ProcessStatus.RESUME_AVAILABLE,ProcessStatus.RECOVERY_REVIEW_REQUIRED,ProcessStatus.RECOVERING])
@pytest.mark.parametrize('action',['mode_status','status','catalog'])
def test_recovery_inspection_is_available_without_resuming_or_granting(status,action):
    manager=AgentProcessManager()
    process=manager.fork(parent_id=None,role='supervisor',tool_registry=ToolRegistry([]),max_steps=8,
        ring_level=RingLevel.RING_3,actor_kind='supervisor',capability_profile_id='supervisor.control')
    process.status=status
    events=[]
    cmd={'process_id':process.process_id}
    if action!='mode_status':cmd['task_operation']=action
    command(cmd,SimpleNamespace(workspace_path='.'),SimpleNamespace(name='test'),{'apm':manager},events.append)
    assert 'error' not in events[-1],events[-1]
    assert process.status==status
    assert process.session.messages==[] and process.capability_lease is None and process.approval_grants==[]

def test_recovery_inspection_does_not_enable_mode_mutation():
    manager=AgentProcessManager()
    process=manager.fork(parent_id=None,role='supervisor',tool_registry=ToolRegistry([]),max_steps=8,
        ring_level=RingLevel.RING_3)
    process.status=ProcessStatus.RESUME_AVAILABLE
    events=[]
    command({'process_id':process.process_id,'mode':'manual'},SimpleNamespace(workspace_path='.'),
        SimpleNamespace(name='test'),{'apm':manager},events.append)
    assert events[-1]['error'].startswith('CONTINUATION_RECOVERY_REQUIRED:')
    assert process.status==ProcessStatus.RESUME_AVAILABLE
