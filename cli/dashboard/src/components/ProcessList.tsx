// src/components/ProcessList.tsx — project subprocess flat list
import React, { memo, useMemo, useEffect, useRef, useLayoutEffect } from "react";
import { Box, Text, ScrollBox } from "@anthropic/ink";
import type {DOMElement, ScrollBoxHandle} from "@anthropic/ink";
import type { LoopData, ProcessInfo } from "../hooks/useLoopData.js";
import {
  usePanelSize,
  statusDot,
  useSelectionStyle,
  processStatusToDot,
  truncate,
  padEndWidth,
} from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";
import { useScrollInput } from "../input/runtime.js";
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
  tree_prefix?: string;
  owner_group?: string;
};

export function isBProcess(process: ProcessInfo): boolean {
  return (
    process.actor_kind !== "supervisor" &&
    (Boolean(process.parent_id) ||
      ["worker", "reviewer"].includes(process.actor_kind || ""))
  );
}

export function buildTopology(
  processes: Record<string, ProcessInfo>,
): TopologyNode[] {
  const byId = new Map(
    Object.values(processes).map((item) => [item.process_id, item]),
  );
  const dependencies = new Map<string, string[]>();
  const dependents = new Map<string, string[]>();
  const indegree = new Map<string, number>();
  for (const process of byId.values()) {
    const declared =
      process.parent_ids && process.parent_ids.length > 0
        ? process.parent_ids
        : process.parent_id
          ? [process.parent_id]
          : [];
    const deps = [
      ...new Set([...declared, ...(process.depends_on || [])]),
    ].filter((id) => byId.has(id) && id !== process.process_id);
    dependencies.set(process.process_id, deps);
    indegree.set(process.process_id, deps.length);
    for (const id of deps)
      dependents.set(id, [...(dependents.get(id) || []), process.process_id]);
  }
  const compare = (a: string, b: string) =>
    (byId.get(a)?.created_at || "").localeCompare(
      byId.get(b)?.created_at || "",
    ) || a.localeCompare(b);
  const ready = [...byId.keys()]
    .filter((id) => indegree.get(id) === 0)
    .sort(compare);
  const ordered: string[] = [];
  const depth = new Map<string, number>();
  while (ready.length > 0) {
    const id = ready.shift()!;
    ordered.push(id);
    for (const child of dependents.get(id) || []) {
      depth.set(
        child,
        Math.max(depth.get(child) || 0, (depth.get(id) || 0) + 1),
      );
      indegree.set(child, (indegree.get(child) || 0) - 1);
      if (indegree.get(child) === 0) {
        ready.push(child);
        ready.sort(compare);
      }
    }
  }
  const cyclic = [...byId.keys()]
    .filter((id) => !ordered.includes(id))
    .sort(compare);
  return [...ordered, ...cyclic].map((id) => ({
    ...byId.get(id)!,
    depth: depth.get(id) || 0,
    dependency_ids: dependencies.get(id) || [],
    cyclic: cyclic.includes(id),
  }));
}

export function visibleBProcesses(
  processes: Record<string, ProcessInfo>,
): TopologyNode[] {
  const topological = buildTopology(
    Object.fromEntries(
      Object.entries(processes).filter(
        ([, process]) => isBProcess(process) && !process.archived,
      ),
    ),
  );
  const byId = new Map(topological.map((item) => [item.process_id, item]));
  const children = new Map<string, TopologyNode[]>();
  for (const item of topological) {
    const owner =
      item.parent_id && byId.has(item.parent_id) ? item.parent_id : "";
    children.set(owner, [...(children.get(owner) || []), item]);
  }
  const result: TopologyNode[] = [];
  const visited = new Set<string>();
  const walk = (
    item: TopologyNode,
    prefix: string,
    last: boolean,
    group: string,
  ) => {
    if (visited.has(item.process_id)) return;
    visited.add(item.process_id);
    result.push({
      ...item,
      tree_prefix: prefix + (last ? "└─ " : "├─ "),
      owner_group: group,
    });
    const nested = children.get(item.process_id) || [];
    nested.forEach((child, index) =>
      walk(
        child,
        prefix + (last ? "   " : "│  "),
        index === nested.length - 1,
        group,
      ),
    );
  };
  const roots = children.get("") || [];
  const groups = new Map<string, TopologyNode[]>();
  for (const item of roots) {
    const group = item.parent_id || item.active_task_id || "independent";
    groups.set(group, [...(groups.get(group) || []), item]);
  }
  for (const [group, roots] of groups)
    roots.forEach((item, index) =>
      walk(item, "", index === roots.length - 1, group),
    );
  for (const item of topological)
    if (!visited.has(item.process_id))
      walk(item, "", true, "unresolved ownership");
  return result;
}

// ── ProcessList component ─────────────────────────────────────

