import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/** Shared by the product dashboard and diagnostic entry points. */
export function resolvePythonRuntime(): string {
  if (process.env.GITGO_PYTHON) return process.env.GITGO_PYTHON;
  if (process.platform !== "win32") return "python3";
  const local = process.env.LOCALAPPDATA || "";
  const candidates = [
    join(homedir(), ".gitgo", "runtime", "python", "python.exe"),
    join(local, "Gitgo", "runtime", "python", "python.exe"),
    join(local, "Programs", "Python", "Python312", "python.exe"),
  ];
  // The backend checks the loaded SQLite version before opening any database.
  return candidates.find(existsSync) || "python";
}
