"""Capability discovery separates installed definitions from execution authority."""
import copy

from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tool_surface import admission_snapshot


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
