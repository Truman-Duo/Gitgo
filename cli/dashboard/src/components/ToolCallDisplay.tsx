// src/components/ToolCallDisplay.tsx — Shared tool call card renderer.
// Used by ChatPanel and AgentDetail.

import React, { useEffect, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ToolCallCard } from "../types.js";
import { toolIcon, colors, formatDuration } from "../theme/index.js";
import { Spinner } from "./Spinner.js";
import { DiffView } from "./DiffView.js";
import { parseUnifiedDiff } from "../utils/diff.js";

const DISPATCH_NAMES = ["dispatch_tool", "fork_agent", "delegate_task", "delegate_task_bundle"];

function extractPidShort(s: string | undefined): string | null {
  if (!s) return null;
  // Real pids are UUIDs (manager.fork → str(uuid.uuid4())); mock uses proc-XXX / cpl-XXX.
  const m = s.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?:proc|cpl)-\d{3}/i);
  return m ? m[0].slice(0, 8) : null;
}

export function ToolCallDisplay({ tool, expanded = false, width, displayName, count = 1,
  suppressDetails = false }: {
  tool: ToolCallCard; expanded?: boolean; width?: number; displayName?: string;
  count?: number; suppressDetails?: boolean;
}) {
  const [now, setNow] = useState(Date.now());
  const icon = toolIcon(tool.tool_name);
  const state = tool.state ?? (tool.is_running ? "running" : tool.allowed ? "completed" : "error");
  const isRunning = state === "running" || state === "pending";
  const isError = state === "error";
  const isUnresolved = state === "unresolved";
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
  useEffect(() => {
    if (!isRunning) return;
    const timer = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(timer);
  }, [isRunning]);
  const startedAt = Date.parse(tool.timestamp) || now;
  const duration = isRunning ? Math.max(0, now - startedAt) : Math.max(0, tool.duration_ms || 0);

  return (
    <Box flexDirection="column">
      <Box flexDirection="row" gap={1}
        backgroundColor={isError ? colors.dangerBg : undefined}>
        {tool.actor_label ? <Text dimColor>{tool.actor_label} ·</Text> : null}
        {isRunning ? (
          <Spinner frames={colors.spinner.triangleFrames} intervalMs={colors.spinner.triangleIntervalMs} color={colors.warning} />
        ) : (
          <Text color={isError ? colors.danger : isUnresolved ? colors.warning : undefined}
            dimColor={isDone}>{icon}</Text>
        )}
        <Text dimColor={isDone}>{displayName || tool.tool_name}{count > 1 ? ` (${count})` : ""}</Text>
        {hasTarget && !suppressDetails ? (
          <Text dimColor={isDone}>({expanded ? tool.target : tool.target.slice(0, 60)})</Text>
        ) : null}
        {forkedPid ? (
          <Text dimColor={isDone}>→ {forkedPid}</Text>
        ) : null}
        <Text dimColor>{formatDuration(duration)}</Text>
      </Box>
      {!suppressDetails && (isError || isUnresolved) && tool.blocked_reason ? (
        <Box flexDirection="row" paddingLeft={2}>
          <Text color={colors.warning} dimColor>{expanded ? tool.blocked_reason : tool.blocked_reason.slice(0, 80)}</Text>
        </Box>
      ) : null}
      {!suppressDetails && hasResult && (expanded || isError) ? (
        <Box flexDirection="row" paddingLeft={2}>
          <Text dimColor>{"⎿"} {expanded ? tool.result_text : tool.blocked_reason || tool.compact_summary || "failed"}</Text>
        </Box>
      ) : null}
      {!suppressDetails && !expanded && !isError && tool.compact_summary ? (
        <Box flexDirection="row" paddingLeft={2}>
          <Text dimColor>{"⎿"} {tool.compact_summary}</Text>
        </Box>
      ) : null}
      {!suppressDetails && diffFiles.length > 0 ? (
        <Box paddingLeft={2}><DiffView files={diffFiles} width={width ? Math.max(20, width - 2) : undefined} /></Box>
      ) : null}
    </Box>
  );
}
