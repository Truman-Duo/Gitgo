// src/components/ProcessList.tsx — B Agent process flat list (blueprint L547-564)
import React, { memo, useMemo, useEffect } from "react";
import { Box, Text } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import { useLoopData, type ProcessInfo } from "../hooks/useLoopData.js";
import { colors, usePanelSize, statusDot, badgeBg, indent as indentFn, useSelectionStyle, processStatusToDot, partitionByRank } from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";

type Props = {
  client: McpClient;
  project: string;
  cols: number;
  selIdx: number;
  idsRef?: { current: string[] };
  onStatusUpdate?: (text: string) => void;
};

// ── Tree → flat list (depth for indentation) ──────────────────

type TreeNode = ProcessInfo & { depth: number; children: TreeNode[] };

function buildTree(processes: Record<string, ProcessInfo>): TreeNode[] {
  const procs = Object.values(processes);
  const childrenMap = new Map<string | null, ProcessInfo[]>();
  for (const p of procs) {
    const parentKey = p.parent_id || null;
    if (!childrenMap.has(parentKey)) childrenMap.set(parentKey, []);
    childrenMap.get(parentKey)!.push(p);
  }
  function walk(parentId: string | null, depth: number): TreeNode[] {
    const kids = childrenMap.get(parentId) || [];
    return kids.map((p) => ({
      ...p,
      depth,
      children: walk(p.process_id, depth + 1),
    }));
  }
  return walk(null, 0);
}

function flatten(nodes: TreeNode[]): TreeNode[] {
  const out: TreeNode[] = [];
  function walk(list: TreeNode[]) {
    for (const n of list) {
      out.push(n);
      walk(n.children);
    }
  }
  walk(nodes);
  return out;
}

// ── ProcessList component ─────────────────────────────────────

export const ProcessList = memo(function ProcessList({
  client, project, cols: _cols, selIdx, idsRef, onStatusUpdate,
}: Props) {
  const { w } = usePanelSize({ minWidth: 60 });
  const { processes } = useLoopData(client, project, 5);

  const flatList = useMemo(() => {
    const tree = buildTree(processes);
    return flatten(tree);
  }, [processes]);

  const { running, pending, finished } = useMemo(
    () => partitionByRank(flatList, (p: TreeNode) => (p.status === "running" ? 0 : p.status === "waiting" ? 1 : 2)),
    [flatList],
  );

  // Keep idsRef in sync for keyboard navigation in App.tsx
  useEffect(() => {
    if (idsRef) {
      idsRef.current = [...running, ...pending, ...finished].map((p) => p.process_id);
    }
  }, [running, pending, finished, idsRef]);

  // Report status line to parent
  useEffect(() => {
    if (onStatusUpdate) {
      const wtCount = flatList.filter((p) => p.worktree_path).length;
      onStatusUpdate(
        `● ${flatList.length} processes  |  ${wtCount} worktrees active`
      );
    }
  }, [flatList, onStatusUpdate]);

  const renderRow = (p: TreeNode, i: number) => {
    const st = processStatusToDot(p.status);
    const dot = statusDot(st);
    const isSelected = i === selIdx;
    const rowSel = useSelectionStyle(isSelected ? "focused" : "non-focused", "row");
    const ind = indentFn(p.depth || 0);
    const steps =
      p.max_steps > 0
        ? `${p.steps_used}/${p.max_steps} steps`
        : "— steps";
    return (
      <Box key={p.process_id} flexDirection="row"
        backgroundColor={rowSel.bg}
      >
        <Text dimColor backgroundColor={rowSel.bg}>{ind}</Text>
        <Text color={dot.color} backgroundColor={rowSel.bg}>● </Text>
        <Text
          color={rowSel.fg}
          bold={rowSel.bold}
          backgroundColor={rowSel.bg}
        >
          {p.role || "agent"}
        </Text>
        <Text backgroundColor={rowSel.bg}> </Text>
        <Text backgroundColor={dot.badgeBg} color={dot.color}>
          {p.status}
        </Text>
        <Text dimColor backgroundColor={rowSel.bg}>  {steps}</Text>
        <Text dimColor backgroundColor={rowSel.bg}>  ring:{p.ring_level}</Text>
        {p.worktree_path ? (
          <Text dimColor backgroundColor={rowSel.bg}>  wt: {p.worktree_path}</Text>
        ) : null}
      </Box>
    );
  };

  return (
    <Box flexDirection="column" paddingLeft={1} paddingRight={1} flexGrow={1}>
      {/* Header */}
      <Box flexDirection="row" gap={8}>
        <Text bold color={colors.accent}>B Agent Processes</Text>
        <Text dimColor>—</Text>
        <Text color={colors.success}>{project}</Text>
      </Box>

      {/* Grouped list: running then finished */}
      <Box flexDirection="column" flexGrow={1}>
        {flatList.length === 0 ? (
          <Box paddingTop={1}>
            <Text dimColor>No active processes</Text>
          </Box>
        ) : (
          <>
            {running.length > 0 ? (
              <>
                <Text dimColor bold>Running ({running.length})</Text>
                {running.map((p, i) => renderRow(p, i))}
              </>
            ) : null}
            {pending.length > 0 ? (
              <>
                <Text dimColor bold>Pending ({pending.length})</Text>
                {pending.map((p, i) => renderRow(p, running.length + i))}
              </>
            ) : null}
            {finished.length > 0 ? (
              <>
                <Text dimColor bold>Finished ({finished.length})</Text>
                {finished.map((p, i) => renderRow(p, running.length + pending.length + i))}
              </>
            ) : null}
          </>
        )}
      </Box>

      {/* Footer */}
      <Box marginTop={1}>
        <Text dimColor>{chordLabel("upDown")} select  {chordLabel("enter")} detail  {chordLabel("tab")} chat  {chordLabel("escape")} back</Text>
      </Box>
    </Box>
  );
});
