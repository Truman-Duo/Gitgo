// src/components/ProcessList.tsx — project subprocess flat list
import React, { memo, useMemo, useEffect } from "react";
import { Box, Text } from "@anthropic/ink";
import type { LoopData, ProcessInfo } from "../hooks/useLoopData.js";
import { usePanelSize, statusDot, indent as indentFn, useSelectionStyle, processStatusToDot, truncate, displayWidth } from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";
import { agentLabel } from "../daemon/agentLabels.js";

type Props = {
  project: string;
  loopData: LoopData;
  cols: number;
  selIdx: number;
  idsRef?: { current: string[] };
  onStatusUpdate?: (text: string) => void;
};

// ── DAG → deterministic topological list ─────────────────────

type TopologyNode = ProcessInfo & {
  depth: number;
  dependency_ids: string[];
  cyclic: boolean;
};

export function isBProcess(process: ProcessInfo): boolean {
  return process.actor_kind !== "supervisor"
    && (
      Boolean(process.parent_id)
      || ["worker", "reviewer"].includes(process.actor_kind || "")
    );
}

export function buildTopology(processes: Record<string, ProcessInfo>): TopologyNode[] {
  const byId = new Map(Object.values(processes).map((item) => [item.process_id, item]));
  const dependencies = new Map<string, string[]>();
  const dependents = new Map<string, string[]>();
  const indegree = new Map<string, number>();
  for (const process of byId.values()) {
    const declared = process.parent_ids && process.parent_ids.length > 0
      ? process.parent_ids
      : process.parent_id ? [process.parent_id] : [];
    const deps = [...new Set(declared)].filter((id) => byId.has(id) && id !== process.process_id);
    dependencies.set(process.process_id, deps);
    indegree.set(process.process_id, deps.length);
    for (const id of deps) dependents.set(id, [...(dependents.get(id) || []), process.process_id]);
  }
  const compare = (a: string, b: string) =>
    (byId.get(a)?.created_at || "").localeCompare(byId.get(b)?.created_at || "") || a.localeCompare(b);
  const ready = [...byId.keys()].filter((id) => indegree.get(id) === 0).sort(compare);
  const ordered: string[] = [];
  const depth = new Map<string, number>();
  while (ready.length > 0) {
    const id = ready.shift()!;
    ordered.push(id);
    for (const child of dependents.get(id) || []) {
      depth.set(child, Math.max(depth.get(child) || 0, (depth.get(id) || 0) + 1));
      indegree.set(child, (indegree.get(child) || 0) - 1);
      if (indegree.get(child) === 0) {
        ready.push(child);
        ready.sort(compare);
      }
    }
  }
  const cyclic = [...byId.keys()].filter((id) => !ordered.includes(id)).sort(compare);
  return [...ordered, ...cyclic].map((id) => ({
    ...byId.get(id)!,
    depth: depth.get(id) || 0,
    dependency_ids: dependencies.get(id) || [],
    cyclic: cyclic.includes(id),
  }));
}

export function visibleBProcesses(processes: Record<string, ProcessInfo>): TopologyNode[] {
  return buildTopology(Object.fromEntries(Object.entries(processes)
    .filter(([, process]) => isBProcess(process) && !process.archived)));
}

// ── ProcessList component ─────────────────────────────────────

