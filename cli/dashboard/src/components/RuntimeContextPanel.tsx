import React, {memo, useMemo, useRef, useState} from "react";
import {Box, ScrollBox, Text} from "@anthropic/ink";
import type {ScrollBoxHandle} from "@anthropic/ink";
import type {ProcessInfo} from "../hooks/useLoopData.js";
import {chordLabel, matchChord} from "../input/bindings.js";
import {useManagedInput as useInput, useScrollInput} from "../input/runtime.js";
import {colors, useSelectionStyle} from "../theme/index.js";

type Props = {
  project: string;
  processes: Record<string, ProcessInfo>;
  onDismiss: () => void;
  interactive?: boolean;
};

const ProcessTab = memo(function ProcessTab({
  label, selected,
}: {label: string; selected: boolean}) {
  const style = useSelectionStyle(selected ? "focused" : "non-focused", "block");
  return <Text {...style}>{label + "  "}</Text>;
});

export const RuntimeContextPanel = memo(function RuntimeContextPanel({
  project, processes, onDismiss, interactive = true,
}: Props) {
  const [index, setIndex] = useState(0);
  const scrollRef = useRef<ScrollBoxHandle>(null);
  useScrollInput(scrollRef, interactive);
  const rows = useMemo(() => Object.values(processes).sort((left, right) =>
    (left.actor_kind === "supervisor" ? -1 : right.actor_kind === "supervisor" ? 1 : 0)
      || left.created_at.localeCompare(right.created_at)), [processes]);
  const selected = rows[Math.min(index, Math.max(0, rows.length - 1))];

  useInput((input, key) => {
    if (matchChord("escape", input, key)) {
      onDismiss();
      return;
    }
    if (matchChord("left", input, key) || matchChord("up", input, key)) {
      setIndex(value => Math.max(0, value - 1));
    } else if (matchChord("right", input, key) || matchChord("down", input, key)) {
      setIndex(value => Math.min(Math.max(0, rows.length - 1), value + 1));
    }
  }, {isActive: interactive});

  const context = selected?.context;
  const used = Number(context?.used_tokens || context?.estimated_tokens || selected?.estimated_tokens || 0);
  const limit = Number(context?.limit || 0);
  const percent = limit ? Math.min(999, used / limit * 100) : 0;
  const sections = context?.breakdown?.sections || [];
  const reserve = Number(context?.auto_compact_tokens ?? (context?.auto_compact_enabled === false ? 0 : limit * 0.1));
  const free = Number(context?.free_tokens ?? Math.max(0, limit - used - reserve));
  const cache = Number(
    selected?.cache_summary?.eligible_hit_ratio
      || selected?.cache_summary?.raw_hit_ratio
      || 0,
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
    <Box flexDirection="row" marginTop={1}>
      {rows.map((process, rowIndex) => <ProcessTab
        key={process.process_id}
        selected={rowIndex === index}
        label={process.actor_kind === "supervisor" ? "Main process" : process.display_name || "Subprocess"}
      />)}
    </Box>
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1}>
    {!selected ? <Text dimColor>No durable process context yet.</Text> :
      <Box flexDirection="column" marginTop={1}>
        <Text bold>{selected.model_id || "Unknown model"}</Text>
        <Text>{used.toLocaleString()} / {limit ? limit.toLocaleString() : "unknown"} tokens ({percent.toFixed(1)}%)</Text>
        <Text dimColor>{context?.measurement === "provider" ? "provider measured" : "estimated"}   epoch {context?.epoch ?? 0}   cache {Math.round(cache * 100)}%</Text>
        {limit > 0 ? <Box flexDirection="column" marginTop={1}>
          {Array.from({length: 10}, (_, row) => <Box key={row} flexDirection="row">
            {gridCells.slice(row * 10, row * 10 + 10).map((cell, column) =>
              <Text key={`${row}-${column}`} color={cell.color}>■ </Text>)}
          </Box>)}
        </Box> : null}
        <Box marginTop={1} flexDirection="column">
          {sections.length === 0
            ? <Text dimColor>Section measurements will appear after the next prompt compilation.</Text>
            : sections.map((section, sectionIndex) => <Text key={section.name}>
              <Text color={colors.contextMap.categories[sectionIndex % colors.contextMap.categories.length]}>■</Text>
              {" "}{section.name.padEnd(29)} {section.estimated_tokens.toLocaleString().padStart(8)}   {limit ? (section.estimated_tokens / limit * 100).toFixed(1).padStart(5) : "  0.0"}%
            </Text>)}
          {limit > 0 ? <>
            <Text><Text color={colors.contextMap.free}>■</Text>{" "}{"Free space".padEnd(29)} {Math.max(0, free).toLocaleString().padStart(8)}   {(Math.max(0, free) / limit * 100).toFixed(1).padStart(5)}%</Text>
            {reserve > 0 ? <Text><Text color={colors.contextMap.compact}>■</Text>{" "}{"Autocompact buffer".padEnd(29)} {reserve.toLocaleString().padStart(8)}   {(reserve / limit * 100).toFixed(1).padStart(5)}%</Text> : null}
          </> : null}
        </Box>
      </Box>}
    </ScrollBox>
    <Text dimColor>{chordLabel("leftRight")} process   PgUp/PgDn scroll   {chordLabel("escape")} back</Text>
  </Box>;
});
