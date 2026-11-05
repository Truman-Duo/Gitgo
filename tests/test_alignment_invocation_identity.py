"""Canonical interpretation records retain exact identity and source guards."""
import copy
import pytest

from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.decision_support import confirmed_user_state
from tests.test_alignment_semantics import process, pending_reply, interpretation


@pytest.mark.parametrize('outer',['node_only','record_only','both'])
def test_full_interpretation_record_can_supply_its_own_exact_identity(tmp_path_factory,outer):
    p=process(tmp_path_factory)
    w,card,node=pending_reply(p,'Now implement the agreed work')
    record=interpretation(node,card,'Now implement the agreed work')
    args={'operation':'resolve_answer','content':record}
    if outer in {'node_only','both'}:args['node_id']=node
    if outer=='both':args['decision_id']=card['decision_id']
    result=w.invoke(args)
    assert not result.get('error'),result
    assert w.status()['complete']
    assert confirmed_user_state(p.session.host_ledger)[0]['decision_id']==card['decision_id']
    assert not p.approval_grants


@pytest.mark.parametrize('field',['node_id','decision_id'])
def test_conflicting_outer_and_record_identity_cannot_choose_either_silently(tmp_path_factory,field):
    p=process(tmp_path_factory)
    w,card,node=pending_reply(p,'Actual business reply')
    record=interpretation(node,card,'Actual business reply')
    args={'operation':'resolve_answer','node_id':node,'decision_id':card['decision_id'],'content':record}
    args[field]='different-identity'
    before=copy.deepcopy(w._load())
    result=w.invoke(args)
    assert result.get('error') and 'conflict' in result['detail'].lower()
    assert w._load()==before and not confirmed_user_state(p.session.host_ledger) and not p.approval_grants


def test_canonical_record_still_requires_actual_quoted_user_source(tmp_path_factory):
    p=process(tmp_path_factory)
    w,card,node=pending_reply(p,'Keep waiting')
    result=w.invoke({'operation':'resolve_answer','content':interpretation(node,card,'Invented permission')})
    assert result.get('error') and not w.status()['complete']
    assert not confirmed_user_state(p.session.host_ledger) and not p.approval_grants


def test_canonical_record_resolves_unasked_facet_from_exact_actual_round_source(tmp_path_factory):
    from tests.test_alignment_business_receipts import receive
    from backend.core.loop.decision_support import create_user_decision
    p=process(tmp_path_factory)
    w=EngineeringWorkflow(p)
    w.configure({'profiles':['alignment'],'nodes':[{'id':'next_stage','kind':'decision',
        'state_topic':'next-stage','before_mutation':True}]})
    original='actual_source'
    card=create_user_decision(p,{'kind':'clarification','question':'When should work start?',
        'why_user_must_decide':'The user controls timing','questions':[{'node_id':original,'state_topic':'timing',
            'question':'When should work start?','why_user_must_decide':'The user controls timing','options':[]}]})
    text='Now start the agreed work and keep the original checks.'
    receive(p,card,text)
    result=w.invoke({'operation':'resolve_answer','node_id':'next_stage',
        'content':interpretation('next_stage',card,text)})
    assert not result.get('error'),result
    assert w.status()['complete'] and not p.pending_decision and not p.approval_grants
    event=next(e for e in reversed(p.session.host_ledger) if e.get('event')=='user_decision_interpreted' and e.get('node_id')=='next_stage')
    assert event['decision_id']==card['decision_id'] and event['source_node_id']==original


def test_configure_missing_plan_identifies_the_argument_location(tmp_path_factory):
    p=process(tmp_path_factory)
    w=EngineeringWorkflow(p)
    before=copy.deepcopy(p.context_snapshot)
    result=w.invoke({'operation':'configure','content':{'profiles':['retrospective']}})
    assert result.get('error') and 'top-level plan' in result['detail']
    assert not w.status()['active'] and p.context_snapshot==before
