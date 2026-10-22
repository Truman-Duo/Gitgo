import React, {memo, useMemo, useRef, useState, useEffect} from "react";
import {Box, ScrollBox, Text} from "@anthropic/ink";
import type {ScrollBoxHandle} from "@anthropic/ink";
import type {ProcessInfo} from "../hooks/useLoopData.js";
import {chordLabel, matchChord} from "../input/bindings.js";
import {useManagedInput as useInput, useScrollInput} from "../input/runtime.js";
import {colors, truncate, padEndWidth, usePanelSize} from "../theme/index.js";

type Props = {
  project: string;
  processes: Record<string, ProcessInfo>;
  onDismiss: () => void;
  interactive?: boolean;
};

import { HorizontalHeaderStrip } from "./HorizontalHeaderStrip.js";

export const RuntimeContextPanel = memo(function RuntimeContextPanel({
  project, processes, onDismiss, interactive = true,
}: Props) {
  const [selectedId, setSelectedId] = useState("");
  const {w} = usePanelSize({minWidth: 20, widthOffset: 6});
  const scrollRef = useRef<ScrollBoxHandle>(null);
  useScrollInput(scrollRef, interactive);
  const rows = useMemo(() => Object.values(processes).sort((left, right) =>
    (Number(right.actor_kind === "supervisor") - Number(left.actor_kind === "supervisor"))
      || left.created_at.localeCompare(right.created_at)), [processes]);
  const index = Math.max(0, rows.findIndex(row => row.process_id === selectedId));
  const selected = rows[index];
  useEffect(() => { scrollRef.current?.scrollTo(0); }, [selected?.process_id]);

  useInput((input, key) => {
    if (matchChord("escape", input, key)) {
      onDismiss();
      return;
    }
    if (matchChord("left", input, key) || matchChord("up", input, key)) {
      setSelectedId(rows[Math.max(0, index - 1)]?.process_id || "");
    } else if (matchChord("right", input, key) || matchChord("down", input, key)) {
      setSelectedId(rows[Math.min(rows.length - 1, index + 1)]?.process_id || "");
    }
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

  return <Box flexDirection="column" paddingLeft={1} paddingRight={1} flexGrow={1}>
    <Text bold>Context Usage — {project}</Text>
    <Box marginTop={1}>
      <HorizontalHeaderStrip items={rows.map(process => ({id: process.process_id,
        label: truncate(process.actor_kind === "supervisor" ? "Main process" : process.display_name || "Subprocess", Math.min(22, Math.max(8, w - 10)))}))}
        selected={index} width={w} contentFocused={!interactive}/>
    </Box>
    {selected ? <Text dimColor wrap="truncate-end">{index + 1}/{rows.length}  ·  {selected.display_name || (selected.actor_kind === "supervisor" ? "Main process" : "Subprocess")}  ·  {selected.process_id.slice(0, 8)}</Text> : null}
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1}>
    {!selected ? <Text dimColor>No durable process context yet.</Text> :
      <Box flexDirection="column" marginTop={1}>
        <Text bold>{selected.model_id && selected.model_id !== "unknown" ? selected.model_id : "Model not recorded for this process"}</Text>
        <Text>{used.toLocaleString()} / {limit ? limit.toLocaleString() : "unknown"} tokens ({percent.toFixed(1)}%)</Text>
        <Text dimColor>{context?.measurement === "provider" ? "provider measured" : context?.measurement === "provider+increment_estimate" ? "provider + new content estimate" : "estimated"}   epoch {context?.epoch ?? 0}   cache {Math.round(cache * 100)}%</Text>
        <Box flexDirection={sideBySide ? "row" : "column"} marginTop={1}>
        {limit > 0 ? <Box flexDirection="column" width={22} flexShrink={0} marginRight={sideBySide ? 3 : 0}>
          {Array.from({length: 10}, (_, row) => <Box key={row} flexDirection="row">
            {gridCells.slice(row * 10, row * 10 + 10).map((cell, column) =>
              <Text key={`${row}-${column}`} color={cell.color}>■ </Text>)}
          </Box>)}
        </Box> : null}
        <Box marginTop={sideBySide ? 0 : 1} flexDirection="column" flexGrow={1} minWidth={0}>
          {sections.length === 0
            ? <Text dimColor>Section measurements will appear after the next prompt compilation.</Text>
            : sections.map((section, sectionIndex) => <Text key={section.name}>
              <Text color={colors.contextMap.categories[sectionIndex % colors.contextMap.categories.length]}>■</Text>
              {" "}{legendName(section.name)} {section.estimated_tokens.toLocaleString().padStart(8)}   {limit ? (section.estimated_tokens / limit * 100).toFixed(1).padStart(5) : "  0.0"}%
            </Text>)}
          {limit > 0 ? <>
            <Text><Text color={colors.contextMap.free}>■</Text>{" "}{legendName("Free space")} {Math.max(0, free).toLocaleString().padStart(8)}   {(Math.max(0, free) / limit * 100).toFixed(1).padStart(5)}%</Text>
            {reserve > 0 ? <Text><Text color={colors.contextMap.compact}>■</Text>{" "}{legendName("Autocompact buffer")} {reserve.toLocaleString().padStart(8)}   {(reserve / limit * 100).toFixed(1).padStart(5)}%</Text> : null}
          </> : null}
        </Box>
        </Box>
      </Box>}
    </ScrollBox>
    <Text dimColor>{chordLabel("leftRight")} process   PgUp/PgDn scroll   {chordLabel("escape")} back</Text>
  </Box>;
});
