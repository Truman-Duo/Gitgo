"""Capability discovery separates installed definitions from execution authority."""
import copy
from types import SimpleNamespace

from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tool_surface import admission_snapshot


def test_coordination_tools_follow_real_ownership_and_recheck_stale_calls(tmp_path_factory):
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.capabilities import CapabilityProfiles
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.loop.tool_surface import _select_authorized_tools
    from backend.core.loop.coordination import coordination_owner, publish_coordination_event, CoordinationOwnerUnavailable
    import pytest

    manager = AgentProcessManager()
    def create(actor, parent=None):
        return manager.fork(parent_id=parent, role=actor, actor_kind=actor,
            tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools(
                "supervisor.control" if actor == "supervisor" else "text.only")),
            max_steps=8, ring_level=RingLevel.RING_0 if actor == "supervisor" else RingLevel.RING_3,
            workspace_path=str(tmp_path_factory), task_kind="answer")
    root = create("supervisor")
    standalone = create("worker")
    child = create("worker", root.process_id)
    names = {"escalate_to_supervisor", "publish_interface_update"}
    def tools(process):
        return build_internal_tools(process, {}, str(tmp_path_factory), object(), SimpleNamespace(_executors={}), None)
    independent = tools(standalone)
    assert not names.intersection(independent)
    active, unavailable = _select_authorized_tools(standalone, independent)
    assert not names.intersection(active) and not names.intersection(unavailable)
    assert coordination_owner(manager, standalone) is None
    with pytest.raises(CoordinationOwnerUnavailable):
        publish_coordination_event(manager, standalone, kind="blocked", summary="No owner")
    owned = tools(child)
    assert names <= owned.keys()
    result = owned["escalate_to_supervisor"].execute({"kind": "blocked", "summary": "Needs owner"})
    assert result["accepted"] and result["owner_process_id"] == root.process_id
    # A retained tool closure must not keep authority after its owner disappears.
    manager._processes.pop(root.process_id)
    stale = owned["escalate_to_supervisor"].execute({"kind": "blocked", "summary": "stale"})
    assert stale["error"] == "COORDINATION_OWNER_UNAVAILABLE"
    assert stale["authority_granted"] is False
    assert not names.intersection(_select_authorized_tools(child, owned)[0])


def test_inactive_installed_tools_are_discoverable_without_authority_change():
    process = AgentProcess('p', 'supervisor', RingLevel.RING_0, actor_kind='supervisor',
        session=AgentSession(), active_task_id='t')
    before = copy.deepcopy(process.context_snapshot), list(process.session.host_ledger), process.capability_lease
    snapshot = admission_snapshot(process, {'read_file', 'declare_task_contract', 'request_self_execute'},
        {'read_file', 'author_tool', 'shell_script'})
    inactive = {name for profile in snapshot['requestable_profiles'] for name in profile['inactive_installed_tools']}
    assert {'author_tool', 'shell_script'} <= inactive
    assert 'author_tool' not in snapshot['active_tools']
    assert not snapshot['authority_granted']
    assert before == (process.context_snapshot, process.session.host_ledger, process.capability_lease)
    assert [item['tool'] for item in snapshot['admission_options']] == ['declare_task_contract', 'request_self_execute']


def test_missing_installation_and_worker_authority_are_not_advertised_as_requestable():
    process = AgentProcess('p', 'worker', RingLevel.RING_3, actor_kind='worker', session=AgentSession())
    assert not admission_snapshot(process, {'read_file'}, {'read_file', 'shell_script'})['requestable_profiles']
    process.actor_kind = 'supervisor'
    assert not admission_snapshot(process, {'read_file'}, {'read_file'})['requestable_profiles']
