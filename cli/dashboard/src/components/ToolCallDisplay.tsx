// src/components/ToolCallDisplay.tsx — Shared tool call card renderer.
// Used by ChatPanel and AgentDetail.

import React from "react";
import { Box, Text } from "@anthropic/ink";
import type { ToolCallCard } from "../types.js";
import { toolColor, toolIcon, colors } from "../theme/index.js";
import { Spinner } from "./Spinner.js";
import { DiffView } from "./DiffView.js";
import { parseUnifiedDiff } from "../utils/diff.js";

const DISPATCH_NAMES = ["dispatch_tool", "fork_agent"];

function extractPidShort(s: string | undefined): string | null {
  if (!s) return null;
  // Real pids are UUIDs (manager.fork → str(uuid.uuid4())); mock uses proc-XXX / cpl-XXX.
  const m = s.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?:proc|cpl)-\d{3}/i);
  return m ? m[0].slice(0, 8) : null;
}

export function ToolCallDisplay({ tool }: { tool: ToolCallCard }) {
  const color = toolColor(tool.tool_name);
  const icon = toolIcon(tool.tool_name);
  const state = tool.state ?? (tool.is_running ? "running" : tool.allowed ? "completed" : "error");
  const isRunning = state === "running" || state === "pending";
  const isError = state === "error";
  const isDone = state === "completed";
  const hasTarget = tool.target && tool.target.length > 0;
  const hasResult = tool.result_text && tool.result_text.length > 0;
  const diffFiles = isDone && tool.diff ? parseUnifiedDiff(tool.diff) : [];
  const isDispatch = DISPATCH_NAMES.some(
    (d) => tool.tool_name.includes(d) || d.includes(tool.tool_name),
  );
  const forkedPid = isDispatch
    ? extractPidShort(tool.result_text) || extractPidShort(tool.target)
    : null;

  return (
    <Box flexDirection="column">
      <Box flexDirection="row" gap={1}
        backgroundColor={isError ? colors.dangerBg : undefined}>
        {isRunning ? (
          <Spinner frames={colors.spinner.triangleFrames} intervalMs={colors.spinner.triangleIntervalMs} color={color} />
        ) : (
          <Text color={isError ? colors.danger : color} dimColor={isDone}>{icon}</Text>
        )}
        <Text color={color} dimColor={isDone}>{tool.tool_name}</Text>
        {hasTarget ? (
          <Text dimColor={isDone}>({tool.target.slice(0, 60)})</Text>
        ) : null}
        {forkedPid ? (
          <Text dimColor={isDone}>→ {forkedPid}</Text>
        ) : null}
      </Box>
      {isError && tool.blocked_reason ? (
        <Box flexDirection="row" paddingLeft={4}>
          <Text color={colors.warning} dimColor>{tool.blocked_reason.slice(0, 80)}</Text>
        </Box>
      ) : null}
      {hasResult ? (
        <Box flexDirection="row" paddingLeft={4}>
          <Text dimColor>{"⎿"} {tool.result_text!.slice(0, 120)}</Text>
        </Box>
      ) : null}
      {diffFiles.length > 0 ? (
        <DiffView files={diffFiles} />
      ) : null}
    </Box>
  );
}
