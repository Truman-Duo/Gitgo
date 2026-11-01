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
