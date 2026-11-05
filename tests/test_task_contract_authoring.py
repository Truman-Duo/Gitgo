"""Concise authoring follows canonical validation; examples must really compile."""
import json
import re
from copy import deepcopy
import pytest
from jsonschema import Draft202012Validator
from tests.test_task_contract_activation import runtime, revision_tool, proposal, isolated_pipeline_runtime
from backend.core.loop.task_contract import publish_contract, get_task_contract, validate_contract_proposal
from backend.core.loop.engineering_workflow import plan_schema, compile_plan
from backend.core.prompt_templates import load_prompt


def examples():
    return [json.loads(s) for s in re.findall(r'```json\s*(.*?)\s*```', load_prompt('tools/declare_task_contract'), re.S)]


def test_all_general_prompt_examples_pass_the_advertised_and_canonical_compilers(runtime):
    _,p,store,root=runtime
    tool=revision_tool(p,root)
    schema=Draft202012Validator(tool.to_openai_function()['function']['parameters'])
    for args in examples():
        schema.validate(args)
        if args.get('operation')=='revise':
            publish_contract(p,proposal(root,uncertainties=['Real pending choice'],requires_user_decision=True),store=store)
        bound=tool.prepare_args(args)
        value=validate_contract_proposal(bound,str(root))
        if args.get('operation')=='revise':
            assert value['uncertainties']==['Real pending choice'] and value['requires_user_decision']
        if args.get('uncertainties'):
            assert value['requires_user_decision']
        if args.get('engineering_workflow'):
            nodes=value['engineering_workflow']['nodes']
            assert [n['kind'] for n in nodes]==['check','change','check']
            assert nodes[1]['files']==['src/parser.py']
        assert not p.approval_grants and p.capability_lease is None


def test_runtime_initial_defaults_do_not_clear_existing_or_explicit_choices(runtime):
    _,p,store,root=runtime;tool=revision_tool(p,root)
    args=examples()[0]
    before=deepcopy(args)
    bound=tool.prepare_args({**args,'uncertainties':['Choose a policy']})
    assert bound['requires_user_decision'] and args==before
    publish_contract(p,proposal(root,uncertainties=['Keep me'],requires_user_decision=True),store=store)
    amended=tool.prepare_args({'operation':'revise','append':{'acceptance_criteria':['New check']}})
    assert amended['uncertainties']==['Keep me'] and amended['requires_user_decision']
    with pytest.raises(ValueError,match='user_request_evidence'):
        validate_contract_proposal(tool.prepare_args({'operation':'revise',
            'execution_mode':'delegate','user_requested_delegation':True}),str(root))


def test_schema_explains_nodes_and_presets_do_not_guess_missing_tests(runtime):
    *_,root=runtime;schema=Draft202012Validator(plan_schema())
    for kind in ['change','check','decision','document','report','observation']:
        assert not schema.is_valid({'profiles':['alignment'],'nodes':[{'id':'incomplete','kind':kind}]})
    with pytest.raises(ValueError,match='concrete registered test_id'):
        compile_plan({'profiles':['tdd'],'target_files':['app.py']},root)
    with pytest.raises(ValueError,match="change node 'setup' requires files"):
        compile_plan({'profiles':['alignment'],'nodes':[{'id':'setup','kind':'change'}]},root)
