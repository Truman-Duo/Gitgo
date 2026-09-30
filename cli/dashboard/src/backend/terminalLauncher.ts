import { execFileSync, spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

export type TerminalMode = "auto" | "current" | "windows_terminal" | "custom";

export type TerminalLauncherConfig = {
  terminal: TerminalMode;
  command: string;
  args: string[];
};

/** Make Windows console input bytes deterministic before Bun/Ink reads stdin. */
export function ensureWindowsUtf8Console(platform: NodeJS.Platform = process.platform): void {
  if (platform !== "win32") return;
  try {
    execFileSync("chcp.com", ["65001"], {
      encoding: "utf8", windowsHide: true, timeout: 2500,
      stdio: ["ignore", "ignore", "ignore"],
    });
  } catch {
    // The strict prompt digest/Host validation still fails closed if a custom
    // terminal does not expose chcp (for example an SSH pseudo-terminal).
  }
}

const DEFAULT_CONFIG: TerminalLauncherConfig = {
  terminal: "auto",
  command: "",
  args: [],
};

export function globalConfigPath(
  environment: NodeJS.ProcessEnv = process.env,
  userHome = homedir(),
): string {
  const explicit = String(environment.GITGO_CONFIG_PATH || "").trim();
  return explicit ? resolve(explicit) : join(userHome, ".gitgo", "config.json");
}

export function loadTerminalLauncherConfig(path = globalConfigPath()): TerminalLauncherConfig {
  if (!existsSync(path)) return {...DEFAULT_CONFIG};
  try {
    const raw = JSON.parse(readFileSync(path, "utf8").replace(/^\uFEFF/, ""));
    const launcher = raw?.launcher && typeof raw.launcher === "object" ? raw.launcher : {};
    const terminal = ["auto", "current", "windows_terminal", "custom"].includes(launcher.terminal)
      ? launcher.terminal as TerminalMode
      : "auto";
    return {
      terminal,
      command: typeof launcher.command === "string" ? launcher.command.trim() : "",
      args: Array.isArray(launcher.args)
        ? launcher.args.filter((item: unknown): item is string => typeof item === "string")
        : [],
    };
  } catch {
    // The Native Host reports malformed JSON as a structured config_error.
    // Falling back here keeps a directly-launched console usable for repair.
    return {...DEFAULT_CONFIG};
  }
}

export function isShellLauncherProcess(name: string): boolean {
  const normalized = name.trim().replace(/\.exe$/i, "").toLowerCase();
  return new Set([
    "explorer", "startmenuexperiencehost", "searchhost", "searchapp",
  ]).has(normalized);
}

export function windowsParentProcessName(parentPid = process.ppid): string {
  if (process.platform !== "win32" || !Number.isInteger(parentPid) || parentPid <= 0) return "";
  try {
    return execFileSync(
      "powershell.exe",
      [
        "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
        `(Get-Process -Id ${parentPid} -ErrorAction Stop).ProcessName`,
      ],
      {encoding: "utf8", windowsHide: true, timeout: 2500},
    ).trim();
  } catch {
    return "";
  }
}

export function shouldRelaunchInConfiguredTerminal(options: {
  compiled: boolean;
  platform: NodeJS.Platform;
  argv: string[];
  parentProcessName: string;
  config: TerminalLauncherConfig;
}): boolean {
  if (!options.compiled || options.platform !== "win32") return false;
  if (options.argv.includes("--attached") || options.argv.includes("--mock")) return false;
  if (options.config.terminal === "current") return false;
  return isShellLauncherProcess(options.parentProcessName);
}

export function terminalChildCommand(
  config: TerminalLauncherConfig,
  executable: string,
  forwardedArgs: string[],
  windowsTerminalPath = "wt.exe",
): {command: string; args: string[]} | null {
  const childArgs = [
    executable,
    "--attached",
    ...forwardedArgs.filter((item) => item !== "--attached"),
  ];
  if (config.terminal === "custom") {
    if (!config.command) return null;
    return {command: config.command, args: [...config.args, ...childArgs]};
  }
  if (config.terminal === "auto" || config.terminal === "windows_terminal") {
    return {command: windowsTerminalPath, args: ["new-tab", "--", ...childArgs]};
  }
  return null;
}

export function relaunchInConfiguredTerminal(
  config: TerminalLauncherConfig,
  executable = process.execPath,
  forwardedArgs = process.argv.slice(1),
): boolean {
  const child = terminalChildCommand(config, executable, forwardedArgs);
  if (!child) return false;
  try {
    const command = (() => {
      if (child.command.includes("\\") || child.command.includes("/")) {
        return existsSync(child.command) ? child.command : "";
      }
      try {
        return execFileSync("where.exe", [child.command], {
          encoding: "utf8", windowsHide: true, timeout: 2500,
        }).split(/\r?\n/, 1)[0]?.trim() || "";
      } catch {
        return "";
      }
    })();
    if (!command) return false;
    const proc = spawn(command, child.args, {
      detached: true,
      stdio: "ignore",
      windowsHide: false,
      cwd: process.cwd(),
      env: process.env,
    });
    proc.unref();
    return true;
  } catch {
    return false;
  }
}
