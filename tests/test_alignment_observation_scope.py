"""Current read-only substeps retain the original development gates."""
import copy
import pytest
from tests.test_alignment_semantics import process,pending_reply,interpretation
from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.alignment_evidence import exploratory_scope
from backend.core.loop.alignment_routing import reuse_assessment
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.completion_protocol import HostCompletionEvaluator

def setup(root):
    p=process(root);p.task_kind='supervisor';p.required_test_ids=['required-unit-test']
    runtime=AlignmentRuntime(p);runtime.payload('Implement the agreed change, preserve original tests')
    w,card,node=pending_reply(p,'Decide implementation later. First inspect current state without changing anything.')
    text=w._answers(w.status()['nodes'][0])[-1]['answer']
    proposal={'status':'ready','questions':[],
        'interpretations':[interpretation(node,card,text,'deferred','Implementation timing undecided')],
        'scope_update':{'mode':'exploration','quote':text,'request':text,'decision_id':card['decision_id']}}
    return p,runtime,w,proposal,node,card

def test_read_substep_is_ready_without_resolving_or_completing_original_task(tmp_path_factory):
    p,runtime,w,proposal,node,card=setup(tmp_path_factory)
    out=runtime.apply(proposal,runtime.payload(observation_only=True))
    assert out['status']=='ready' and exploratory_scope(p,admission=True) and not exploratory_scope(p)
    assert node not in w._load()['records'] and not w.status()['complete']
    assert w.completion_reasons() and not HostCompletionEvaluator.evaluate(p,'Read-only inspection finished').allowed
    assert p.required_test_ids==['required-unit-test'] and not p.pending_decision
    assert not w.guard('unit_write','workspace_write')['allowed']
    assert reuse_assessment(p,runtime.payload(observation_only=True))=='unchanged_admission'
    assert reuse_assessment(p,runtime.payload())==''
    assert reuse_assessment(p,runtime.payload(candidate='All work complete'))==''
    for payload in (runtime.payload(),runtime.payload(candidate='All work complete')):
        with pytest.raises(ValueError,match='discard unresolved'):
            runtime.apply({'status':'ready','questions':[],'interpretations':[]},payload)

@pytest.mark.parametrize('kind',['action','supervisor'])
def test_non_observation_boundary_cannot_create_scope_or_partially_interpret(tmp_path_factory,kind):
    p,runtime,w,proposal,node,_=setup(tmp_path_factory);p.task_kind=kind
    payload=runtime.payload();before=copy.deepcopy(p.context_snapshot);ledger=copy.deepcopy(p.session.host_ledger)
    with pytest.raises(ValueError,match='read-only task'):runtime.apply(proposal,payload)
    assert p.context_snapshot==before and p.session.host_ledger==ledger and node not in w._load()['records']

def test_later_business_reply_invalidates_read_substep_but_control_answers_do_not(tmp_path_factory):
    p,runtime,w,proposal,node,_=setup(tmp_path_factory)
    runtime.apply(proposal,runtime.payload(observation_only=True))
    p.session.host_ledger.extend([{'event':'user_decision_requested','task_id':'t','kind':'permission','decision_id':'control'},
        {'event':'user_decision_received','task_id':'t','decision_id':'control','answer':'unit fixture'}])
    assert exploratory_scope(p,admission=True)
    p.session.host_ledger.extend([{'event':'user_decision_requested','task_id':'t','kind':'clarification','decision_id':'later'},
        {'event':'user_decision_received','task_id':'t','decision_id':'later','answer':'Now implement'}])
    assert not exploratory_scope(p,admission=True)
    with pytest.raises(ValueError,match='discard unresolved'):
        runtime.apply({'status':'ready','questions':[],'interpretations':[]},runtime.payload(observation_only=True))

def test_boundary_classifier_requires_every_call_to_be_observation_only():
    from backend.core.loop.agent_tool import AgentTool,ToolEffect
    from backend.core.loop.tool_boundary_preview import preview_tool_boundaries
    tool=lambda name,effect,read:AgentTool(name,'Unit fixture',{},lambda args:{},effect=effect,read_only=read)
    tools={'read':tool('read',ToolEffect.READ,True),'write':tool('write',ToolEffect.WORKSPACE_WRITE,False),
        'misdeclared':tool('misdeclared',ToolEffect.PROCESS,True)}
    calls=lambda *names:[{'name':n,'args':{}} for n in names]
    assert preview_tool_boundaries(calls('read'),tools).observation_only
    for names in [('read','write'),('misdeclared',),('unknown',)]:
        assert not preview_tool_boundaries(calls(*names),tools).observation_only
