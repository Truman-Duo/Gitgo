import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import {geometry} from "../theme/tokens.js";
import {SelectionText} from "./Selection.js";
import React,{useEffect,useRef,useState} from "react";
import {Box,ScrollBox,Text,type ScrollBoxHandle} from "@anthropic/ink";
import {useManagedInput as useInput,useScrollInput} from "../input/runtime.js";
import type {BackendClient} from "../backend/client.js";
import {runtimeUsage} from "../backend/tools.js";
import {colors,truncate,padEndWidth,formatDuration} from "../theme/index.js";
import {PageHeader} from "../ui/PageHeader.js";
import {heatCalendar,localDay,tokenTotal,moveCalendarCell} from "./usagePresentation.js";
import {PageFrame,useCopy} from "../ui/PageFrame.js";
import {pages,standardKey} from "../ui/pageContract.js";

type Props={client:BackendClient;cols:number;onDismiss:()=>void;onInspect?:(project:string)=>void};
export function StatsOverview({client,cols,onDismiss,onInspect}:Props){
 const t=useCopy();
 const [data,setData]=useState<any>(null),[error,setError]=useState("");
 const [view,setView]=useState(0),[focus,setFocus]=useState<"header"|"content"|"actions">("header");
 const [selected,setSelected]=useState(0),[action,setAction]=useState(0);
 const [day,setDay]=useState(localDay()),[metric,setMetric]=useState<"tokens"|"requests">("tokens");
 const [refresh,setRefresh]=useState(0);
 const scroll=useRef<ScrollBoxHandle>(null);useScrollInput(scroll,focus==="content");

 useEffect(()=>{
  let alive=true;setError("");
  runtimeUsage(client).then(value=>{if(alive)setData(value);}).catch(e=>{if(alive)setError(String(e.message||e));});
  return()=>{alive=false;};
 },[client,refresh]);
 const tasks=data?.tasks||[],projects=data?.projects||[],list=view===1?tasks:projects;
 const weeks=Math.max(4,Math.min(53,Math.floor((cols-12)/2)));
 const calendar=heatCalendar(data?.daily||[],localDay(),weeks,metric);
 const firstDay=calendar[0]![0]!.date;
 useEffect(()=>{setDay(v=>v<firstDay?firstDay:v);},[firstDay]);
 useEffect(()=>{setSelected(value=>Math.max(0,Math.min(value,list.length-1)));},[list.length,view]);
 const menuHelp=useLocalHelpCommand("usage",undefined,true);
  useInput((input,key)=>{
    if(menuHelp.handle(input,key))return;
  const command=standardKey(input,key);
  if(command==="back"){if(focus==="header")onDismiss();else setFocus("header");return;}
  if(focus==="header"){
   if(command==="left"||command==="right"){
    setView(v=>Math.max(0,Math.min(2,v+(command==="left"?-1:1))));
    setSelected(0);scroll.current?.scrollTo(0);
   }else if(command==="activate")setFocus("content");
   return;
  }
  if(command==="next"||command==="previous"){const order=["header","content","actions"] as const;setFocus(f=>order[(order.indexOf(f)+(command==="previous"?2:1))%3]);return;}
  if(focus==="actions"){
   if(command==="left"||command==="right")setAction(v=>Math.max(0,Math.min(1,v+(command==="left"?-1:1))));
   else if(command==="up")setFocus("content");
   else if(command==="activate"){
    if(action===0)setMetric(m=>m==="tokens"?"requests":"tokens");else setRefresh(n=>n+1);
   }
   return;
  }
  if(view===0){
   if(command==="left"||command==="right"||command==="up"||command==="down")
    setDay(v=>moveCalendarCell(calendar,v,command));
  }else{
   if(command==="up"||command==="down"){
    const delta=command==="up"?-1:1;
    if(delta>0&&selected>=list.length-1){setFocus("actions");return;}
    setSelected(v=>Math.max(0,Math.min(list.length-1,v+delta)));scroll.current?.scrollBy(delta);
   }else if(command==="activate"&&list[selected]?.project&&!list[selected]?.error)onInspect?.(list[selected].project);
  }
 });
 const summary=data?.summary||{},selectedDay=(data?.daily||[]).find((v:any)=>v.date===day);
 const metricLabel=metric==="tokens"?t({en:"Input + output tokens",zh:"输入与输出词元"}):t({en:"Provider requests",zh:"模型请求次数"});
 const labels=[t({en:"Daily activity",zh:"每日用量"}),t({en:"Recent tasks",zh:"最近任务"}),t({en:"Projects",zh:"项目汇总"})];
 const amount=(v:any)=>t({en:`${tokenTotal(v).toLocaleString()} tokens · ${Number(v?.provider_calls||0)} requests · ${Number(v?.task_count||0)} tasks`,
  zh:`${tokenTotal(v).toLocaleString()} 词元 · ${Number(v?.provider_calls||0)} 次请求 · ${Number(v?.task_count||0)} 项任务`});
 return <PageFrame page={pages.usage}>
  <Text bold>{t(pages.usage.title)}</Text>
  <PageHeader items={labels.map((label,id)=>({id:String(id),label}))} selected={view} width={cols-4} contentFocused={focus!=="header"}/>
  {error?<Text color={colors.danger}>{error}</Text>:!data?<Text dimColor>{t({en:"Loading recorded usage…",zh:"正在读取用量记录…"})}</Text>:<>
   <Text>{amount(summary)} · {t({en:"cache reads",zh:"缓存读取"})} {Number(summary.cache_read_tokens||0).toLocaleString()}</Text>
   {data.complete===false?<Text color={colors.warning}>{t({en:"Partial totals; some projects are unavailable.",zh:"部分项目不可用，汇总尚不完整。"})}</Text>:null}
  </>}
  <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} minHeight={0} marginTop={1}>
   {!data?<Text dimColor>{t({en:"Usage will appear after the query completes.",zh:"查询完成后显示用量。"})}</Text>:view===0?<>
    <Text bold>{metricLabel} · {t({en:`last ${weeks} weeks`,zh:`最近 ${weeks} 周`})}</Text>
    <Text dimColor>{calendar[0]![0]!.date} — {localDay()} · UTC{Number(data.day_offset_minutes||0)>=0?"+":""}{Number(data.day_offset_minutes||0)/60}</Text>
    {Array.from({length:7},(_,weekday)=><Text key={weekday}>
     <Text dimColor>{t({en:["Mo","Tu","We","Th","Fr","Sa","Su"][weekday]!,zh:["一","二","三","四","五","六","日"][weekday]!})} </Text>
     {calendar.map((week,index)=>{const cell=week[weekday]!,active=cell.date===day&&focus==="content";return <React.Fragment key={index}><SelectionText selected={active} variant="block"
      color={colors.usage.heat[cell.level]} >{cell.future?" ":"■"}</SelectionText><Text> </Text></React.Fragment>;})}
    </Text>)}
    <Text dimColor>{t({en:"Less",zh:"少"})} {colors.usage.heat.map((color,i)=><Text color={color} key={i}>■ </Text>)} {t({en:"More",zh:"多"})}</Text>
    <Text>{day} · {amount(selectedDay)}</Text>

    <Text dimColor>{t({en:"Cost unrecorded",zh:"费用未记录"})}</Text>
    {data.undated_tasks?<Text dimColor>{t({en:`${data.undated_tasks} tasks have no valid date.`,zh:`${data.undated_tasks} 项任务没有有效日期。`})}</Text>:null}
   </>:view===1?<>

    {tasks.map((task:any,index:number)=><SelectionText selected={index===selected} focused={focus==="content"} key={task.project+task.task_id}  >
     {padEndWidth(truncate(task.project,18),18)} {String(task.created_at||"").slice(0,10)} {tokenTotal(task).toLocaleString()} {t({en:"tokens",zh:"词元"})} · {formatDuration(Number(task.duration_ms||0))}
    </SelectionText>)}
   </>:projects.map((project:any,index:number)=><SelectionText selected={index===selected} focused={focus==="content"} key={project.project}  >
    {padEndWidth(truncate(project.project,22),22)} {project.error?t({en:`Unavailable: ${project.error}`,zh:`不可用：${project.error}`}):project.alias_of?t({en:`Shared with ${project.alias_of}; counted once`,zh:`与 ${project.alias_of} 共用记录，仅计一次`}):amount(project)}
   </SelectionText>)}
  </ScrollBox>
  <Box gap={geometry.actions.gap}>{[t({en:`Metric: ${metricLabel}`,zh:`统计口径：${metricLabel}`}),t({en:"Refresh",zh:"刷新"})].map((label,index)=><SelectionText selected={focus==="actions"&&action===index} variant="block" key={index}

   >{label}</SelectionText>)}</Box>

 </PageFrame>;
}
