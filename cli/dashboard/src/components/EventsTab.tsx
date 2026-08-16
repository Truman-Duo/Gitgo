// src/components/EventsTab.tsx — Context panel: Recent events with tool icons
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ToolEvent } from "../hooks/useLoopData.js";
import { toolColor, toolIcon, colors } from "../theme/index.js";

type Props = { events: any[]; toolEvents: ToolEvent[]; width: number };

export const EventsTab = memo(function EventsTab({ events, toolEvents, width }: Props) {
  const toolItems = toolEvents.slice(-15).map((t) => ({
    type: "tool" as const,
    time: t.timestamp?.slice(11, 19) || "",
    toolName: t.tool_name,
    allowed: t.allowed,
    agent: t.role || t.process_id?.slice(0, 8) || "",
  }));

  const eventItems = events.slice(-5).map((e: any) => ({
    type: "event" as const,
    time: e.timestamp?.slice(11, 19) || "",
    text: `${e.operation} ${e.status}`,
  }));

  const combined = [...toolItems, ...eventItems]
    .sort((a, b) => b.time.localeCompare(a.time))
    .slice(0, 15);

  if (combined.length === 0) return <Text dimColor>No recent events</Text>;

  return (
    <Box flexDirection="column">
      <Text bold dimColor>Recent Events</Text>
      {combined.map((item, i) => {
        if (item.type === "event") {
          return (
            <Box key={i} flexDirection="row">
              <Text dimColor>{item.time} </Text>
              <Text dimColor>{item.text.slice(0, width - 12)}</Text>
            </Box>
          );
        }
        const tItem = item as typeof toolItems[0];
        const tColor = toolColor(tItem.toolName);
        const icon = toolIcon(tItem.toolName);
        const statusColor = tItem.allowed ? colors.named.green : colors.named.red;
        const statusLabel = tItem.allowed ? "OK" : "BLOCKED";
        return (
          <Box key={i} flexDirection="row">
            <Text dimColor>{tItem.time} </Text>
            <Text color={tColor}>{icon} {tItem.toolName}</Text>
            <Text> </Text>
            <Text color={statusColor}>{statusLabel}</Text>
            {tItem.agent ? (
              <Text dimColor> {tItem.agent}</Text>
            ) : null}
          </Box>
        );
      })}
    </Box>
  );
});
