// A tool owns one foldable body. Gray names are normal; only failures are red.
import React, {useCallback,useEffect,useMemo,useState} from "react";
import {Box,Text} from "@anthropic/ink";
import type {ToolCallCard} from "../types.js";
import type {TraceBodyReader} from "../chat/traceSource.js";
import {colors,formatDuration,truncate} from "../theme/index.js";
import {Disclosure} from "./Disclosure.js";
import {TraceContent} from "./TraceContent.js";
import {DiffView} from "./DiffView.js";
import {parseUnifiedDiff} from "../utils/diff.js";
import {useSharedClock} from "../hooks/useSharedClock.js";
import {useCopy} from "../ui/PageFrame.js";
import {timelineFromTrace} from "../chat/turnTimeline.js";
import {actorLabelCopy} from "../daemon/agentLabels.js";
import {materializeToolDetail,toolOutputWithoutPatch,toolArgumentSummary} from "../chat/toolResult.js";
import {geometry} from "../theme/tokens.js";
import {useDisclosureState} from "../hooks/useDisclosureState.js";
import {DetailViewport} from "./DetailViewport.js";
import {TraceSourceText} from "./TraceSourceText.js";
import {VirtualContent} from "./VirtualContent.js";
import {VerbatimText} from "./VerbatimText.js";

