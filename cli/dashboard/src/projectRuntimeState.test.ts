import { describe, expect, test } from "bun:test";
import { projectRuntimeCategory } from "./projectRuntimeState.js";

const row = (overrides: Record<string, unknown> = {}) => ({
  name: "demo", workspace: "C:/demo", backup: "", commit_prefix: "",
  pendingLessons: 0, features: 0, constraints: 0, techStack: "-",
  daemonOnline: false, stateAvailable: true, stateError: "",
  activeProcessCount: 0, waitingProcessCount: 0, finishedProcessCount: 0,
  durableStatus: "", governanceStatus: "Unknown", llmProviderSummary: "",
  llmStatus: "offline", lessonsSeverity: "none", ...overrides,
} as any);

describe("project runtime category", () => {
  test("distinguishes a new project from a finished project", () => {
    expect(projectRuntimeCategory(row())).toBe("new");
    expect(projectRuntimeCategory(row({ finishedProcessCount: 1 }))).toBe("finished");
  });

  test("never disguises an unavailable projection as new", () => {
    expect(projectRuntimeCategory(row({ stateAvailable: false, stateError: "storage unavailable" }))).toBe("unavailable");
  });
});
