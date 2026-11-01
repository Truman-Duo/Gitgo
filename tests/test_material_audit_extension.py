"""Component fixtures, not real Provider or independent human evaluations."""
from types import SimpleNamespace
import pytest

from backend.core.mechanism_runtime.extensions.material_audit import audit


def run(*, canonical=True, foreign=False, partial=False, include_answered=True):
    calls=[]
    task='other-task' if foreign else 'task-a'
    def call(service, payload):
        calls.append((service,payload))
        if service=='context.project': return {}
        return {'task_id':task, 'questions': {'partial':partial, 'items':[
            {'task_id':task,'decision_id':'q1','status':'awaiting_user','materials':[{'reference':'discussion:one'}]},
            {'task_id':task,'decision_id':'q0','status':'received','materials':[{'reference':'discussion:old'}]}]},
            'material_facts':[{'task_id':task,'decision_id':'q1' if payload.get('material_reference')=='discussion:one' else 'q0',
                'event':'user_decision_material_attached','reference':payload.get('material_reference')}]
                if canonical and payload.get('material_reference') else []}
    ctx=SimpleNamespace(task_id='task-a',state={},boundary='restored',check=lambda:None,call=call)
    result=audit(ctx,{'max_questions':4,'max_materials':2,'include_answered':include_answered},{})
    return result,calls


def test_material_audit_observes_canonical_links_and_keeps_answered_status():
    result,calls=run()
    links=result.outputs['report']['material_links']
    assert {x['question_status'] for x in links}=={'awaiting_user','received'}
    assert all(x['canonical_link_observed'] and x['fact_digest'] for x in links)
    assert {s for s,_ in calls}=={'decision.read','context.project'}
    assert result.outputs['report']['authority_granted'] is False
    assert result.state['checks']==1


def test_material_audit_missing_canonical_fact_stays_uncertain():
    result,_=run(canonical=False,partial=True,include_answered=False)
    report=result.outputs['report']
    assert report['history_or_selection_partial']
    assert len(report['material_links'])==1
    assert report['material_links'][0]['requires_source_inspection']
    assert report['material_links'][0]['canonical_link_observed'] is False


def test_material_audit_rejects_cross_task_projection():
    with pytest.raises(ValueError,match='MATERIAL_AUDIT_SCOPE_CHANGED'):
        run(foreign=True)
