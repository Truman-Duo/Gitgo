"""Real storage/pipeline draft submission, including cold and stale boundaries."""
from copy import deepcopy
import pytest
from tests.test_mechanism_interactions import runtime
from tests.test_mechanism_capability import pipeline_tool, declaration
from backend.core.mechanism_runtime.application import Application


def draft(runtime):
    tool, execute = pipeline_tool(runtime)
    args = {'operation':'preview','declaration':declaration()}
    boundary, result = execute(args)
    assert not result.is_error and not boundary.contracts
    return tool.model_result(args,result.data)['next_call']


def test_cold_draft_submit_uses_same_semantic_admission_and_replays_once(runtime):
    app,p,*_ = runtime
    request = draft(runtime)
    assert app.repo.definition('guidance') == (None,None)
    p._harness_application = Application(p)
    tool,execute = pipeline_tool(runtime)
    boundary,result = execute(request)
    assert len(boundary.contracts)==1 and boundary.contracts[0]['kind']=='harness_capability_deployment'
    assert not result.is_error and result.data['committed']
    current = p._harness_application
    assert current.repo.definition('guidance')[0]['declaration']==declaration()
    before = deepcopy(current.repo.list_instances(current.task))
    _,again=execute(request)
    assert not again.is_error and again.data['replayed']
    assert current.repo.definition('guidance')[0]['revision']==1
    assert current.repo.list_instances(current.task)==before
    assert not p.approval_grants


@pytest.mark.parametrize('change',['source','contract','task','actor','authority'])
def test_draft_never_overrides_current_identity_source_contract_or_authority(runtime,change):
    app,p,*_=runtime
    request=draft(runtime)
    if change=='source': p.session.append_user('A new requirement changes the source basis')
    elif change=='contract': p.update_context_snapshot(lambda c:{**c,'task_contract':{'revision':19}})
    elif change=='task': p.active_task_id='different-task'
    elif change=='actor': p.process_id='different-owner'
    else: p.capability_profile_id='text.only'
    if change=='authority':
        app.capability_runtime.prepare(request)
        with pytest.raises(ValueError,match='CONFIGURATION_CAPABILITY_REQUIRED'): app.invoke(request)
    else:
        with pytest.raises(ValueError,match='PREVIEW_STALE|PREVIEW_UNAVAILABLE'): app.capability_runtime.prepare(request)
    assert app.repo.definition('guidance')==(None,None)


def test_draft_is_not_a_prepared_commit_and_cannot_accept_rewritten_arguments(runtime):
    app,*_=runtime
    request=draft(runtime)
    with pytest.raises(ValueError,match='PREPARATION_REQUIRED'): app.invoke(request)
    from backend.core.mechanism_runtime.capability_protocol import validate_request
    with pytest.raises(ValueError,match='unsupported fields'):
        validate_request({**request,'declaration':declaration()})
    with pytest.raises(ValueError,match='PREVIEW_UNAVAILABLE'):
        app.capability_runtime.prepare({'operation':'commit','preview_ref':'sha256:'+'a'*64})


def test_source_changes_between_admission_and_commit_cannot_publish(runtime):
    app,p,*_=runtime
    request=draft(runtime)
    app.capability_runtime.prepare(request)
    p.session.append_user('Stop and change this requirement')
    with pytest.raises(ValueError,match='SOURCE_OR_CONTRACT_CHANGED'): app.invoke(request)
    assert app.repo.definition('guidance')==(None,None)
