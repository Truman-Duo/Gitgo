import type { ProjectRow } from "./hooks/useGitgoData.js";

export type ProjectRuntimeCategory = "running" | "pending" | "unavailable" | "finished" | "new";

/** One canonical projection shared by polling order and the visible groups. */
export function projectRuntimeCategory(project: ProjectRow): ProjectRuntimeCategory {
  if (!project.stateAvailable) return "unavailable";
  if (project.daemonOnline && project.activeProcessCount > 0) return "running";
  if (project.waitingProcessCount > 0 || (!project.daemonOnline && project.activeProcessCount > 0)) return "pending";
  if (Number(project.finishedProcessCount || 0) > 0) return "finished";
  return "new";
}

export function projectRuntimeRank(project: ProjectRow): number {
  return { running: 0, pending: 1, unavailable: 2, finished: 3, new: 4 }[
    projectRuntimeCategory(project)
  ];
}