export const ProcessList = memo(function ProcessList({
  project, loopData, cols: _cols, selIdx, idsRef, onStatusUpdate,
}: Props) {
  const { w } = usePanelSize({ minWidth: 60 });
  const { processes, recoveryAvailable, storage } = loopData;

  const flatList = useMemo(() => {
    // /processlist is the project-wide B registry, not a task-run view. Keep
    // every worker/reviewer state across tasks and exclude all A supervisors.
    return visibleBProcesses(processes);
  }, [processes]);

  const runningCount = flatList.filter((p) => p.status === "running").length;
  const waitingCount = flatList.filter((p) => ["waiting", "awaiting_user", "recovering", "resume_available"].includes(p.status)).length;

  // Keep idsRef in sync for keyboard navigation in App.tsx
  useEffect(() => {
    if (idsRef) {
      idsRef.current = flatList.map((p) => p.process_id);
    }
  }, [flatList, idsRef]);

  // Report status line to parent
  useEffect(() => {
    if (onStatusUpdate) {
      const wtCount = flatList.filter((p) => p.worktree?.isolated).length;
      const storagePrefix = storage?.level === "blocked" ? "storage blocked  |  " : "";
      onStatusUpdate(
        `${storagePrefix}● ${flatList.length} subprocesses  |  ${wtCount} isolated worktrees  |  ${recoveryAvailable.length} recovery candidates`
      );
    }
  }, [flatList, recoveryAvailable.length, storage?.level, onStatusUpdate]);

  const renderRow = (p: TopologyNode, i: number) => {
    const st = processStatusToDot(p.status);
    const dot = statusDot(st);
    const isSelected = i === selIdx;
    const rowSel = useSelectionStyle(isSelected ? "focused" : "non-focused", "row");
    const ind = indentFn(p.depth || 0);
    const steps =
      p.max_steps > 0
        ? `${p.steps_used}/${p.max_steps} steps`
        : "— steps";
    const dependency = p.dependency_ids.length > 1 ? `  deps:${p.dependency_ids.length}` : "";
    const cycle = p.cyclic ? "  cycle" : "";
    const children = p.child_ids && p.child_ids.length > 0 ? `  children:${p.child_ids.length}` : "";
    const decision = p.pending_decision ? "  decision" : "";
    const worktree = p.worktree
      ? `  wt:${p.worktree.isolated ? "isolated" : "shared"}${p.worktree.state ? `/${p.worktree.state}` : ""}${p.worktree.promoted ? "/promoted" : ""}`
      : "";
    const fixed = `${ind}● ${agentLabel(p)}  ${steps}  ring:${p.ring_level}${dependency}${cycle}${children}${decision}${worktree}`;
    const pathWidth = Math.max(0, w - displayWidth(fixed) - 2);
    return (
      <Box key={p.process_id} flexDirection="row">
        <Text backgroundColor={rowSel.bg} color={rowSel.fg} bold={rowSel.bold}>
          <Text dimColor>{ind}</Text>
          <Text color={dot.color}>● </Text>
          <Text>{agentLabel(p)}</Text>
          <Text dimColor>{`  ${steps}  ring:${p.ring_level}`}</Text>
          {dependency ? <Text dimColor>{dependency}</Text> : null}
          {cycle ? <Text dimColor>{cycle}</Text> : null}
          {children ? <Text dimColor>{children}</Text> : null}
          {decision ? <Text dimColor>{decision}</Text> : null}
          {p.worktree ? (
            <Text dimColor>
              {worktree}
              {p.worktree.path && pathWidth > 3 ? ` ${truncate(p.worktree.path, pathWidth - 1)}` : ""}
            </Text>
          ) : null}
        </Text>
      </Box>
    );
  };

  return (
    <Box flexDirection="column" paddingLeft={1} paddingRight={1} flexGrow={1}>
      {/* Header */}
      <Box flexDirection="row" gap={8}>
        <Text bold>Subprocesses</Text>
        <Text dimColor>—</Text>
        <Text>{project}</Text>
      </Box>

      {/* Project-wide B list: every status, across every task */}
      <Box flexDirection="column" flexGrow={1}>
        {recoveryAvailable.length > 0 ? (
          <Box marginBottom={1}>
            <Text dimColor>
              Recovery available for {recoveryAvailable.length} incomplete session(s); automatic replay is disabled.
            </Text>
          </Box>
        ) : null}
        {flatList.length === 0 ? (
          <Box paddingTop={1}>
            <Text dimColor>No subprocesses</Text>
          </Box>
        ) : (
          <>
            <>
              <Text dimColor bold>
                All subprocesses ({flatList.length}) · running {runningCount} · waiting {waitingCount}
              </Text>
              {flatList.map((p, i) => renderRow(p, i))}
            </>
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
