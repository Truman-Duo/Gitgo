import {SelectionText} from "./Selection.js";
import React from "react";
import { Box, Text } from "@anthropic/ink";
import type { ProcessInfo } from "../hooks/useLoopData.js";
import { colors, displayWidth, truncate } from "../theme/index.js";
import { ContextStatus } from "./ContextStatus.js";
import { agentLabel } from "../daemon/agentLabels.js";

const workerLabel = (process: ProcessInfo, zh: boolean) => agentLabel(process) + " · " +
  (process.closeout_state === "pending" ? (zh ? "收尾中" : "closing") : process.closeout_state === "failed" ? (zh ? "收尾失败" : "save failed") : process.status === "running" ? (zh ? "工作中" : "working") : process.status === "awaiting_user" ? (zh ? "待答复" : "awaiting input") : (zh ? "等待" : "waiting"));

type Props = {
  language?: "en" | "zh";
  runningB: ProcessInfo[]; selIdx: number; focused: boolean;
  contextPct?: string; cachePct?: number | null; width: number; statusText?:string;
  advancement?: "auto" | "manual" | null;
  advancementPending?: boolean;
};

function fitAgents(runningB: ProcessInfo[], selected: number, width: number, zh: boolean) {
  if (width <= 0 || runningB.length === 0) {
    return {items: [] as ProcessInfo[], left: false, right: false, start: 0};
  }
  let start = Math.max(0, Math.min(selected, runningB.length - 1));
  let end = start + 1;
  const renderedWidth = (from: number, to: number) => {
    const labels = runningB.slice(from, to).reduce(
      (sum, item) => sum + displayWidth(workerLabel(item, zh)), 0,
    );
    const separators = Math.max(0, to - from - 1) * 3;
    return labels + separators + (from > 0 ? 4 : 0) + (to < runningB.length ? 4 : 0);
  };
  while (start > 0 || end < runningB.length) {
    const candidates = [
      ...(end < runningB.length ? [{ start, end: end + 1 }] : []),
      ...(start > 0 ? [{ start: start - 1, end }] : []),
    ].filter((item) => renderedWidth(item.start, item.end) <= width);
    if (candidates.length === 0) break;
    const next = candidates.sort(
      (a, b) => renderedWidth(a.start, a.end) - renderedWidth(b.start, b.end),
    )[0]!;
    start = next.start;
    end = next.end;
  }
  return {items: runningB.slice(start, end), left: start > 0, right: end < runningB.length, start};
}

export function RunningBStrip({ runningB, selIdx, focused, contextPct, cachePct, width, language = "en",statusText="",
  advancement, advancementPending = false }: Props) {
  const showMode = advancement !== undefined;
  const statusLabel = showMode ? (advancement === "auto" ? "AUTO" : advancement === "manual" ? "MANUAL" : "…")
    + (advancementPending ? " …" : "") : statusText;
  const statusColor = showMode ? colors.input.advancement[advancement ?? "unavailable"] : undefined;
  const leftWidth=Math.min(width,displayWidth("● "+(contextPct||"0%")));
  // Reserve the same slot for AUTO/MANUAL and pending acknowledgement, so
  // worker selection never jumps left/right when only the mode changes.
  const statusWidth=showMode?Math.min(8,Math.max(0,width-leftWidth-3)):
    statusText?Math.min(displayWidth(statusText),Math.max(0,Math.floor((width-leftWidth)/3))):0;
  const middleWidth=Math.max(0,width-leftWidth-(statusWidth?statusWidth+3:0)-3);
  const selected=selIdx%Math.max(1,runningB.length);
  const fitted=fitAgents(runningB,selected,middleWidth,language==="zh");
  const markers=Number(fitted.left)*4+Number(fitted.right)*4;
  const labelWidth=Math.max(1,middleWidth-markers-(fitted.items.length-1)*3);
  const boundedLabel=(p:ProcessInfo)=>{
    const state=language==="zh"?(p.status==="running"?"工作中":p.status==="awaiting_user"?"待答复":"等待")
      :(p.status==="running"?"working":p.status==="awaiting_user"?"awaiting input":"waiting");
    const name=agentLabel(p);
    const nameWidth=Math.min(displayWidth(name),Math.max(1,Math.floor(labelWidth/2),labelWidth-displayWidth(state)-3));
    const label=truncate(name,nameWidth);
    const stateWidth=labelWidth-displayWidth(label)-3;
    return stateWidth>0?label+" · "+truncate(state,stateWidth):label;
  };
  return <Box flexDirection="row" flexWrap="nowrap" width={Math.max(1,width)} overflow="hidden">
    <Box width={leftWidth} flexShrink={0}><ContextStatus contextPct={contextPct||"0%"} cachePct={cachePct}/></Box>
    {statusWidth?<><Text dimColor> · </Text><Box width={statusWidth} flexShrink={0}><Text dimColor={!showMode} color={statusColor} wrap="truncate-end">{statusLabel}</Text></Box></>:null}
    {fitted.items.length?<Text dimColor> · </Text>:null}
    <Box width={middleWidth} flexDirection="row" overflow="hidden">
      {fitted.left?<Text dimColor>... </Text>:null}
      {fitted.items.map((p,index)=>{
        const active=focused&&fitted.start+index===selected;
        return <React.Fragment key={p.process_id}>{index>0?<Text dimColor> · </Text>:null}
          <SelectionText selected={active}
             >{boundedLabel(p)}</SelectionText>
        </React.Fragment>;
      })}
      {fitted.right?<Text dimColor> ...</Text>:null}
    </Box>
  </Box>;
}
