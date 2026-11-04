"""New after v25 freeze: observe received-vs-confirmed answer boundaries."""
import json
from backend.core.mechanism_runtime.contracts import Element, Port, digest
from backend.core.mechanism_runtime.runner import NodeResult


def audit(ctx, params, inputs):
    ctx.check()
    if ctx.state.get('task_id',ctx.task_id) != ctx.task_id:
        raise ValueError('RECEIPT_AUDIT_SCOPE_CHANGED')
    offset, confirmed, questions = 0, set(), None
    for _ in range(params['max_pages']):
        page=ctx.call('decision.read',{'offset':offset,'limit':32})
        if page['task_id'] != ctx.task_id:
            raise ValueError('RECEIPT_AUDIT_SCOPE_CHANGED')
        confirmed.update(item['decision_id'] for item in page['confirmed'])
        questions=page['questions']
        offset=page['next_offset']
        if offset is None:break
    records=[]
    for item in questions['items']:
        if item['task_id'] != ctx.task_id:
            raise ValueError('RECEIPT_AUDIT_SCOPE_CHANGED')
        if params['kinds'] and item['kind'] not in params['kinds']:continue
        records.append({'decision_id':item['decision_id'],'kind':item['kind'],'status':item['status'],
            'answer_state':item['answer_state'],'answer_truncated':item['answer_truncated'],
            'visible_answer_digest':digest(item['answer']) if item['answer_state']=='received' else None,
            'confirmed_in_current_canonical_page':item['decision_id'] in confirmed})
    partial=questions['partial'] or offset is not None
    versions={r['decision_id']:digest(r) for r in records}
    prior=ctx.state.get('versions',{})
    report={'task_id':ctx.task_id,'records':records,'partial':partial,
        'received_not_confirmed_in_current_page':[r['decision_id'] for r in records
            if r['answer_state']=='received' and not r['confirmed_in_current_canonical_page']],
        'changed_visible_receipts':[key for key in sorted(prior.keys() & versions.keys()) if prior[key]!=versions[key]],
        'authority_granted':False,'scope':'bounded canonical received-answer projection; digest is of visible prefix only when truncated; absence from a partial page proves neither retirement nor lack of confirmation'}
    ctx.call('context.project',{'content':'Answer receipt boundary audit: '+json.dumps(report,ensure_ascii=False,separators=(',',':')),
        'include_event':False})
    return NodeResult({'checked':True,'report':report},{'task_id':ctx.task_id,
        'versions':prior if partial else versions,'checks':ctx.state.get('checks',0)+1})


def install(registry):
    return registry.register(Element('question.receipt_state_audit',1,
        {'type':'object','properties':{'max_pages':{'type':'integer','minimum':1,'maximum':4,'default':2},
            'kinds':{'type':'array','items':{'type':'string'},'maxItems':16,'uniqueItems':True,'default':[]}},
            'additionalProperties':False},
        (Port('go','trigger'),Port('event','record',False)),(Port('checked','trigger'),Port('report','record')),
        ('after_compaction','restored'),audit,effect='projection',capabilities=('decision.read','context.project'),
        description='On compaction/restore, compare real received answer states with the current canonical confirmed-decision page. Fingerprint visible reply prefixes, preserving truncation and paging limits. Receiving is not confirmation or permission. Configurable kind filter; no answers, execution, authority changes or subscriptions.'))
