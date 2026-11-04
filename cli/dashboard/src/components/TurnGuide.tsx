import React from "react";
import {Box, Text} from "@anthropic/ink";
import {colors} from "../theme/index.js";
import {geometry} from "../theme/tokens.js";

/** Endpoints occupy content-row gutter cells; tool nesting keeps its thin line. */
export function TurnGuide({first, last, children, active = true, status}: {
  first: boolean; last: boolean; children: React.ReactNode; active?: boolean; status?: string;
}) {
  if (!active) return <Box flexDirection="column" width="100%" paddingLeft={2}>{children}</Box>;
  const color = status === "failed" ? colors.danger
    : status === "cancelled" || status === "timed_out" || status === "degraded" ? colors.warning : colors.chat.turnGuide;
  return <Box flexDirection="column" width="100%">
    <Box flexDirection="column" width="100%" borderStyle={geometry.turnGuide.borderStyle} borderLeft
      borderRight={false} borderTop={false} borderBottom={false}
      borderColor={color} paddingLeft={1}>{children}</Box>
    {first ? <Box position="absolute" left={0} top={0} width={1} height={1}><Text color={color}>{geometry.turnGuide.top}</Text></Box> : null}
    {last ? <Box position="absolute" left={0} bottom={0} width={1} height={1}><Text color={color}>{geometry.turnGuide.bottom}</Text></Box> : null}
  </Box>;
}