export const ProcessList = memo(function ProcessList({
  project,
  loopData,
  cols: _cols,
  selIdx,
  idsRef,
  onStatusUpdate,
}: Props) {
  const { w, h } = usePanelSize({ minWidth: 40, widthOffset: 2 });
  const scrollRef = useRef<ScrollBoxHandle>(null);
  useScrollInput(scrollRef);
  const selectedRow = useRef<DOMElement>(null);
  const { processes, recoveryAvailable, storage } = loopData;

  const flatList = useMemo(() => {
    // /processlist is the project-wide B registry, not a task-run view. Keep
    // every worker/reviewer state across tasks and exclude all A supervisors.
    return visibleBProcesses(processes);
  }, [processes]);
  useLayoutEffect(() => {
    if (selectedRow.current) scrollRef.current?.scrollIntoView(selectedRow.current);
  }, [selIdx, w, h, flatList[selIdx]?.process_id]);

  const runningCount = flatList.filter((p) => p.status === "running").length;
  const waitingCount = flatList.filter((p) =>
    ["waiting", "awaiting_user", "recovering", "resume_available"].includes(
      p.status,
    ),
  ).length;

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
      const storagePrefix =
        storage?.level === "blocked" ? "storage blocked  |  " : "";
      onStatusUpdate(
        `${storagePrefix}● ${flatList.length} subprocesses  |  ${wtCount} isolated worktrees  |  ${recoveryAvailable.length} recovery candidates`,
      );
    }
  }, [flatList, recoveryAvailable.length, storage?.level, onStatusUpdate]);

  const selected = flatList[selIdx];
  const nameWidth = Math.max(18, Math.floor(w * 0.55));
  const statusWidth = 16;
  const dependencies = (process: TopologyNode) =>
    process.depends_on?.length
      ? process.depends_on
      : process.dependency_ids.filter((id) => id !== process.parent_id);
  return (
    <Box flexDirection="column" paddingX={1} flexGrow={1} minHeight={0}>
      <Text bold>Subprocesses · {project}</Text>
      <Text dimColor>
        {flatList.length} subprocesses · {runningCount} running · {waitingCount}{" "}
        waiting · tree: ownership · ←: prerequisites
      </Text>
      <Text dimColor>
        {padEndWidth("Process", nameWidth)} {"Status".padEnd(statusWidth)}{" "}
        Prerequisites
      </Text>
      <ScrollBox
        ref={scrollRef}
        flexDirection="column"
        flexGrow={1}
        minHeight={0}
      >
        {flatList.length === 0 ? <Text dimColor>No subprocesses.</Text> : null}
        {flatList.map((process, index) => {
          const dot = statusDot(processStatusToDot(process.status));
          const active = index === selIdx;
          const style = useSelectionStyle(
            active ? "focused" : "non-focused",
            "row",
          );
          const deps = dependencies(process);
          const previous = flatList[index - 1];
          const title = `${process.tree_prefix || ""}${process.process_id.slice(0, 6)} ${agentLabel(process)}`;
          return (
            <React.Fragment key={process.process_id}>
              {previous?.owner_group !== process.owner_group ? (
                <Text dimColor>
                  Supervisor / task {String(process.owner_group).slice(0, 8)}
                </Text>
              ) : null}
              <Box ref={active ? selectedRow : undefined} flexDirection="row">
              <Text
                backgroundColor={style.bg}
                color={style.fg}
                bold={style.bold}
              >
                <Text color={dot.color}>● </Text>
                {padEndWidth(
                  truncate(title, nameWidth - 2),
                  nameWidth - 2,
                )}{" "}
                {padEndWidth(
                  truncate(process.status.replace(/_/g, " "), statusWidth),
                  statusWidth,
                )}{" "}
                {process.cyclic
                  ? "Cycle / blocked chain"
                  : deps.length
                    ? `← ${deps
                        .slice(0, 2)
                        .map((id) => id.slice(0, 6))
                        .join(
                          ", ",
                        )}${deps.length > 2 ? ` +${deps.length - 2}` : ""}`
                    : "—"}
              </Text>
              </Box>
            </React.Fragment>
          );
        })}
      </ScrollBox>
      {selected ? (
        <>
          <Text dimColor wrap="truncate-end">
            {agentLabel(selected)} · {selected.role} · {selected.steps_used}/
            {selected.max_steps} steps ·{" "}
            {selected.model_id || "model unrecorded"}
          </Text>
          <Text dimColor wrap="truncate-end">
            {selected.worktree?.isolated
              ? "Isolated worktree"
              : "Shared workspace"}{" "}
            ·{" "}
            {selected.worktree?.path ||
              selected.worktree_path ||
              "path unrecorded"}
            {dependencies(selected).length
              ? ` · Requires: ${dependencies(selected)
                  .map((id) => processes[id]?.display_name || id.slice(0, 8))
                  .join(", ")}`
              : ""}
          </Text>
        </>
      ) : null}
      {recoveryAvailable.length ? (
        <Text color="#d29922">
          {recoveryAvailable.length} recovery candidates; resume remains
          explicit.
        </Text>
      ) : null}
      <Text dimColor>
        {chordLabel("upDown")} select · PgUp/PgDn scroll · {chordLabel("enter")}{" "}
        detail · {chordLabel("tab")} chat · {chordLabel("escape")} back
      </Text>
    </Box>
  );
});
