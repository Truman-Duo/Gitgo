import React,{useEffect,useRef} from "react";
import {Box,ScrollBox,Text,type ScrollBoxHandle} from "@anthropic/ink";
import type {ProcessInfo} from "../hooks/useLoopData.js";
import {PageFrame,useCopy} from "../ui/PageFrame.js";
import {pages,standardKey,type Copy} from "../ui/pageContract.js";
import {label} from "../ui/runtimeLabels.js";
import {agentLabel} from "../daemon/agentLabels.js";
import {useManagedInput as useInput,useScrollInput} from "../input/runtime.js";
type Props={project:string;processId:string;processes:Record<string,ProcessInfo>;onDismiss:()=>void};
export function ProcessDetailPanel({project,processId,processes,onDismiss}:Props){
 const t=useCopy(),scroll=useRef<ScrollBoxHandle>(null),process=processes[processId];
 useScrollInput(scroll);
 useEffect(()=>{scroll.current?.scrollTo(0);},[project,processId]);
 useInput((input,key)=>{
  const action=standardKey(input,key);
  if(action==="back")onDismiss();
  else if(action==="up"||action==="down")scroll.current?.scrollBy(action==="up"?-1:1);
 });
 return <PageFrame page={pages.processDetail}>
  <Box height={1} minHeight={1} flexShrink={0}><Text bold wrap="truncate-end">{t(pages.processDetail.title)} · {project}</Text></Box>
  <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} minHeight={0}>
   {!process?<Text>{processId?t({en:"This subprocess is no longer available.",zh:"此子进程已不可用。"}):t({en:"Select a subprocess in /processlist first.",zh:"请先在 /processlist 选中子进程。"})}</Text>
    :processDetailSections(process,processes,t).map(section=><Box key={section.title} flexDirection="column" marginTop={1} flexShrink={0}>
     <Text bold>{section.title}</Text>
     {section.rows.map(row=><Box key={row.label} flexDirection="column" marginTop={1} flexShrink={0}>
      <Text dimColor>{row.label}</Text><Text>{row.value}</Text>
     </Box>)}
    </Box>)}
  </ScrollBox>
 </PageFrame>;
}
type Section={title:string;rows:Array<{label:string;value:string}>};
/** The panel reads the same process map as the list/chat; opening it never mutates or fetches a second authority. */
export function processDetailSections(process:ProcessInfo,processes:Record<string,ProcessInfo>,t:(value:Copy)=>string):Section[]{
 const missing=t({en:"Not recorded",zh:"未记录"}),none=t({en:"None",zh:"无"});
 const value=(v:unknown)=>v===undefined||v===null||v===""?missing:String(v);
 const references=(ids:string[])=>ids.length?ids.map(id=>{
  const p=processes[id];return p?agentLabel(p)+" · "+id:id;
 }).join("\n"):none;
 const parents=[...new Set(process.parent_ids?.length?process.parent_ids:process.parent_id?[process.parent_id]:[])];
 const children=[...new Set([...(process.child_ids||[]),...Object.values(processes).filter(p=>p.parent_id===process.process_id).map(p=>p.process_id)])];
 const budget=process.task_budget||{};
 const limits=budget.limits as Record<string,unknown>|undefined;
 const deadline=budget.deadline as Record<string,unknown>|undefined;
 const sections:Section[]=[
  {title:t({en:"Identity",zh:"身份"}),rows:[
   {label:t({en:"Name",zh:"名称"}),value:agentLabel(process)},
   {label:t({en:"Status",zh:"状态"}),value:label(process.status,t)},
   {label:t({en:"Role",zh:"角色"}),value:label(process.actor_kind||process.role,t)},
   {label:t({en:"Model",zh:"模型"}),value:value(process.model_id)},
   {label:t({en:"Process ID",zh:"进程 ID"}),value:process.process_id},
   {label:t({en:"Session ID",zh:"会话 ID"}),value:value(process.session_id)},
   {label:t({en:"Created",zh:"创建时间"}),value:value(process.created_at)},
  ]},
  {title:t({en:"Execution budget",zh:"执行预算"}),rows:[
   {label:t({en:"Steps used / current execution limit",zh:"已用步数 / 当前执行上限"}),value:value(process.steps_used)+" / "+value(process.max_steps)},
   {label:t({en:"Task time limit (seconds)",zh:"任务时限（秒）"}),value:value(deadline?.allowance_seconds)},
   ...(limits?[
    {label:t({en:"Provider request limit",zh:"模型请求上限"}),value:value(limits.max_provider_calls)},
    {label:t({en:"Output token limit",zh:"输出词元上限"}),value:value(limits.max_output_tokens)},
   ]:[]),
   {label:t({en:"Capability profile",zh:"能力配置"}),value:value(process.capability_profile_id)},
   {label:t({en:"Permission ring",zh:"权限级别"}),value:value(process.ring_level)},
  ]},
  {title:t({en:"Workspace",zh:"工作目录"}),rows:[
   {label:t({en:"Location",zh:"位置"}),value:value(process.worktree?.path||process.worktree_path)},
   {label:t({en:"Isolation",zh:"隔离状态"}),value:process.worktree?process.worktree.isolated?t({en:"Isolated worktree",zh:"独立工作树"}):t({en:"Shared workspace",zh:"共享工作区"}):missing},
   ...(process.worktree?.dirty===undefined||process.worktree.dirty===null?[]:[{label:t({en:"Uncommitted changes",zh:"未提交变更"}),value:process.worktree.dirty?t({en:"Yes",zh:"有"}):t({en:"No",zh:"无"})}]),
   ...(process.worktree?.base_commit?[{label:t({en:"Base commit",zh:"基准提交"}),value:process.worktree.base_commit}]:[]),
   ...(process.worktree?.output_commit?[{label:t({en:"Output commit",zh:"产出提交"}),value:process.worktree.output_commit}]:[]),
  ]},
  {title:t({en:"Relationships",zh:"进程关系"}),rows:[
   {label:t({en:"Parent processes",zh:"上级进程"}),value:references(parents)},
   {label:t({en:"Prerequisite subprocesses",zh:"前置依赖"}),value:references(process.depends_on||[])},
   {label:t({en:"Dependent subprocesses",zh:"后续依赖"}),value:references(process.downstream_ids||[])},
   {label:t({en:"Owned subprocesses",zh:"所属子进程"}),value:references(children)},
  ]},
 ];
 if(process.recovery)sections.push({title:t({en:"Recovery",zh:"恢复"}),rows:[
  {label:t({en:"Admission",zh:"恢复条件"}),value:process.recovery.resume_forbidden?t({en:"Resume forbidden",zh:"禁止恢复"}):process.recovery.requires_manual_verification?t({en:"Manual verification required",zh:"需要人工核验"}):t({en:"Explicit confirmation required",zh:"需要明确确认"})},
  {label:t({en:"Recovery command",zh:"恢复命令"}),value:"/runtime recovery "+(process.recovery.resume_forbidden?"discard":process.recovery.requires_manual_verification?"resume_verified":"resume")+" "+process.process_id},
 ]});
 return sections;
}
