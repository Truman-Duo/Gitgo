"""New after v18 freeze: bounded lifecycle observation of confirmed kinds."""
import json
from backend.core.mechanism_runtime.contracts import Element,Port,digest
from backend.core.mechanism_runtime.runner import NodeResult
def observe(ctx,params,inputs):
    ctx.check();offset=0;items=[]
    for _ in range(params['max_pages']):
        page=ctx.call('decision.read',{'offset':offset,'limit':32})
        if page['task_id']!=ctx.task_id:raise ValueError('CONFIRMED_KIND_SCOPE_CHANGED')
        items.extend(page['confirmed']);offset=page['next_offset']
        if offset is None:break
    selected=[v for v in items if not params['include_kinds'] or v['kind'] in params['include_kinds']]
    counts={}
    for value in selected:counts[value['kind']]=counts.get(value['kind'],0)+1
    report={'task_id':ctx.task_id,'counts':counts,'records':[{'decision_id':v['decision_id'],'version':digest(v)} for v in selected],
        'partial':offset is not None,'include_kinds':params['include_kinds'],'authority_granted':False,
        'scope':'canonical current-task confirmed decisions; observation only'}
    ctx.call('context.project',{'content':'Confirmed kind observation: '+json.dumps(report,ensure_ascii=False,separators=(',',':')),'include_event':False})
    return NodeResult({'checked':True,'report':report},{'checks':ctx.state.get('checks',0)+1,'last_report':report})
def install(registry):
    return registry.register(Element('decision.confirmed_kinds',1,{'type':'object','properties':{
        'include_kinds':{'type':'array','items':{'type':'string','minLength':1},'maxItems':8,'uniqueItems':True,'default':[]},
        'max_pages':{'type':'integer','minimum':1,'maximum':4,'default':2}},'additionalProperties':False},
        (Port('go','trigger'),Port('event','record',False)),(Port('checked','trigger'),Port('report','record')),
        ('after_compaction','restored'),observe,effect='projection',capabilities=('decision.read','context.project'),
        description='At compaction/restore, observe counts, IDs and exact value versions of canonical current-task confirmed decisions, grouped by kind. Bounded pages stay partial; no answers, permissions, subscriptions or execution control.'))
