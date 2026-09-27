import React from "react";
import { Box, Text } from "@anthropic/ink";
import type { ProcessInfo } from "../hooks/useLoopData.js";
import { colors, displayWidth } from "../theme/index.js";
import { ContextStatus } from "./ContextStatus.js";
import { agentLabel } from "../daemon/agentLabels.js";

type Props = {
  runningB: ProcessInfo[]; selIdx: number; focused: boolean;
  contextPct?: string; cachePct?: number | null; width: number;
};

function fitAgents(runningB: ProcessInfo[], selected: number, width: number) {
  if (width <= 0 || runningB.length === 0) {
    return {items: [] as ProcessInfo[], left: false, right: false, start: 0};
  }
  let start = Math.max(0, Math.min(selected, runningB.length - 1));
  let end = start + 1;
  const renderedWidth = (from: number, to: number) => {
    const labels = runningB.slice(from, to).reduce(
      (sum, item) => sum + displayWidth(agentLabel(item)), 0,
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

export function RunningBStrip({ runningB, selIdx, focused, contextPct, cachePct, width }: Props) {
  const hint = "/processlist · ← projects · type to chat";
  // Both edge regions are stable; only the B strip scrolls/reflows.
  const leftWidth = 6;
  const rightWidth = displayWidth(hint);
  const middleWidth = Math.max(0, width - leftWidth - rightWidth - 6);
  const selected = selIdx % Math.max(1, runningB.length);
  const fitted = fitAgents(runningB, selected, middleWidth);
  return <Box flexDirection="row" flexWrap="nowrap" width={Math.max(1, width)}>
    <Box width={leftWidth}><ContextStatus contextPct={contextPct || "0%"} cachePct={cachePct} /></Box>
    <Text dimColor> · </Text>
    <Box width={middleWidth} flexDirection="row" overflow="hidden">
      {fitted.left ? <Text dimColor>... </Text> : null}
      {fitted.items.map((process, index) => {
        const actualIndex = fitted.start + index;
        const selectedAgent = focused && actualIndex === selected;
        return <React.Fragment key={process.process_id}>
          {index > 0 ? <Text dimColor> · </Text> : null}
          <Text
            color={selectedAgent ? colors.selection.row.fg : undefined}
            backgroundColor={selectedAgent ? colors.selection.row.bg : undefined}
            bold={selectedAgent}
          >{agentLabel(process)}</Text>
        </React.Fragment>;
      })}
      {fitted.right ? <Text dimColor> ...</Text> : null}
    </Box>
    <Text dimColor> · </Text>
    <Box width={rightWidth} justifyContent="flex-end"><Text dimColor>{hint}</Text></Box>
  </Box>;
}
