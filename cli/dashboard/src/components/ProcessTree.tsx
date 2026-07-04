// src/components/ProcessTree.tsx — indented process tree (Claude Code SpinnerWithVerb pattern)
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ProcessInfo } from "../hooks/useLoopData.js";

type Props = {
  processes: Record<string, ProcessInfo>;
  sel: number;
  onSelect: (processId: string) => void;
  focus: boolean;
};

const STATUS_COLORS: Record<string, string> = {
  running: "green",
  waiting: "yellow",
  completed: "white",
  killed: "red",
  orphaned: "yellow",
};

const STATUS_ICONS: Record<string, string> = {
  running: "●",
  waiting: "◐",
  completed: "✓",
  killed: "✗",
  orphaned: "○",
};

export const ProcessTree = memo(function ProcessTree({
  processes,
  sel,
  onSelect,
  focus,
}: Props) {
  const procs = Object.values(processes);

  // Find root processes (no parent or parent not in this list)
  const pidSet = new Set(procs.map((p) => p.process_id));
  const roots = procs.filter((p) => !p.parent_id || !pidSet.has(p.parent_id));

  const renderNode = (
    p: ProcessInfo,
    depth: number,
    isLast: boolean,
    ancestors: boolean[],
    index: number,
  ): any => {
    const color = STATUS_COLORS[p.status] || "white";
    const icon = STATUS_ICONS[p.status] || "?";
    const isSelected = focus && index === sel;
    const indent = ancestors.map((a) => (a ? "  " : "│ ")).join("");
    const branch = depth > 0 ? (isLast ? "└─" : "├─") : "";
    const prefix = (indent + branch).slice(-30);

    const children = procs.filter(
      (c) => c.parent_id === p.process_id,
    );

    return (
      <Box key={p.process_id} flexDirection="column">
        <Box flexDirection="row">
          <Text color={isSelected ? "cyan" : undefined}>
            {isSelected && focus ? "▶ " : "  "}
          </Text>
          <Text dimColor>{prefix}</Text>
          <Text color={color}>{icon} </Text>
          <Text color={isSelected ? "cyan" : color} bold={isSelected}>
            {p.role || "agent"}
          </Text>
          <Text dimColor>
            {" "}{p.status} steps {p.steps_used}/{p.max_steps}
          </Text>
        </Box>
        {children.map((child, ci) =>
          renderNode(
            child,
            depth + 1,
            ci === children.length - 1,
            [...ancestors, !isLast],
            index + 1 + ci,
          ),
        )}
      </Box>
    );
  };

  if (procs.length === 0) {
    return (
      <Box paddingTop={1}>
        <Text dimColor>无活跃 Agent 进程</Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column">
      {roots.map((root, i) => renderNode(root, 0, i === roots.length - 1, [], i))}
    </Box>
  );
});
