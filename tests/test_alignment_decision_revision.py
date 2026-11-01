"""Synthetic source-bound revision fixtures; not real API or human labels."""
import copy
import pytest
from tests.test_alignment_semantics import process,pending_reply,interpretation
from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.decision_support import confirmed_user_state

def confirmed(root):
    p=process(root);runtime=AlignmentRuntime(p);runtime.payload('Plan a trip')
    w,request,node=pending_reply(p,'Japan with family')
    runtime.apply({'status':'ready','interpretations':[interpretation(node,request,'Japan with family')],'questions':[]},runtime.payload())
    return p,runtime,w,request,node

def revision(request,node):
    return {'proposal_index':0,'state_topic':'trip','node_id':node,'tier':'foundational',
        'question':'Reconsider the trip?','why_user_must_decide':'The user requested a changed destination',
        'supersedes_decision_id':request['decision_id']}

def test_selected_question_keeps_revision_identity_and_publishes_real_successor(tmp_path_factory):
    p,runtime,w,old,node=confirmed(tmp_path_factory)
    question=revision(old,node)
    proposed={k:v for k,v in question.items() if k not in ('proposal_index','tier','node_id')}
    payload=runtime.payload(proposed_questions=[proposed])
    result=runtime.apply({'status':'needs_input','interpretations':[],'questions':[question]},payload)
    card=result['decision']
    assert card['decision_id']!=old['decision_id'] and card['supersedes_decision_id']==old['decision_id']
    assert card['question']==proposed['question'] and p.pending_decision['decision_id']==card['decision_id']
    assert not w.status()['complete'] and not confirmed_user_state(p.session.host_ledger)
    p.session.host_ledger.append({'event':'user_decision_received','task_id':'t','decision_id':card['decision_id'],
        'state_topic':'trip','answer':'Italy with family'})
    p.pending_decision=None
    runtime.apply({'status':'ready','interpretations':[interpretation(node,card,'Italy with family')],'questions':[]},runtime.payload())
    current=confirmed_user_state(p.session.host_ledger)
    assert len(current)==1 and current[0]['decision_id']==card['decision_id'] and current[0]['value']=='Italy with family'
    assert current[0]['supersedes_decision_id']==old['decision_id']
    assert any(e.get('decision_id')==old['decision_id'] and e.get('event')=='user_decision_interpreted' for e in p.session.host_ledger)
    old_event=next(e for e in p.session.host_ledger if e.get('decision_id')==old['decision_id'] and e.get('event')=='user_decision_interpreted')
    p.session.host_ledger.append(copy.deepcopy(old_event))
    assert confirmed_user_state(p.session.host_ledger)[0]['decision_id']==card['decision_id']

@pytest.mark.parametrize('mutation',['stale','missing','later-invalid'])
def test_invalid_revision_never_partially_invalidates_confirmed_state(tmp_path_factory,mutation):
    p,runtime,w,old,node=confirmed(tmp_path_factory);q=revision(old,node)
    if mutation=='stale':q['supersedes_decision_id']='not-the-current-id'
    if mutation=='missing':q.pop('supersedes_decision_id')
    proposal={'status':'needs_input','interpretations':[],'questions':[q]}
    if mutation=='later-invalid':proposal['assumptions']=[{'state_topic':'bad','value':'invented','reason':'fixture','tier':'foundational'}]
    payload=runtime.payload(proposed_questions=[{'question':q['question'],'why_user_must_decide':q['why_user_must_decide']}])
    before=copy.deepcopy(p.context_snapshot);ledger=copy.deepcopy(p.session.host_ledger)
    with pytest.raises(ValueError):runtime.apply(proposal,payload)
    assert p.context_snapshot==before and p.session.host_ledger==ledger and p.pending_decision is None
    assert w.status()['complete'] and confirmed_user_state(p.session.host_ledger)[0]['decision_id']==old['decision_id']

def test_conflicting_selected_source_and_assessor_revision_are_rejected():
    from backend.core.loop.alignment_question import question_request
    with pytest.raises(ValueError,match='different previous decision'):
        question_request({'proposal_index':0,'supersedes_decision_id':'another-id'},
            {'stage':'question_proposal','proposed_questions':[{'question':'Revise?','why_user_must_decide':'Requested',
                'supersedes_decision_id':'current-id'}]})

@pytest.mark.parametrize('case',['cancelled-publication','other-task','different-previous-id'])
def test_non_effective_or_foreign_publication_does_not_retire_current_fact(tmp_path_factory,case):
    p,_,_,old,_=confirmed(tmp_path_factory)
    event={'event':'user_decision_requested','decision_id':'unit-successor','task_id':'t','kind':'clarification',
        'state_topic':'trip','supersedes_decision_id':old['decision_id']}
    if case=='other-task':event['task_id']='other-task'
    if case=='different-previous-id':event['supersedes_decision_id']='unrelated-id'
    p.session.host_ledger.append(event)
    if case=='cancelled-publication':p.session.host_ledger.append({'event':'user_decision_cancelled','decision_id':'unit-successor'})
    assert confirmed_user_state(p.session.host_ledger)[0]['decision_id']==old['decision_id']
