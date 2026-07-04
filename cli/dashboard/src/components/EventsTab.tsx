// src/components/EventsTab.tsx — Context panel: Recent events + tool calls
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ToolEvent } from "../hooks/useLoopData.js";

type Props = { events: any[]; toolEvents: ToolEvent[]; width: number };

export const EventsTab = memo(function EventsTab({ events, toolEvents, width }: Props) {
  const combined = [
    ...toolEvents.slice(-10).map((t) => ({
      type: "tool",
      time: t.timestamp?.slice(11, 19) || "",
      text: `${t.role}/${t.tool_name} ${t.allowed ? "OK" : "DENIED"} ${t.duration_ms}ms`,
    })),
    ...events.slice(-10).map((e: any) => ({
      type: "event",
      time: e.timestamp?.slice(11, 19) || "",
      text: `${e.operation} ${e.status}`,
    })),
  ].slice(-15);

  if (combined.length === 0) return <Text dimColor>无最近事件</Text>;

  return (
    <Box flexDirection="column">
      {combined.map((item, i) => (
        <Box key={i} flexDirection="row">
          <Text dimColor>{item.time} </Text>
          <Text color={item.type === "tool" ? "cyan" : undefined}>
            {item.text.slice(0, width - 12)}
          </Text>
        </Box>
      ))}
    </Box>
  );
});
