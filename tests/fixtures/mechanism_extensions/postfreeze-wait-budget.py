"""New post-v16 lifecycle component; read-only observation, no wait enforcement."""
import json
from backend.core.mechanism_runtime.contracts import Element,Port
from backend.core.mechanism_runtime.runner import NodeResult

def inspect_waits(ctx,params,inputs):
    ctx.check()
    page=ctx.call('decision.read',{'offset':0,'limit':1})
    questions=page['questions']
    if page['task_id']!=ctx.task_id or any(q['task_id']!=ctx.task_id for q in questions['items']):
        raise ValueError('WAIT_BUDGET_SCOPE_CHANGED')
    waiting=sorted(q['decision_id'] for q in questions['items'] if q['status']=='awaiting_user'
        and (not params['kinds'] or q['kind'] in params['kinds']))
    exceeded=True if len(waiting)>params['max_pending'] else None if questions['partial'] else False
    report={'task_id':ctx.task_id,'pending_decision_ids':waiting,'observed_pending':len(waiting),
        'max_pending':params['max_pending'],'over_budget':exceeded,'history_partial':questions['partial'],
        'scope':'canonical current-task visible pending questions; observation only','authority_granted':False}
    ctx.call('context.project',{'content':'Pending decision budget audit: '+json.dumps(report,ensure_ascii=False,separators=(',',':')),
        'include_event':False})
    return NodeResult({'checked':True,'report':report},{'checks':ctx.state.get('checks',0)+1,
        'task_id':ctx.task_id,'last_report':report})

def install(registry):
    return registry.register(Element('decision.wait_budget',1,{'type':'object','properties':{
        'max_pending':{'type':'integer','minimum':0,'maximum':8,'default':1},
        'kinds':{'type':'array','items':{'type':'string','minLength':1},'maxItems':8,'uniqueItems':True,'default':[]}},
        'additionalProperties':False},(Port('go','trigger'),Port('event','record',False)),
        (Port('checked','trigger'),Port('report','record')),('after_compaction','restored'),inspect_waits,
        effect='projection',capabilities=('decision.read','context.project'),
        description='At compaction/restore, observe this task’s actual pending-question count against a configured observation budget. '
        'Reports actual IDs and excess, marks incomplete low counts unknown. Never closes, answers, grants, enforces waiting or changes execution policy.'))
