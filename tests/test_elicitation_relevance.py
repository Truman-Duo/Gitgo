import copy
import pytest

from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.decision_support import confirmed_user_state
from tests.test_alignment_semantics import process


def prepared(root, *, dependent=False):
    p = process(root)
    p.task_kind = 'answer'
    runtime = AlignmentRuntime(p)
    questions = [{'state_topic': topic, 'question': question, 'tier': 'foundational',
        'why_user_must_decide': 'Affects the requested deliverable'} for topic,question in (
            ('format','Which output format?'), ('palette','Which web palette?'))]
    if dependent:
        questions.append({'state_topic':'contrast','question':'What contrast?',
            'why_user_must_decide':'Affects the web palette', 'depends_on_topics':['palette']})
    request = runtime.apply({'status':'needs_input','questions':questions,'interpretations':[]},
        runtime.payload('Discuss the output before implementation'))['decision']
    w = EngineeringWorkflow(p)
    nodes = {n['state_topic']:n['id'] for n in w.status()['nodes']}
    answer = '纯文本邮件，不制作网页。'
    p.session.host_ledger.append({'event':'user_decision_received','task_id':'t','process_id':'p',
        'decision_id':request['decision_id'],'kind':'clarification',
        'answers':{nodes['format']:{'answer':answer}}})
    p.pending_decision = None
    retirement = {'node_id':nodes['palette'], 'decision_id':request['decision_id'],
        'source_node_id':nodes['format'],'quote':'不制作网页','reason':'A web palette no longer affects a plain-text email'}
    candidate = 'Deliver a plain-text email; no web design has been selected or authorized.'
    assessment = {'status':'ready','questions':[], 'interpretations':[{
        'node_id':nodes['format'],'decision_id':request['decision_id'],'source_node_id':nodes['format'],
        'quote':answer,'value':answer,'disposition':'confirmed'}],
        'retire_questions':[retirement], 'delivery_check':{'complete':True,'evidence':[candidate]}}
    return p,runtime,w,nodes,assessment,candidate


def test_obsolete_model_question_can_close_read_only_without_fabricating_an_answer(tmp_path_factory):
    p,runtime,w,nodes,assessment,candidate = prepared(tmp_path_factory)
    assert runtime.apply(assessment, runtime.payload(candidate=candidate))['status'] == 'ready'
    graph = {n['id']:n for n in w.status()['nodes']}
    assert graph[nodes['palette']]['status'] == 'retired'
    assert graph[nodes['format']]['status'] == 'complete'
    assert not w.completion_reasons()
    assert w.status()['complete']
    assert not w.guard('write_file','workspace_write',{'path':'new.txt'})['allowed']
    assert {s['topic'] for s in confirmed_user_state(p.session.host_ledger)} == {'format'}
    assert not any(e.get('event') == 'user_decision_interpreted' and e.get('node_id') == nodes['palette']
                   for e in p.session.host_ledger)
    # Every new actual reply invalidates the bounded relevance certificate.
    old_request = copy.deepcopy(next(e for e in p.session.host_ledger if e.get('event') == 'user_decision_requested'))
    old_request['decision_id'] = 'new-actual-round'
    p.session.host_ledger.extend([old_request, {'event':'user_decision_received','task_id':'t','process_id':'p',
        'decision_id':'new-actual-round','kind':'clarification','answers':{nodes['format']:{'answer':'改成网页。'}}}])
    assert next(n for n in w.status()['nodes'] if n['id'] == nodes['palette'])['status'] == 'ready'
    assert w.completion_reasons()


@pytest.mark.parametrize('failure', ['user_obligation','false_quote','foreign_process','active_dependent','execution','adaptive_execution'])
def test_retirement_is_source_bound_atomic_and_cannot_waive_other_authorities(tmp_path_factory, failure):
    p,runtime,w,nodes,assessment,candidate = prepared(tmp_path_factory, dependent=failure=='active_dependent')
    if failure == 'user_obligation':
        state=w._load();state['elicitation_tiers'].pop(nodes['palette']);w._save(state)
    elif failure == 'false_quote':
        assessment['retire_questions'][0]['quote']='invented user approval'
    elif failure == 'foreign_process':
        p.session.host_ledger[-1]['process_id']='other'
        assessment['interpretations']=[]
    elif failure == 'execution':
        p.task_kind='action'
    elif failure == 'adaptive_execution':
        p.update_context_snapshot(lambda ctx:{**ctx,'task_contract':{'execution_mode':'self_execute'}})
    payload=runtime.payload(candidate=candidate)
    before=copy.deepcopy(p.context_snapshot),copy.deepcopy(p.session.host_ledger)
    with pytest.raises(ValueError,match='QUESTION_RETIREMENT_INVALID'):
        runtime.apply(assessment,payload)
    assert (p.context_snapshot,p.session.host_ledger) == before
