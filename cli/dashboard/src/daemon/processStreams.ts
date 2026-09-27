// Process-scoped transient stream registry.
//
// One native task request carries events from the root A and every delegated
// B. Keeping one reducer per request makes child activity overwrite Chat and
// leaves AgentDetail blind, so retain the same reducer state per process.
import { useSyncExternalStore } from "react";
import type { StreamingRow } from "../types.js";
import type { StreamEvent } from "./streamEvents.js";
import { finalizeTools, initStreamState, reduceStreamEvent } from "./streamReducer.js";

type Entry = StreamingRow & { process_id: string };

const entries = new Map<string, Entry>();
const listeners = new Set<() => void>();

function key(project: string, processId: string): string {
  return `${project}\u0000${processId}`;
}

function notify(): void {
  for (const listener of listeners) listener();
}

export function publishProcessStreamEvent(
  project: string,
  event: StreamEvent,
  startedAt: string,
): void {
  const processId = String(event.process_id || "");
  if (!project || !processId) return;
  const entryKey = key(project, processId);
  const previous = entries.get(entryKey);
  const state = initStreamState();
  if (previous) {
    state.text = previous.text;
    state.reasoning = previous.reasoning;
    state.tools = previous.tools;
    state.notices = previous.notices;
    state.activity = previous.activity || [];
    state.noticeKeys = new Set(previous.notices.map((notice) => notice.key));
    state.visibility = previous.visibility || "verbose";
    state.providerStep = previous.providerStep || 0;
    previous.tools.forEach((tool, index) => {
      const owner = tool.process_id || processId;
      if (tool.tool_call_id) state.toolIndex.set(`${owner}:${tool.tool_call_id}`, index);
      if (tool.tool_name) state.toolIndex.set(`${owner}:${tool.tool_name}`, index);
    });
  }
  const next = reduceStreamEvent(state, event);
  const terminal = event.event === "agent_terminal" || event.event === "agent_complete";
  const pausedForUser = event.event === "decision_required";
  const settled = terminal || pausedForUser;
  entries.set(entryKey, {
    process_id: processId,
    text: next.text,
    reasoning: settled ? "" : next.reasoning,
    tools: settled ? finalizeTools(next.tools) : next.tools,
    notices: next.notices,
    activity: next.activity,
    timestamp: previous?.timestamp || startedAt,
    visibility: next.visibility,
    providerStep: next.providerStep,
  });
  const projectPrefix = `${project}\u0000`;
  const projectEntries = [...entries.entries()]
    .filter(([candidate]) => candidate.startsWith(projectPrefix))
    .sort((left, right) => left[1].timestamp.localeCompare(right[1].timestamp));
  // The task budget bounds active agents, while this cap prevents completed
  // transient reasoning/tool buffers from accumulating for the whole app
  // lifetime. Durable outcomes and traces remain the historical authority.
  while (projectEntries.length > 64) {
    const oldest = projectEntries.shift();
    if (oldest) entries.delete(oldest[0]);
  }
  notify();
}

export function getProcessStream(
  project: string | null,
  processId: string | null,
): StreamingRow | null {
  if (!project || !processId) return null;
  return entries.get(key(project, processId)) ?? null;
}

export function useProcessStream(
  project: string | null,
  processId: string | null,
): StreamingRow | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    () => getProcessStream(project, processId),
  );
}
