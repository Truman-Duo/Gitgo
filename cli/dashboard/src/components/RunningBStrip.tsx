// src/components/RunningBStrip.tsx — single-line running-B status strip (in the status bar).
// Modal selection: Tab enters/leaves (statusBarFocused), ←/→ pick, Enter opens.
import React from "react";
import { Box, Text } from "@anthropic/ink";
import type { ProcessInfo } from "../hooks/useLoopData.js";
import { colors } from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";

type Props = {
  runningB: ProcessInfo[];
  selIdx: number;
  focused: boolean;
  contextPct?: string;
};

export function RunningBStrip({ runningB, selIdx, focused, contextPct }: Props) {
  const selBg = colors.selection.row.bg;
  const selFg = colors.selection.row.fg;
  const hint = focused
    ? (runningB.length > 0
      ? `  ${chordLabel("leftRight")} choose · ${chordLabel("enter")} open · ${chordLabel("tab")} exit`
      : `  ${chordLabel("tab")} exit`)
    : (runningB.length > 0
      ? `  ${chordLabel("tab")} select · /processlist · ${chordLabel("shiftEnter")} input`
      : `  /processlist · ${chordLabel("shiftEnter")} input`);

  return (
    <Box flexDirection="row" flexWrap="wrap">
      {contextPct ? (
        <>
          <Text dimColor>{contextPct}</Text>
          <Text dimColor>  </Text>
        </>
      ) : null}
      {runningB.map((p, i) => {
        const isSel = focused && i === selIdx % Math.max(1, runningB.length);
        return (
          <React.Fragment key={p.process_id}>
            {i > 0 ? <Text dimColor> · </Text> : null}
            <Text color={isSel ? selFg : undefined} backgroundColor={isSel ? selBg : undefined} bold={isSel}>
              {p.role || "worker"} ({p.process_id.slice(0, 8)})
            </Text>
          </React.Fragment>
        );
      })}
      <Text dimColor>{hint}</Text>
    </Box>
  );
}
