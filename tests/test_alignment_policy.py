import pytest
from backend.core.loop.alignment_policy import selected_modules, policy_text, AlignmentPolicyUnavailable, COMMON, MECHANISMS
from backend.core.prompt_templates import load_prompt, PromptTemplateError


def ordinary():
    return {'harness_semantics':{'task_id':'actual-task','state_available':True,
        'definitions_present':False,'instances':[],'authority_granted':False}}


def test_unused_mechanism_instructions_are_not_loaded_but_common_source_rules_remain():
    payload=ordinary()
    assert selected_modules(payload) == [COMMON]
    text=policy_text(payload)
    assert 'request_frame' in text and 'actual_answers' in text
    assert 'Assumptions are assistant defaults' in text
    assert 'target_publication comes from the immutable' not in text
    assert len(text) < len(policy_text({}))
    # Business wording cannot create or hide actual runtime state.
    assert selected_modules({**payload,'request':'Explain checkpoints and a lifecycle'}) == [COMMON]


@pytest.mark.parametrize('change', ['active','inactive','saved_definition','unavailable','unknown','proposal','future_proposal','malformed_proposal'])
def test_actual_or_unknown_mechanism_state_keeps_all_original_instruction_modules(change):
    payload=ordinary()
    if change in {'active','inactive'}:
        payload['harness_semantics']['instances']=[{'phase':'ready' if change=='active' else 'suspended'}]
    elif change=='unavailable':payload['harness_semantics']['state_available']=False
    elif change=='unknown':payload.pop('harness_semantics')
    elif change=='saved_definition':payload['harness_semantics']['definitions_present']=True
    elif change=='malformed_proposal':payload['proposed_contracts']=[{'kind':[]}]
    else:payload['proposed_contracts']=[{'kind':'harness_lifecycle' if change=='proposal' else 'future_operation'}]
    assert selected_modules(payload) == [COMMON,MECHANISMS]
    assert load_prompt(MECHANISMS) in policy_text(payload)


def test_required_module_failure_never_silently_omits_its_rules(monkeypatch):
    def fail(resource):
        if resource==MECHANISMS:raise PromptTemplateError('synthetic missing resource')
        return 'common'
    monkeypatch.setattr('backend.core.loop.alignment_policy.load_prompt',fail)
    assert policy_text(ordinary()) == 'common'
    with pytest.raises(AlignmentPolicyUnavailable) as error:policy_text({})
    assert error.value.resource == MECHANISMS


def test_local_policy_failure_has_its_own_code_and_never_calls_provider(monkeypatch, tmp_path_factory):
    from types import SimpleNamespace
    from tests.test_alignment_semantics import process
    from backend.core.loop.alignment_runtime import assess_with_provider
    from backend.core.loop.alignment_review import AlignmentReview, AlignmentReviewHooks
    from backend.core.loop.runtime_outcomes import _make_result
    from backend.core.loop.models import ProcessStatus
    p=process(tmp_path_factory);p.task_kind='plan';p.status=ProcessStatus.RUNNING
    calls=[]
    def fail(resource):raise PromptTemplateError('synthetic missing module')
    monkeypatch.setattr('backend.core.loop.alignment_policy.load_prompt',fail)
    client=SimpleNamespace(chat=lambda *a,**kw:calls.append('paid request'))
    provider=SimpleNamespace(assess_alignment=lambda payload,**kw:assess_with_provider(client,payload,**kw))
    noop=lambda *a,**kw:None
    hooks=AlignmentReviewHooks(emit=noop,frontend_emit=None,notice=noop,refresh_prompt=noop,
        before_attempt=noop,route_notice=noop,cancellation=noop,budget_exhausted=noop,
        result=lambda response,**kw:_make_result(p,p.session,response,**kw))
    result=AlignmentReview(p,provider,hooks).check('Return the current plan.',candidate='A proposed plan.')
    assert not calls and result['status']=='failed'
    failure=result['error']['details']['details']['failure']
    assert failure['code']=='ALIGNMENT_POLICY_UNAVAILABLE'
    assert failure['stage']=='requirement_review_input'
    assert failure['execution_state']=='review_request_not_started'
