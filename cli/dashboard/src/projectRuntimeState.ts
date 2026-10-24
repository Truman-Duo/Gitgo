import type { ProjectRow } from "./hooks/useGitgoData.js";

export type ProjectRuntimeCategory = "running" | "pending" | "unavailable";

/** Visual groups only; durable per-process lifecycle remains unchanged. */
export function projectRuntimeCategory(project: ProjectRow): ProjectRuntimeCategory {
  if (!project.stateAvailable) return "unavailable";
  if (project.daemonOnline && project.activeProcessCount > 0) return "running";
  return "pending";
}

export function projectRuntimeRank(project: ProjectRow): number {
  return {running: 0, pending: 1, unavailable: 2}[projectRuntimeCategory(project)];
}