function ToolBody({tool,width}: {tool:ToolCallCard;width?:number}) {
  const t=useCopy();
  const files=useMemo(()=>tool.diff ? parseUnifiedDiff(tool.diff) : [],[tool.diff]);
  const output=useMemo(()=>toolOutputWithoutPatch(tool.result_text,files.length ? tool.diff : ""),[tool.result_text,tool.diff,files.length]);
  const reason=tool.blocked_reason && !output.includes(tool.blocked_reason) ? tool.blocked_reason : "";
  return <Box flexDirection="column" paddingLeft={geometry.record.indent}>
    {reason ? <Text dimColor>{reason}</Text> : null}
    {tool.source_text!==undefined ? <VerbatimText content={tool.source_text} width={Math.max(1,(width||80)-geometry.record.indent)}/>
      : output ? <VerbatimText content={output} width={Math.max(1,(width||80)-geometry.record.indent)}/> : null}
    {files.length ? <DiffView files={files} width={width ? Math.max(12,width-4) : undefined}/> : null}
  </Box>;
}
function ResolvedArguments({tool,onSummary}:{tool:ToolCallCard;onSummary:(summary:string)=>void}) {
 const summary=useMemo(()=>toolArgumentSummary(tool),[tool.arguments,tool.argument_preview,tool.target,tool.tool_name]);
 useEffect(()=>{if(summary)onSummary(summary);},[summary,onSummary]);
 return null;
}
function PagedBody({tool,read,width,onSummary}:{tool:ToolCallCard;read:TraceBodyReader;width:number;onSummary:(summary:string)=>void}) {
 const t=useCopy();
 return <TraceContent detailRef={tool.detail_ref!} readDetail={read.recordSources}>
  {(saved:any)=>{
   if(!saved)return null;
   if(saved.has_diff || !Array.isArray(saved.sources) || !saved.sources.length)
    return <RecordedBody tool={tool} read={read} width={width} onSummary={onSummary}/>;
   const sources=saved.sources as Array<{source_index:number;total_chars:number;kind:string}>;
   return <Box paddingLeft={geometry.record.indent} flexDirection="column">
    <ResolvedArguments tool={{...tool,argument_preview:saved.argument_preview}} onSummary={onSummary}/>
    <VirtualContent itemKeys={sources.map(source=>String(source.source_index))} minimumItems={1} layoutKey={width}
     estimateRows={(_key,index)=>Math.max(1,Math.ceil(sources[index]!.total_chars/Math.max(1,width-geometry.record.indent)))}>
     {index=><TraceSourceText reference={tool.detail_ref!} index={sources[index]!.source_index} read={read.recordSource!}
      width={Math.max(1,width-geometry.record.indent)} totalChars={sources[index]!.total_chars}/>}
    </VirtualContent>
    {saved.file_excerpt ? <Text dimColor>{t({en:"The saved read contains a file excerpt.",zh:"本次已记录的读取结果为文件片段。"})}</Text> : null}
   </Box>;
  }}
 </TraceContent>;
}
function RecordedBody({tool,read,width,onSummary}: {tool:ToolCallCard;read?:TraceBodyReader;width?:number;onSummary:(summary:string)=>void}) {
  return <TraceContent references={tool.trace_refs} read={read}>{events=>{
    const exact=events ? timelineFromTrace(events,tool.process_id).tools.find(item=>item.tool_call_id===tool.tool_call_id && item.process_id===tool.process_id) : undefined;
    const recorded=exact ? {...tool,...exact,
      ...(tool.state === "unresolved" ? {state:tool.state,blocked_reason:tool.blocked_reason,allowed:false,is_running:false} : {})} : tool;
    return <TraceContent detailRef={recorded.detail_ref || ""} readDetail={read?.detail}>
      {(detail:unknown)=>{
        const full=materializeToolDetail(recorded,detail);
        return <><ResolvedArguments tool={full} onSummary={onSummary}/><ToolBody tool={full} width={width}/></>;
      }}
    </TraceContent>;
  }}</TraceContent>;
}
export function ToolCallDisplay({tool,expanded:defaultExpanded=false,width,displayName,count=1,countUnit="calls",readBody,children}: {
  tool:ToolCallCard;expanded?:boolean;width?:number;displayName?:string;count?:number;
  countUnit?:"calls"|"requests"|"pages";
  readBody?:TraceBodyReader;children?:React.ReactNode;
}) {
  const t=useCopy();
  const [expanded,setExpanded]=useDisclosureState(defaultExpanded,'tool:'+JSON.stringify([tool.process_id,
    tool.tool_call_id||tool.detail_ref||[tool.tool_name,tool.timestamp,tool.target],Boolean(children)]));
  const identity=JSON.stringify([tool.process_id,tool.tool_call_id,tool.detail_ref,tool.trace_refs]);
  const [loadedSummary,setLoadedSummary]=useState<{identity:string;summary:string}>();
  const onSummary=useCallback((summary:string)=>setLoadedSummary(previous=>previous?.identity===identity && previous.summary===summary
    ? previous : {identity,summary}),[identity]);
  const preview=useMemo(()=>toolArgumentSummary(tool,Math.max(12,(width||80)-tool.tool_name.length-16)),
    [tool.arguments,tool.argument_preview,tool.target,tool.tool_name,width]);
  const argumentsText=loadedSummary?.identity===identity ? loadedSummary.summary : preview;
  const state=tool.state ?? (tool.is_running ? "running" : tool.allowed ? "completed" : "error");
  const running=state==="running" || state==="pending";
  const now=useSharedClock(100,running);
  const started=Date.parse(tool.timestamp) || now;
  const duration=running ? Math.max(0,now-started) : Math.max(0,tool.duration_ms || 0);
  const expandable=Boolean(children || tool.target || tool.arguments !== undefined || tool.result_text || tool.diff || tool.blocked_reason || tool.trace_refs?.length || tool.detail_ref);
  return <Disclosure expanded={expanded && expandable} expandable={expandable} running={state==="running"}
    onToggle={()=>setExpanded(current=>!current)} label={<>
      {tool.actor_label ? <Text color={colors.chat.reasoningFg}>{t(actorLabelCopy(tool.actor_label))} · </Text> : null}
      <Text color={state==="error" ? colors.danger : colors.chat.reasoningFg}>{displayName || tool.tool_name}{count>1
        ? countUnit==="pages" ? t({en:` ${count} web pages`,zh:` ${count} 个网页`})
        : countUnit==="requests" ? t({en:` · ${count} requests`,zh:` ${count} 次`})
        : t({en:` · ${count} calls`,zh:` ${count} 次调用`}) : ""}</Text>
      {argumentsText ? <Text color={colors.chat.reasoningFg}> {truncate(argumentsText,Math.max(12,(width||80)-tool.tool_name.length-16))}</Text> : null}
      {state==="unresolved" ? <Text dimColor>{t({en:" · Unconfirmed",zh:" · 状态未确认"})}</Text> : null}
      <Text dimColor> {formatDuration(duration)}</Text>
    </>}>
    {expanded && expandable ? children || <DetailViewport onClose={()=>setExpanded(false)} width={width}>
      {bodyWidth=>tool.detail_ref && readBody?.recordSources && readBody.recordSource
        ? <PagedBody tool={tool} read={readBody} width={bodyWidth} onSummary={onSummary}/>
        : <RecordedBody tool={tool} read={readBody} width={bodyWidth} onSummary={onSummary}/>}
    </DetailViewport> : null}
  </Disclosure>;
}
