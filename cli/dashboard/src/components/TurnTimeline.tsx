import React, {useEffect, useMemo, useState} from "react";
import { Box, Text } from "@anthropic/ink";
import type { ReactNode } from "react";
import type { LiveStageDetail, ProviderUsageSummary, RuntimeNotice, StreamActivity, ToolCallCard } from "../types.js";
import { colors, formatDuration } from "../theme/index.js";
import { MarkdownText, StreamingMarkdownText } from "./MarkdownText.js";
import { RuntimeNoticeRow } from "./RuntimeNoticeList.js";
import { Spinner } from "./Spinner.js";
import { ToolCallDisplay } from "./ToolCallDisplay.js";
import {actorLabelCopy} from "../daemon/agentLabels.js";
import {useCopy} from "../ui/PageFrame.js";
import type {Copy} from "../ui/pageContract.js";
import {timelineRows} from "../chat/timelineRows.js";
import {Disclosure} from "./Disclosure.js";
import {TraceContent} from "./TraceContent.js";
import type {TraceBodyReader, TraceBodyRef} from "../chat/traceSource.js";
import type {TraceRecord} from "../traceView.js";
import {VirtualContent} from "./VirtualContent.js";
import {geometry} from "../theme/tokens.js";
import {RequestContextRow} from "./RequestContextRow.js";
import {useDisclosureState} from "../hooks/useDisclosureState.js";
import {StageReport} from './StageReport.js';
import {DisclosureScope,useDisclosureScope} from '../hooks/disclosureScope.js';

type Props = {
  activity: StreamActivity[];
  tools: ToolCallCard[];
  notices: RuntimeNotice[];
  verbose: boolean;
  width: number;
  active?: boolean;
  thinking?: boolean;
  thinkingText?: string;
  elapsedMs?: number;
  providerUsage?: ProviderUsageSummary;
  readBody?: TraceBodyReader;
  stage?:LiveStageDetail;
};

function webFamily(tool: ToolCallCard | undefined, t: (copy: Copy) => string): {key: string; label: string} | null {
  const name = String(tool?.tool_name || "").toLowerCase();
  if (name === "web_search" || name === "hosted_web_search") {
    return {key: "search", label: t({en:"Search",zh:"搜索"})};
  }
  if (name === "web_fetch") return {key: "fetch", label: t({en:"Read",zh:"读取"})};
  return null;
}

function aggregateTool(first: ToolCallCard, rest: ToolCallCard[], t: (copy: Copy) => string): ToolCallCard {
  const all = [first, ...rest];
  const running = all.some(tool => tool.state === "running" || tool.state === "pending" || tool.is_running);
  const succeeded = all.some(tool => tool.state === "completed" && tool.allowed !== false);
  const failed = all.every(tool => tool.state === "error" || tool.allowed === false);
  return {
    ...first,
    target: "",
    arguments: undefined,
    argument_preview: undefined,
    result_text: undefined,
    compact_summary: undefined,
    blocked_reason: failed ? t({en:"All parallel requests failed",zh:"所有并行请求均失败"}) : undefined,
    duration_ms: Math.max(...all.map(tool => Number(tool.duration_ms || 0))),
    is_running: running,
    state: running ? "running" : failed ? "error" : succeeded ? "completed" : first.state,
    allowed: !failed,
  };
}

function recordedText(events: TraceRecord[]): string {
  const summary = events.findLast(event => event.event === "progress_summary");
  return summary ? String(summary.message || "") : events.map(event => String(event.delta || "")).join("");
}
function RecordText({text, references, read, width, dimColor = false, streaming = false}: {
  text: string; references?: TraceBodyRef[]; read?: TraceBodyReader; width: number; dimColor?: boolean;
  streaming?: boolean;
}) {
  const Markdown = streaming ? StreamingMarkdownText : MarkdownText;
  return <TraceContent references={references} read={read}>
    {events => <Markdown content={events ? recordedText(events) : read && references?.length ? "" : text}
      width={width} dimColor={dimColor}/>}
  </TraceContent>;
}

