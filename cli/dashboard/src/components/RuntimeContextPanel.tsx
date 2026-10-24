import {label} from "../ui/runtimeLabels.js";
import {useCopy} from "../ui/PageFrame.js";
import React, {memo, useMemo, useRef, useState, useEffect} from "react";
import {Box, ScrollBox, Text} from "@anthropic/ink";
import type {ScrollBoxHandle} from "@anthropic/ink";
import type {ProcessInfo} from "../hooks/useLoopData.js";
import {chordLabel, matchChord} from "../input/bindings.js";
import {useManagedInput as useInput, useScrollInput} from "../input/runtime.js";
import {colors, truncate, padEndWidth, displayWidth, usePanelSize} from "../theme/index.js";

type Props = {
  project: string;
  processes: Record<string, ProcessInfo>;
  onDismiss: () => void;
  interactive?: boolean;
};

import {InlineChoiceStrip} from "./InlineChoiceStrip.js";

export const RuntimeContextPanel = memo(function RuntimeContextPanel({
  project, processes, onDismiss, interactive = true,
}: Props) {
  const t=useCopy();
  const [selectedId, setSelectedId] = useState("");
  const {w} = usePanelSize({minWidth: 20, widthOffset: 6});
  const scrollRef = useRef<ScrollBoxHandle>(null);
  useScrollInput(scrollRef, interactive);
  const rows=useMemo(()=>contextSessions(Object.values(processes)),[processes]);
  const index = Math.max(0, rows.findIndex(row => (row.session_id||row.process_id) === selectedId));
  const selected = rows[index];
  const count=t({en:"Process",zh:"进程"})+" "+(selected?index+1:0)+"/"+rows.length;
  useEffect(() => { scrollRef.current?.scrollTo(0); }, [selected?.session_id,selected?.process_id]);

  useInput((input, key) => {
    if (matchChord("escape", input, key)) {
      onDismiss();
      return;
    }
    const direction = matchChord("left", input, key) ? -1
      : matchChord("right", input, key) ? 1 : 0;
    if (matchChord("up",input,key)) scrollRef.current?.scrollBy(-1);
    else if(matchChord("down",input,key)) scrollRef.current?.scrollBy(1);
    if (direction) setSelectedId(previous => {
      const current = Math.max(0, rows.findIndex(row => (row.session_id||row.process_id) === previous));
      const row=rows[Math.max(0, Math.min(rows.length - 1, current + direction))];
      return row?.session_id||row?.process_id||"";
    });
  }, {isActive: interactive});

  const sideBySide = w >= 88;
  const legendLabelWidth = Math.max(8, Math.min(32, w - (sideBySide ? 25 : 0) - 23));
  const legendName = (name: string) => padEndWidth(truncate(name, legendLabelWidth), legendLabelWidth);
  const context = selected?.context;
  const used = Number(context?.used_tokens ?? context?.estimated_tokens ?? selected?.estimated_tokens ?? 0);
  const limit = Number(context?.limit || 0);
  const percent = limit ? Math.min(999, used / limit * 100) : 0;
  const sections = context?.breakdown?.sections || [];
  const reserve = Number(context?.auto_compact_tokens ?? (context?.auto_compact_enabled === false ? 0 : limit * 0.1));
  const free = Number(context?.free_tokens ?? Math.max(0, limit - used - reserve));
  const cache = Number(
    selected?.cache_summary?.eligible_hit_ratio
      ?? selected?.cache_summary?.raw_hit_ratio
      ?? 0,
  );

  const gridSegments = (() => {
    if (!limit) return [] as Array<{name: string; cells: number; color: string}>;
    const raw: Array<{name: string; cells: number; color: any}> = sections.map((section, sectionIndex) => ({
      name: section.name,
      cells: Math.max(0, Math.round(Number(section.estimated_tokens || 0) / limit * 100)),
      color: colors.contextMap.categories[sectionIndex % colors.contextMap.categories.length]!,
    }));
    raw.push({name: "Free space", cells: Math.max(0, Math.round(free / limit * 100)), color: colors.contextMap.free});
    if (reserve > 0) raw.push({name: "Autocompact buffer", cells: Math.max(0, Math.round(reserve / limit * 100)), color: colors.contextMap.compact});
    let total = raw.reduce((sum, item) => sum + item.cells, 0);
    if (total < 100) raw.find(item => item.name === "Free space")!.cells += 100 - total;
    if (total > 100) {
      let overflow = total - 100;
      for (const item of [...raw].reverse()) {
        const take = Math.min(overflow, item.cells);
        item.cells -= take;
        overflow -= take;
        if (!overflow) break;
      }
    }
    return raw;
  })();
  const gridCells: Array<{color: any; name: string}> = gridSegments.flatMap(segment =>
    Array.from({length: segment.cells}, () => ({color: segment.color, name: segment.name})),
  ).slice(0, 100);

  return <Box flexDirection="column" paddingLeft={1} paddingRight={1} flexGrow={1} minHeight={0}>
    <Box height={1} minHeight={1} flexShrink={0}><Text bold wrap="truncate-end">{t({en:"Context Usage —",zh:"上下文用量 —"})} {project}</Text></Box>
    {selected?<Box marginTop={1} height={1} minHeight={1} flexShrink={0} flexDirection="row" gap={1}>
      <Box width={displayWidth(count)} flexShrink={0}><Text dimColor>{count}</Text></Box>
      <InlineChoiceStrip items={rows.map(process=>({id:process.process_id,
        label:process.actor_kind==="supervisor"?t({en:"Main process",zh:"主进程"}):process.display_name||t({en:"Subprocess",zh:"子进程"})}))}
        selected={index} width={Math.max(1,w-displayWidth(count)-1)} interactive={interactive}/>
    </Box>:null}
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1} minHeight={0}>
    {!selected ? <Text dimColor>{t({en:"No durable process context yet.",zh:"尚无持久化的进程上下文。"})}</Text> :
      <Box flexDirection="column" marginTop={1}>
        <Text bold>{selected.model_id && selected.model_id !== "unknown" ? selected.model_id : t({en:"Model not recorded for this process",zh:"此进程未记录模型名称"})}</Text>
        <Text>{used.toLocaleString()} / {limit ? limit.toLocaleString() : t({en:"unknown",zh:"未知"})} {t({en:"tokens",zh:"词元"})} ({percent.toFixed(1)}%)</Text>
        <Text dimColor>{context?.measurement === "provider" ? t({en:"provider measured",zh:"模型接口实测"}) : context?.measurement === "provider+increment_estimate" ? t({en:"provider + new content estimate",zh:"接口实测与新增内容估算"}) : t({en:"estimated",zh:"估算"})}   {t({en:"epoch",zh:"轮期"})} {context?.epoch ?? 0}   {t({en:"cache",zh:"缓存"})} {Math.round(cache * 100)}%</Text>
        <Box flexDirection={sideBySide ? "row" : "column"} marginTop={1}>
        {limit > 0 ? <Box flexDirection="column" width={22} flexShrink={0} marginRight={sideBySide ? 3 : 0}>
          {Array.from({length: 10}, (_, row) => <Box key={row} flexDirection="row">
            {gridCells.slice(row * 10, row * 10 + 10).map((cell, column) =>
              <Text key={`${row}-${column}`} color={cell.color}>■ </Text>)}
          </Box>)}
        </Box> : null}
        <Box marginTop={sideBySide ? 0 : 1} flexDirection="column" flexGrow={1} minWidth={0}>
          {sections.length === 0
            ? <Text dimColor>{t({en:"Section measurements will appear after the next prompt compilation.",zh:"下次编译提示词后显示各部分用量。"})}</Text>
            : sections.map((section, sectionIndex) => <Text key={section.name}>
              <Text color={colors.contextMap.categories[sectionIndex % colors.contextMap.categories.length]}>■</Text>
              {" "}{legendName(label(section.name,t))} {section.estimated_tokens.toLocaleString().padStart(8)}   {limit ? (section.estimated_tokens / limit * 100).toFixed(1).padStart(5) : "  0.0"}%
            </Text>)}
          {limit > 0 ? <>
            <Text><Text color={colors.contextMap.free}>■</Text>{" "}{legendName(t({en:"Free space",zh:"剩余空间"}))} {Math.max(0, free).toLocaleString().padStart(8)}   {(Math.max(0, free) / limit * 100).toFixed(1).padStart(5)}%</Text>
            {reserve > 0 ? <Text><Text color={colors.contextMap.compact}>■</Text>{" "}{legendName(t({en:"Autocompact buffer",zh:"自动压缩预留"}))} {reserve.toLocaleString().padStart(8)}   {(reserve / limit * 100).toFixed(1).padStart(5)}%</Text> : null}
          </> : null}
        </Box>
        </Box>
      </Box>}
    </ScrollBox>
    <Box flexShrink={0}><Text dimColor>{t({en:"←/→ process · ↑/↓ scroll · Esc back",zh:"←/→ 进程 · ↑/↓ 滚动 · Esc 返回"})}</Text></Box>
  </Box>;
});


/** Context belongs to a durable conversation; old executions remain in /stats. */
export function contextSessions(processes:ProcessInfo[]):ProcessInfo[]{
 const latest=new Map<string,ProcessInfo>();
 for(const process of processes){
  const key=process.session_id||process.process_id,previous=latest.get(key);
  if(!previous||previous.created_at.localeCompare(process.created_at)<=0)latest.set(key,process);
 }
 return [...latest.values()].sort((a,b)=>
  Number(b.actor_kind==="supervisor")-Number(a.actor_kind==="supervisor")||a.created_at.localeCompare(b.created_at));
}
