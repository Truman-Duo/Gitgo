"""A semantic certificate must bind the actual compiled operation proposal."""
from copy import deepcopy
from tests.test_mechanism_interactions import runtime, checkpoint_program
from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.alignment_routing import reuse_assessment


def test_changed_composition_cannot_reuse_the_previous_programs_certificate(runtime):
    app,p,*_=runtime
    alignment=AlignmentRuntime(p)
    raw=checkpoint_program(p)
    first={'kind':'harness_composition',**app.semantic_preview({'program':raw})}
    payload=alignment.payload(proposed_contracts=[first])
    alignment.apply({'status':'ready','questions':[],'interpretations':[]},payload)
    assert reuse_assessment(p,payload)=='unchanged_admission'
    changed=deepcopy(raw)
    next(n for n in changed['elements'] if n['id']=='question')['parameters']['question']='A different proposed prediction question'
    other={'kind':'harness_composition',**app.semantic_preview({'program':changed})}
    assert other['compiled']['digest']!=first['compiled']['digest']
    assert reuse_assessment(p,alignment.payload(proposed_contracts=[other]))=='', 'Different mechanism proposals must receive a fresh semantic assessment'
    assert reuse_assessment(p,alignment.payload())=='', 'A mechanism proposal review does not certify general business readiness'


def test_every_operation_in_the_batch_is_bound_not_only_the_last(runtime):
    app,p,*_=runtime
    alignment=AlignmentRuntime(p)
    raw=checkpoint_program(p)
    first={'kind':'harness_composition',**app.semantic_preview({'program':raw})}
    last={'goal':'The same ordinary task contract','execution_mode':'answer'}
    payload=alignment.payload(proposed_contracts=[first,last])
    alignment.apply({'status':'ready','questions':[],'interpretations':[]},payload)
    changed=deepcopy(first);changed['compiled']['intent']='A different requested behavior'
    assert reuse_assessment(p,alignment.payload(proposed_contracts=[changed,last]))==''


def test_single_ordinary_contract_still_certifies_its_committed_state(runtime):
    _,p,*_=runtime
    alignment=AlignmentRuntime(p)
    proposed={'goal':'The original requested work','execution_mode':'answer'}
    payload=alignment.payload(proposed_contracts=[proposed])
    alignment.apply({'status':'ready','questions':[],'interpretations':[]},payload)
    p.update_context_snapshot(lambda ctx:{**ctx,'task_contract':deepcopy(proposed)})
    assert reuse_assessment(p,alignment.payload())=='unchanged_admission'


def test_contract_readiness_cannot_release_business_before_current_prerequisites_review(runtime):
    from backend.core.loop.tool_boundary_preview import preview_tool_boundaries
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.alignment_routing import REVIEW_POLICY
    _, p, effects, tool, *_ = runtime
    alignment = AlignmentRuntime(p)
    proposed = {'goal': 'Set up requested behavior before the real operation', 'execution_mode': 'answer'}
    alignment.apply({'status': 'ready', 'questions': [], 'interpretations': []},
        alignment.payload(proposed_contracts=[proposed]))
    p.update_context_snapshot(lambda ctx: {**ctx, 'task_contract': deepcopy(proposed),
        'alignment_runtime': {**ctx['alignment_runtime'], 'review_policy': REVIEW_POLICY}})
    call = {'name': tool.name, 'args': {'value': 'first'}}
    boundary = preview_tool_boundaries([call], {tool.name: tool}, p)
    assert boundary.contracts[0]['kind'] == 'business_readiness'
    assert 'first' in boundary.contracts[0]['arguments_preview']
    payload = alignment.payload(proposed_contracts=boundary.contracts)
    assert reuse_assessment(p, payload) == ''
    assert ToolPipeline._alignment_admission_error(p, tool, call['args'])[0] == 'ALIGNMENT_REVIEW_REQUIRED'
    assert effects == [] and not p.approval_grants
    # This fixture supplies a semantic outcome; production uses the existing
    # assessor. It certifies readiness of task meaning, not operation authority.
    alignment.apply({'status': 'ready', 'questions': [], 'interpretations': []}, payload)
    assert ToolPipeline._alignment_admission_error(p, tool, call['args']) is None
    another = preview_tool_boundaries([{'name': tool.name, 'args': {'value': 'next'}}], {tool.name: tool}, p)
    assert reuse_assessment(p, alignment.payload(proposed_contracts=another.contracts)) == 'unchanged_admission'
    assert effects == [] and not p.approval_grants
    p.task_constraints.append('New actual task constraint')
    assert reuse_assessment(p, alignment.payload(proposed_contracts=another.contracts)) == ''
    assert ToolPipeline._alignment_admission_error(p, tool, call['args'])[0] == 'ALIGNMENT_REVIEW_REQUIRED'


def test_runtime_control_preparation_does_not_claim_business_readiness(runtime):
    from backend.core.loop.agent_tool import AgentTool, ToolEffect
    from backend.core.loop.execution_contract import data_broker
    from backend.core.loop.tool_boundary_preview import preview_tool_boundaries
    _, p, *_ = runtime
    control = AgentTool(name='fixture-control', description='Prepare runtime state',
        parameters={'type': 'object', 'properties': {}}, effect=ToolEffect.PROCESS,
        execution_contract=data_broker('host.runtime.fixture'), execute=lambda args: {})
    boundary = preview_tool_boundaries([{'name': control.name, 'args': {}}], {control.name: control}, p)
    assert boundary.effectful and not boundary.contracts