function TextRecord({text, label, verbose, width, active = false, elapsedMs = 0, usage = "", actor, references, read,identity}: {
  text: string; label: string; verbose: boolean; width: number; active?: boolean; elapsedMs?: number; usage?: string; actor?: string;
  references?: TraceBodyRef[]; read?: TraceBodyReader;
  identity?:string;
}) {
  const [expanded,setExpanded] = useDisclosureState(verbose,'text:'+identity);
  // Turning verbose off removes private text in this commit, even while the
  // disclosure's closing height animation is still settling.
  const showText = verbose;
  return <Disclosure expanded={expanded} onToggle={() => setExpanded(current => !current)} label={<>
    {active ? <Spinner frames={colors.spinner.thinkingFrames} intervalMs={colors.spinner.thinkingIntervalMs} color={colors.warning}/> : null}
    {actor ? actor + " · " : ""}{label}{active ? "… " + formatDuration(elapsedMs) : ""}{usage}
  </>}>
    {showText && expanded ? <Box paddingLeft={geometry.record.indent} flexDirection="column">
      {text || references?.length ? <RecordText text={text} references={references} read={read} width={Math.max(1, width - geometry.record.indent)} dimColor streaming={active}/> : null}
    </Box> : null}
  </Disclosure>;
}

function RecordedNotice({notice, read, verbose}: {notice: RuntimeNotice; read?: TraceBodyReader; verbose: boolean}) {
  return <RuntimeNoticeRow notice={notice} verbose={verbose} readBody={read}/>;
}

