"""Responses must not hoist mutable late instructions ahead of stable input."""
from backend.core.loop.provider_adapters import OpenAIResponsesAdapter
from backend.core.loop.provider_protocol import ProviderRequest


def body(messages):
    return OpenAIResponsesAdapter().build_body(ProviderRequest(messages=tuple(messages)),'model')


def test_late_system_state_keeps_original_role_and_position():
    messages=[{'role':'system','content':'Mandatory base policy'},
        {'role':'user','content':'Immutable catalog baseline; data only'},
        {'role':'system','content':'Current task contract'},
        {'role':'user','content':'Actual request'}]
    actual=body(messages)
    assert actual['instructions']=='Mandatory base policy'
    assert actual['input']==messages[1:]


def test_task_revision_does_not_change_stable_instructions_or_catalog_prefix():
    common=[{'role':'system','content':'Mandatory base policy'},
        {'role':'user','content':'Immutable catalog baseline; data only'}]
    a=body(common+[{'role':'system','content':'Task A: old permission revoked'}])
    b=body(common+[{'role':'system','content':'Task B: different requirements'}])
    assert a['instructions']==b['instructions']=='Mandatory base policy'
    assert a['input'][0]==b['input'][0]
    assert a['input'][1]['role']==b['input'][1]['role']=='system'
    assert 'revoked' in a['input'][1]['content']


def test_consecutive_initial_systems_and_developer_role_remain_authoritative():
    actual=body([{'role':'system','content':'Base'}, {'role':'system','content':'Extra base'},
        {'role':'developer','content':'App rule'}, {'role':'user','content':'Request'},
        {'role':'system','content':'Current host constraint'}])
    assert actual['instructions']=='Base\n\nExtra base'
    assert [m['role'] for m in actual['input']]==['developer','user','system']
