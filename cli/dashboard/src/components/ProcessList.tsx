import {useCopy} from "../ui/PageFrame.js";
import {label} from "../ui/runtimeLabels.js";
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

const listViewports = new Map<string, {top: number; selected: string; width: number}>();
// ── ProcessList component ─────────────────────────────────────

export const ProcessList = memo(function ProcessList({
  project,
  loopData,
  cols: _cols,
  selIdx,
  idsRef,
  onStatusUpdate,
}: Props) {
  const t=useCopy();
  const { w, h } = usePanelSize({ minWidth: 40, widthOffset: 2 });
  const scrollRef = useRef<ScrollBoxHandle>(null);
  useScrollInput(scrollRef);
  const selectedRow = useRef<DOMElement>(null);
  const restoreViewport = useRef(true);
  const currentViewport = useRef({project, selected: "", width: w, top: 0});
  const { processes, recoveryAvailable, storage } = loopData;

  const flatList = useMemo(() => {
    // /processlist is the project-wide B registry, not a task-run view. Keep
    // every worker/reviewer state across tasks and exclude all A supervisors.
    return visibleBProcesses(processes);
  }, [processes]);
  useLayoutEffect(() => {
    const saved = listViewports.get(project);
    if (restoreViewport.current && saved?.selected === flatList[selIdx]?.process_id && saved.width === w)
      scrollRef.current?.scrollTo(saved.top);
    else if (selectedRow.current) scrollRef.current?.scrollIntoView(selectedRow.current);
    restoreViewport.current = false;
  }, [selIdx, w, h, flatList[selIdx]?.process_id]);
  currentViewport.current = {project, selected: flatList[selIdx]?.process_id || "", width: w, top: 0};
  useLayoutEffect(() => {
    const handle = scrollRef.current;
    return () => {
      const value = currentViewport.current;
      listViewports.delete(value.project);
      listViewports.set(value.project, {...value, top: handle?.getScrollTop() || 0});
      if (listViewports.size > 16) listViewports.delete(listViewports.keys().next().value!);
    };
  }, [project]);

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

  useEffect(()=>{
    if(!onStatusUpdate)return;
    const pending=flatList.filter(p=>p.status==="awaiting_user").length;
    const blocked=flatList.filter(p=>["blocked","failed","timed_out","orphaned"].includes(p.status)).length;
    const parts=[t({en:`${flatList.length} subprocesses`,zh:`${flatList.length} 个子进程`})];
    if(runningCount)parts.push(t({en:`${runningCount} working`,zh:`${runningCount} 个工作中`}));
    if(waitingCount-pending)parts.push(t({en:`${waitingCount-pending} waiting`,zh:`${waitingCount-pending} 个等待中`}));
    if(pending)parts.push(t({en:`${pending} awaiting input`,zh:`${pending} 个待答复`}));
    if(blocked)parts.push(t({en:`${blocked} blocked / failed`,zh:`${blocked} 个受阻或失败`}));
    if(storage?.level&&storage.level!=="ok")parts.push(t({en:"Storage: ",zh:"存储："})+label(storage.level,t));
    if(recoveryAvailable.length)parts.push(t({en:`${recoveryAvailable.length} recovery candidates`,zh:`${recoveryAvailable.length} 项待恢复`}));
    onStatusUpdate(parts.join(" · "));
  },[flatList,runningCount,waitingCount,recoveryAvailable.length,storage?.level,onStatusUpdate,t]);

  const statusWidth = w<64?8:12;
  const nameWidth = Math.max(8,Math.min(Math.floor(w*0.55),w-statusWidth-3));
  const dependencies = (process: TopologyNode) =>
    process.depends_on?.length
      ? process.depends_on
      : process.dependency_ids.filter((id) => id !== process.parent_id);
  return (
    <Box flexDirection="column" paddingX={1} flexGrow={1} minHeight={0}>
      <Text bold>{t({en:"Subprocesses",zh:"子进程"})} · {project}</Text>
      <Text dimColor>
        {padEndWidth(t({en:"Process",zh:"进程"}),nameWidth)} {padEndWidth(t({en:"Status",zh:"状态"}),statusWidth)} {t({en:"Prerequisites",zh:"前置依赖"})}
      </Text>
      <ScrollBox
        ref={scrollRef}
        flexDirection="column"
        flexGrow={1}
        minHeight={0}
      >
        {flatList.length === 0 ? <Text dimColor>{t({en:"No subprocesses.",zh:"暂无子进程。"})}</Text> : null}
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
                  {t({en:"Main process / task",zh:"主进程 / 任务"})} {String(process.owner_group).slice(0, 8)}
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
                    truncate(label(process.status,t), statusWidth),
                    statusWidth,
                  )}{" "}
                  {process.cyclic
                    ? t({en:"Cycle / blocked chain",zh:"循环依赖 / 受阻链"})
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
      {recoveryAvailable.length ? (
        <Text color="#d29922">
          {t({en:`${recoveryAvailable.length} recovery candidates; explicit resume required.`,zh:`${recoveryAvailable.length} 项待恢复，须明确确认后恢复。`})}
        </Text>
      ) : null}

    </Box>
  );
});