/** Shared ordered renderer for main/subprocess live and completed trace capsules. */
export function TurnTimeline(props:Props){return <DisclosureScope scope='timeline'><Timeline {...props}/></DisclosureScope>;}
function Timeline({
  activity, tools, notices, verbose, width, active = false, elapsedMs = 0,
  thinking, thinkingText, providerUsage, readBody,stage,
}: Props) {
  const t=useCopy();
  const scope=useDisclosureScope();
  useEffect(()=>{
    // Turning private inspection off retires its open readers; unlike width
    // reflow/virtual eviction, turning it on requires a fresh explicit open.
    if(!verbose&&scope)scope.store.clearPrefix(scope.scope+'/context:');
  },[verbose,scope]);
  const stageId=stage?.id||'current';
  const [stageExpanded,changeStage]=useDisclosureState(true,'stage:'+stageId);
  const rows = useMemo(() => timelineRows(activity, tools).filter(({activity: item}) =>
    item.kind === "reasoning" ? false : item.kind === "context" || item.kind === "draft" ? verbose
      : item.kind === "progress" ? item.visibility === "public" || verbose : true),
    [activity, tools, verbose]);
  const keys = useMemo(() => rows.map(row => row.id), [rows]);
  const lastActivity = activity.at(-1);
  const thinkingActive = active && (thinking ?? lastActivity?.kind === "reasoning");
  const currentThought = thinkingText ?? (lastActivity?.kind === "reasoning" ? lastActivity.text : "");
  const bodyWidth = Math.max(1,width-geometry.record.indent);
  const usage = providerUsage
    ? t({en:` · ${providerUsage.input_tokens} in / ${providerUsage.output_tokens} out`,zh:` · 输入 ${providerUsage.input_tokens} / 输出 ${providerUsage.output_tokens}`})
    : "";
  const stageReport=active&&stage?rows.findLast(row=>row.activity.kind==='progress'
    &&row.activity.visibility==='public'&&row.activity.stage_id===stage.id
    &&row.activity.process_id===stage.process_id):undefined;

  return <Box flexDirection="column" paddingLeft={geometry.record.indent}>
    <VirtualContent itemKeys={keys} layoutKey={width} estimateRows={(_key, index) => {
      const item = rows[index]!.activity;
      return item.kind === "draft" || item.kind === "progress"
        ? 1 + Math.ceil((item.trace_refs?.reduce((n, ref) => n + ref.text_chars, 0) || item.text.length) / Math.max(1,bodyWidth))
        : 4;
    }}>{index => {
      const {id, activity: item, toolIndices} = rows[index]!;
      let row: ReactNode = null;
      if(item.kind==="context"){
        row=<RequestContextRow request={item.request} readBody={readBody} width={bodyWidth}
          actor={item.actor_label ? t(actorLabelCopy(item.actor_label)) : undefined}/>;
      }else if (item.kind === "draft") {
        if (verbose) row = <TextRecord key={id} identity={id} text={item.text} label={t({en:"Response draft",zh:"回答草稿"})} verbose={verbose} width={bodyWidth}
          actor={item.actor_label ? t(actorLabelCopy(item.actor_label)) : undefined}
          references={item.trace_refs} read={readBody}/>;
      } else if (item.kind === "progress") {
        if (item.visibility === "public" || verbose) {
          row = <StageReport identity={stage?.id||id} verbose={verbose} width={bodyWidth}
            expanded={stageExpanded} onExpandedChange={changeStage}
            detail={stageReport?.id===id?stage?.reasoning:undefined}>
            {item.actor_label ? <Text dimColor>{t(actorLabelCopy(item.actor_label))}</Text> : null}
            <RecordText text={item.text} width={Math.max(1,bodyWidth-(verbose&&stageReport?.id===id&&stage?.reasoning?geometry.record.indent:0))} dimColor
              references={item.trace_refs} read={readBody}/>
            {item.progress_status === "interrupted" ? <Text dimColor>{t({en:"Interrupted",zh:"已中断"})}</Text> : null}
          </StageReport>;
        }
      } else if (item.kind === "tool") {
        const tool = tools[item.toolIndex];
        const family = webFamily(tool, t);
        if (tool && family && (toolIndices?.length ?? 0) > 1) {
          const siblings = toolIndices!.slice(1).map(index => tools[index]!);
          const sameFamily = siblings.every(item => webFamily(item,t)?.key === family.key);
          const displayName = sameFamily ? family.label : t({en:"Web research",zh:"网页检索"});
          row = <ToolCallDisplay tool={aggregateTool(tool, siblings, t)} displayName={displayName}
            count={siblings.length + 1} countUnit={sameFamily ? family.key === "fetch" ? "pages" : "requests" : "calls"}
            expanded={false} width={bodyWidth}>
            <Box flexDirection="column" paddingLeft={geometry.record.indent}>{[tool,...siblings].map((item,index)=><ToolCallDisplay
              key={`${item.process_id}:${item.tool_call_id || index}`} tool={item} readBody={readBody} width={Math.max(1,bodyWidth-geometry.record.indent)}/>)}</Box>
          </ToolCallDisplay>;
        } else {
          const display = tool ? <ToolCallDisplay tool={tool} readBody={readBody} width={bodyWidth}/> : null;
          row = tool?.composite_tool ? <Box flexDirection="column" marginLeft={1} paddingLeft={1}
            borderStyle="single" borderLeft borderRight={false} borderTop={false} borderBottom={false} borderColor={colors.chat.turnGuide}>
            <Text dimColor>{tool.composite_tool} / {tool.step_id || t({en:"step",zh:"步骤"})}</Text>{display}
          </Box> : display;
        }
      } else if (item.kind === "notice") {
        const notice = notices[item.noticeIndex];
        row = notice ? <RecordedNotice notice={notice} read={readBody} verbose={verbose}/> : null;
      }
      if (!row) return null;
      return <Box key={id} flexDirection="column">
        {row}
      </Box>;
    }}</VirtualContent>
    {thinkingActive && !stageReport ? <StageReport identity={stage?.id||'current'} verbose={verbose} width={bodyWidth}
      expanded={stageExpanded} onExpandedChange={changeStage}
      detail={stage?.reasoning||currentThought}>
      <Box><Spinner frames={colors.spinner.thinkingFrames} intervalMs={colors.spinner.thinkingIntervalMs} color={colors.warning}/>
        <Text dimColor>{t({en:" Thinking…",zh:" 思考中…"})}{verbose ? " "+formatDuration(elapsedMs)+usage : ""}</Text>
      </Box></StageReport> : null}
  </Box>;
}
