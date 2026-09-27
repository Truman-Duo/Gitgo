import React, { type ReactNode } from "react";
import { Box, Text } from "@anthropic/ink";
import { colors, separator, usePanelSize } from "../theme/index.js";
import { HorizontalHeaderStrip } from "./HorizontalHeaderStrip.js";

export const RUNTIME_TABS = [
  { id: "lesson", label: "lesson" },
  { id: "contract", label: "contract" },
  { id: "governance", label: "governance" },
  { id: "memory", label: "memory" },
  { id: "context", label: "context" },
  { id: "tools", label: "tools" },
] as const;

export function RuntimeChildShell({ tab, children }: { tab: string; children: ReactNode }) {
  const { w } = usePanelSize({ minWidth: 40, widthOffset: 4 });
  const selected = Math.max(0, RUNTIME_TABS.findIndex(item => item.id === tab));
  return <Box flexDirection="column" paddingTop={1} paddingLeft={1} flexGrow={1}>
    <HorizontalHeaderStrip items={[...RUNTIME_TABS]} selected={selected} width={w} contentFocused />
    <Text color={colors.divider.color}>{separator(w)}</Text>
    <Box flexDirection="column" flexGrow={1}>{children}</Box>
  </Box>;
}
