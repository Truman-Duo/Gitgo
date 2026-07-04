// src/components/AgentDetail.tsx — Scene 3: B Agent detail + color-coded tool cards
import React, { memo, useState, useEffect } from "react";
import { Box, Text } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import type { ProcessInfo, ToolEvent } from "../hooks/useLoopData.js";

// ── Tool color coding (DeepSeek pattern) ────────────────────

const TOOL_COLORS: Record<string, string> = {
  read_file: "cyan", scan: "cyan", glob: "cyan", grep: "cyan",
  write_file: "green", edit: "green",
  bash: "yellow", shell: "yellow", execute: "yellow",
  dispatch_tool: "magenta", fork_agent: "magenta",
};

function toolColor(toolName: string): string {
  for (const [key, color] of Object.entries(TOOL_COLORS)) {
    if (toolName.includes(key) || key.includes(toolName)) return color;
  }
  return "white";
}

function toolIcon(toolName: string): string {
  const c = toolColor(toolName);
  if (c === "cyan") return "◇";    // ◇ read
  if (c === "green") return "◆";   // ◆ write
  if (c === "yellow") return "▶";  // ▶ shell
  if (c === "magenta") return "◎"; // ◎ dispatch
  return "●";                       // ●
}

// ── AgentDetail component ────────────────────────────────────

type Props = {
  process: ProcessInfo;
  toolEvents: ToolEvent[];
  cols: number;
  rows: number;
};

export const AgentDetail = memo(function AgentDetail({
  process, toolEvents, cols, rows,
}: Props) {
  const width = Math.max(50, cols);
  const detailW = width - 4;
  const statusColor =
    process.status === "running" ? "green"
    : process.status === "killed" ? "red"
    : process.status === "orphaned" ? "yellow"
    : "white";

  const processTools = toolEvents.filter(
    (t) => t.process_id === process.process_id,
  );

  const pct = process.max_steps > 0
    ? Math.min(1, process.steps_used / process.max_steps) : 0;
  const barLen = Math.min(30, detailW - 15);
  const filled = Math.round(pct * barLen);
  const bar = "█".repeat(filled) + "░".repeat(barLen - filled);

  return (
    <Box flexDirection="column" paddingLeft={1} width={width}>
      <Box marginBottom={1}>
        <Text bold color="cyan">
          B Agent: {process.role} ({process.process_id.slice(0, 8)})
        </Text>
      </Box>

      <Box flexDirection="column" marginBottom={1}>
        <Box flexDirection="row">
          <Text>Status: </Text>
          <Text color={statusColor} bold>{process.status}</Text>
          <Text>  Ring: {process.ring_level}</Text>
          <Text>  Parent: {process.parent_id?.slice(0, 8) || "none"}</Text>
        </Box>
        <Box flexDirection="row" marginTop={1}>
          <Text>Steps: </Text>
          <Text color={pct > 0.8 ? "yellow" : "cyan"}>
            {process.steps_used}/{process.max_steps}
          </Text>
          <Text> [{bar}]</Text>
        </Box>
        <Box flexDirection="row">
          <Text dimColor>Created: {process.created_at?.slice(0, 19) || "?"}</Text>
        </Box>
      </Box>

      <Box marginBottom={1}>
        <Text bold>Tool Calls ({processTools.length})</Text>
      </Box>

      <Box flexDirection="column">
        {processTools.length === 0 ? (
          <Text dimColor>此 Agent 尚无工具调用记录</Text>
        ) : (
          processTools.map((t, i) => {
            const color = toolColor(t.tool_name);
            const icon = toolIcon(t.tool_name);
            const mark = t.allowed ? "OK" : "DENIED";
            const markColor = t.allowed ? "green" : "red";
            return (
              <Box key={i} flexDirection="row" marginBottom={1}>
                <Text color={color}>{icon} </Text>
                <Text color={color}>{t.tool_name}</Text>
                <Text dimColor> ({t.duration_ms}ms)</Text>
                <Text color={markColor}> [{mark}]</Text>
              </Box>
            );
          })
        )}
      </Box>
    </Box>
  );
});

// ── AgentDetailScene — data-fetching wrapper for Scene 3 ─────

type SceneProps = {
  client: McpClient;
  activeProject: string | null;
  activeAgentId: string | null;
  cols: number;
  rows: number;
};

export const AgentDetailScene = memo(function AgentDetailScene({
  client, activeProject, activeAgentId, cols, rows,
}: SceneProps) {
  const [data, setData] = useState<{
    processes: Record<string, ProcessInfo>;
    toolEvents: ToolEvent[];
    loading: boolean;
  }>({ processes: {}, toolEvents: [], loading: true });

  useEffect(() => {
    if (!client || !activeProject) return;
    let cancelled = false;
    (async () => {
      try {
        const result: any = await client.callTool("gitgo_loop_status", {
          project: activeProject,
        });
        if (cancelled) return;
        const procs: Record<string, ProcessInfo> = {};
        if (result?.processes) {
          for (const [pid, p] of Object.entries(result.processes)) {
            procs[pid] = p as ProcessInfo;
          }
        }
        setData({
          processes: procs,
          toolEvents: (result?.recent_tool_executed || []) as ToolEvent[],
          loading: false,
        });
      } catch {
        if (!cancelled) setData({ processes: {}, toolEvents: [], loading: false });
      }
    })();
    return () => { cancelled = true; };
  }, [client, activeProject]);

  if (data.loading) {
    return (
      <Box paddingLeft={1}>
        <Text dimColor>Loading agent data...</Text>
      </Box>
    );
  }

  const process = activeAgentId ? data.processes[activeAgentId] : null;
  if (!process) {
    return (
      <Box paddingLeft={1} flexDirection="column">
        <Text color="red">Agent not found: {activeAgentId}</Text>
        <Text dimColor>Process may have been killed or completed.</Text>
      </Box>
    );
  }

  return (
    <AgentDetail
      process={process}
      toolEvents={data.toolEvents}
      cols={cols}
      rows={rows}
    />
  );
});
