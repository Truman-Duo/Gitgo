"""New after v17 freeze; lifecycle read-only category observation via SDK."""
import json
from backend.core.mechanism_runtime.contracts import Element,Port
from backend.core.mechanism_runtime.runner import NodeResult
def summarize(ctx,parameters,inputs):
    ctx.check()
    page=ctx.call('decision.read',{'offset':0,'limit':1})
    if page['task_id']!=ctx.task_id:raise ValueError('QUESTION_KIND_SCOPE_CHANGED')
    counts={}
    for q in page['questions']['items']:
        if q['task_id']!=ctx.task_id:raise ValueError('QUESTION_KIND_SCOPE_CHANGED')
        if not parameters['include_kinds'] or q['kind'] in parameters['include_kinds']:
            key=q['kind']+':'+q['status'];counts[key]=counts.get(key,0)+1
    report={'task_id':ctx.task_id,'counts':counts,'partial':page['questions']['partial'],
        'include_kinds':parameters['include_kinds'],'authority_granted':False,'scope':'current-task visible questions; observation only'}
    if counts or parameters['emit_empty']:
        ctx.call('context.project',{'content':'Question kind observation: '+json.dumps(report,ensure_ascii=False,separators=(',',':')),'include_event':False})
    return NodeResult({'checked':True,'report':report},{'checks':ctx.state.get('checks',0)+1,'last_report':report})
def install(registry):
    return registry.register(Element('decision.kind_summary',1,{'type':'object','properties':{
        'include_kinds':{'type':'array','items':{'type':'string','minLength':1},'maxItems':8,'uniqueItems':True,'default':[]},
        'emit_empty':{'type':'boolean','default':True}},'additionalProperties':False},
        (Port('go','trigger'),Port('event','record',False)),(Port('checked','trigger'),Port('report','record')),
        ('restored','after_compaction'),summarize,effect='projection',capabilities=('decision.read','context.project'),
        description='At restore/compaction, observe category/status counts of current-task visible questions. Configurable kinds; no answers, permissions, subscriptions or execution control. Partial history remains marked partial.'))
