import type {Copy} from "./pageContract.js";

const labels = {
 closing:{en:"Closing",zh:"收尾中"},closeout_failed:{en:"Save failed",zh:"收尾失败"},
 self_execute:{en:"Main process",zh:"主进程执行"},delegate:{en:"Delegate",zh:"委派"},
 continue_worker:{en:"Continue owner",zh:"原负责人续办"},undecided:{en:"Undecided",zh:"待决定"},

 worker:{en:"Worker",zh:"执行子进程"},reviewer:{en:"Reviewer",zh:"审查子进程"},supervisor:{en:"Main process",zh:"主进程"},
 waiting:{en:"Waiting",zh:"等待中"},recovering:{en:"Recovering",zh:"恢复中"},resume_available:{en:"Resume available",zh:"可恢复"},
 timed_out:{en:"Timed out",zh:"超时"},orphaned:{en:"Orphaned",zh:"失去关联"},killed:{en:"Stopped",zh:"已停止"},
 warning:{en:"Warning",zh:"警告"},degraded:{en:"Degraded",zh:"降级"},initializing:{en:"Initializing",zh:"初始化中"},
 shared_workspace:{en:"Shared workspace",zh:"共享工作区"},isolated:{en:"Isolated worktree",zh:"独立工作树"},
 answer:{en:"Answer",zh:"问答"}, development:{en:"Development",zh:"开发"},
 review:{en:"Review",zh:"审阅"}, research:{en:"Research",zh:"研究"}, Task:{en:"Task",zh:"任务"},
 completed:{en:"Completed",zh:"已完成"}, complete:{en:"Complete",zh:"已完成"},
 running:{en:"Running",zh:"运行中"}, pending:{en:"Pending",zh:"待处理"},
 proposed:{en:"Proposed",zh:"提案"},adopted:{en:"Adopted",zh:"已采用"},rejected:{en:"Rejected",zh:"已拒绝"},
 candidate:{en:"Candidate",zh:"候选"},pending_adoption:{en:"Pending adoption",zh:"采用待对账"},merged:{en:"Merged",zh:"已合并"},
 reviewing:{en:"Reviewing",zh:"审阅中"},rework:{en:"Return to execution",zh:"返回执行"},approved:{en:"Approved",zh:"审阅通过"},
 changes_required:{en:"Changes required",zh:"需要修改"},needs_review:{en:"Needs review",zh:"待修订"},inspect_required:{en:"Inspection required",zh:"需要核对"},
 withdrawn:{en:"Withdrawn",zh:"已撤回"},resolved:{en:"Resolved",zh:"已解决"},acknowledged:{en:"Acknowledged",zh:"已解释"},
 processed:{en:"Processed",zh:"已处理"},discarded:{en:"Discarded",zh:"已丢弃"},unknown:{en:"Unknown",zh:"结果不明"},
 saved:{en:"Saved",zh:"已保存"},pending_activation:{en:"Pending activation",zh:"待生效"},activating:{en:"Activating",zh:"生效中"},
 ready:{en:"Ready",zh:"就绪"},suspended:{en:"Suspended",zh:"已暂停"},retiring:{en:"Retiring",zh:"卸载中"},detached:{en:"Detached",zh:"已卸载"},
 awaiting_user:{en:"Awaiting user",zh:"等待用户"}, blocked:{en:"Blocked",zh:"受阻"},
 failed:{en:"Failed",zh:"失败"}, cancelled:{en:"Cancelled",zh:"已取消"},
 incomplete:{en:"Incomplete",zh:"未完成"}, archived:{en:"Archived",zh:"已归档"}, active:{en:"Active",zh:"可用"},
 "no completion record":{en:"No completion record",zh:"未记录结束"},
 "rolled back":{en:"Rolled back",zh:"已回滚"}, "blocked / failed":{en:"Blocked / failed",zh:"被拦截 / 失败"},
 current:{en:"Current",zh:"当前有效"}, stale:{en:"Stale",zh:"可能过时"},
 "Base identity":{en:"Base identity",zh:"基本身份"},
 "Behavior and delivery standard":{en:"Behavior and delivery standard",zh:"行为与交付标准"},
 "Role and authority":{en:"Role and authority",zh:"角色与权限"}, Capabilities:{en:"Capabilities",zh:"能力"},
 "Delegation economy":{en:"Delegation economy",zh:"委派预算"},
 "Source authority":{en:"Source authority",zh:"来源权威"},
 "Tool use and feedback":{en:"Tool use and feedback",zh:"工具使用与反馈"},
 "Implementation safety":{en:"Implementation safety",zh:"实施安全"},
 "User collaboration protocol":{en:"User collaboration protocol",zh:"用户协作协议"},
 "Agent relationship and communication protocol":{en:"Agent relationship and communication protocol",zh:"进程关系与通信协议"},
 "Runtime environment":{en:"Runtime environment",zh:"运行环境"},
 "Task contract":{en:"Task contract",zh:"任务契约"},
 "Project governance context":{en:"Project governance context",zh:"项目治理上下文"},
 "Prompt":{en:"Prompt",zh:"提示词"}, "Prompt framing":{en:"Prompt framing",zh:"提示词包装"},
 "Tool results":{en:"Tool results",zh:"工具结果"}, "Tool schemas":{en:"Tool schemas",zh:"工具定义"},
 "Context checkpoints":{en:"Context checkpoints",zh:"上下文检查点"},
 "Host steering":{en:"Host steering",zh:"宿主引导"},
 "User conversation":{en:"User conversation",zh:"用户对话"}, "Assistant conversation":{en:"Assistant conversation",zh:"助手对话"},
 "Other history":{en:"Other history",zh:"其他历史"},
 "Provider continuation":{en:"Provider continuation",zh:"模型接口续接"},
 "Runtime context updates":{en:"Runtime context updates",zh:"运行态上下文"},
 "Pending tool catalog":{en:"Pending tool catalog",zh:"待装配工具目录"},
 "Provider overhead":{en:"Provider overhead",zh:"模型接口额外用量"},
} satisfies Record<string,Copy>;
export function label(value:unknown,t:(value:Copy)=>string):string {
 const key=String(value??"");
 return Object.hasOwn(labels,key)?t(labels[key as keyof typeof labels]):key;
}

/** A completed model response remains provisional until Host closeout succeeds. */
export function processDisplayStatus(process: {status: string; closeout_state?: string}): string {
 if (process.closeout_state === "failed") return "closeout_failed";
 if (process.closeout_state === "pending") return "closing";
 return process.status;
}
