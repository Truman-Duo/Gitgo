import { execFileSync, spawn } from "node:child_process";
import { existsSync, readFileSync, readdirSync, writeFileSync, mkdtempSync, unlinkSync, rmdirSync } from "node:fs";
import { randomUUID, createHash } from "node:crypto";
import { homedir, tmpdir } from "node:os";
import { basename, dirname, join, resolve, isAbsolute } from "node:path";

export type TerminalMode = "auto" | "current" | "windows_terminal" | "custom" |
  "wezterm" | "alacritty" | "conemu" | "mintty" | "git_bash" | "git_bash_console" | "gnome_terminal" | "konsole" | "xterm";

export type ExecutableIdentity = {verified: boolean; files: {path: string; sha256: string}[];
  directories?: {path: string; files: string[]}[]};
export type TerminalOption = {id: string; label: string; available: boolean; command: string; args: string[]; identity?: ExecutableIdentity};
export type TerminalInventory = {
  platform: string; options: TerminalOption[]; selected: string; configured: boolean;
  effective: TerminalOption; warnings: string[];
};

export function launcherFromInventory(inventory: TerminalInventory): TerminalLauncherConfig {
  return {terminal: inventory.effective.id as TerminalMode, command: inventory.effective.command,
    args: inventory.effective.args, identity: inventory.effective.identity};
}

export type TerminalLauncherConfig = {
  terminal: TerminalMode;
  command: string;
  args: string[];
  identity?: ExecutableIdentity;
};

export function assertInteractiveTerminal(input: {isTTY?: boolean}, output: {isTTY?: boolean}): void {
  if (!input.isTTY || !output.isTTY) {
    throw new Error("TERMINAL_NOT_INTERACTIVE: child has no interactive terminal; the original window remains available");
  }
}

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
    const terminal = ["auto", "current", "windows_terminal", "custom", "wezterm", "alacritty", "conemu", "mintty", "git_bash", "git_bash_console", "gnome_terminal", "konsole", "xterm"].includes(launcher.terminal)
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
  // A packaged launch uses the saved preference from Explorer or an existing
  // shell alike. --attached is the explicit override and handoff loop guard.
  return true;
}

export function currentFrontendOrigin(environment = process.env, platform = process.platform) {
  if (environment.GITGO_FRONTEND_ORIGIN) return environment.GITGO_FRONTEND_ORIGIN;
  const terminal = environment.WT_SESSION ? "windows_terminal"
    : environment.MSYSTEM ? "git_bash_console"
    : platform === "win32" ? "windows_console" : environment.TERM_PROGRAM || "current";
  return JSON.stringify({version: 1, terminal, platform, source: "environment_detection"});
}

/** A handoff is allowed only after the original backend has released its owner. */
export async function transferTerminalOwner(
  client: {close(): Promise<void> | void; start(): Promise<void>},
  launch: () => Promise<{launched: boolean; message: string}>,
) {
  await client.close();
  let result: {launched: boolean; message: string};
  try {
    result = await launch();
  } catch (error) {
    await client.start();
    throw error;
  }
  if (!result.launched) await client.start();
  return result;
}

export function terminalChildCommand(
  config: TerminalLauncherConfig,
  executable: string,
  forwardedArgs: string[],
  windowsTerminalPath = "wt.exe",
): {command: string; args: string[]} | null {
  const args = forwardedArgs.filter((item, index) => item !== "--attached" &&
    item !== "--smoke-input-b64" && forwardedArgs[index - 1] !== "--smoke-input-b64");
  // In source mode Bun requires the entry point before Gitgo's flags.
  const source = /\.(?:tsx?|m?js)$/.test(args[0] || "") ? args.shift() : undefined;
  const childArgs = [executable, ...(source ? [source] : []), "--attached", ...args];
  if (config.terminal === "git_bash") {
    // MSYS accepts forward-slash Windows paths. Keep argument vectors intact;
    // the Bash program only execs positional parameters, never interpolated text.
    childArgs[0] = executable.replace(/\\/g, "/");
    return {command: config.command, args: ["--dir", process.cwd(), ...config.args, ...childArgs]};
  }
  if (!["auto", "current", "windows_terminal"].includes(config.terminal)) {
    if (!config.command) return null;
    return {command: config.command, args: [...config.args, ...childArgs]};
  }
  if (config.terminal === "auto" || config.terminal === "windows_terminal") {
    return {command: config.command || windowsTerminalPath,
      args: [...(config.args.length ? config.args : ["new-tab", "--"]), ...childArgs]};
  }
  return null;
}

const HANDOFF_ENV = ["GITGO_CONFIG_PATH", "GITGO_STATE_HOME", "GITGO_LLM_CONFIG_PATH",
  "GITGO_LAUNCH_SESSION", "GITGO_LAUNCH_SESSION_TOKEN",
  "GITGO_FRONTEND_ORIGIN",
  "GITGO_LLM_SECRET_PATH", "GITGO_PYTHON", "GITGO_BUN", "GITGO_HOST_EXECUTABLE",
  "GITGO_INSTALL_ROOT", "GITGO_RIPGREP_PATH", "GITGO_NO_COLOR", "FORCE_COLOR", "NO_COLOR", "TERM"];

function terminalEnvironment(config: TerminalLauncherConfig): NodeJS.ProcessEnv {
  if (!["git_bash", "git_bash_console"].includes(config.terminal)) return process.env;
  const environment: NodeJS.ProcessEnv = {...process.env, MSYS2_ARG_CONV_EXCL: "*"};
  if (config.terminal === "git_bash") {
    environment.MSYS = config.args.includes("on") ? "enable_pcon" : "disable_pcon";
  }
  for (const key of Object.keys(environment)) {
    if (["BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "LD_PRELOAD", "LD_LIBRARY_PATH"].includes(key.toUpperCase()) ||
        key.toUpperCase().startsWith("BASH_FUNC_")) delete environment[key];
  }
  return environment;
}

/** Validate the parent's one-use launch ticket before applying its environment. */
export function acceptTerminalHandoff(argv = process.argv): (() => void) | undefined {
  const index = argv.indexOf("--launcher-handoff");
  if (index < 0) return;
  const path = resolve(argv[index + 1] || "");
  const token = argv[argv.indexOf("--launcher-handoff-token") + 1] || "";
  if (basename(path) !== "handoff.json" || !basename(dirname(path)).startsWith("gitgo-terminal-")
      || dirname(dirname(path)) !== resolve(tmpdir()) || !/^[\da-f-]{36}$/i.test(token)) {
    throw new Error("Invalid terminal handoff ticket");
  }
  const ticket = JSON.parse(readFileSync(path, "utf8"));
  if (ticket.token !== token || ticket.status !== "pending" || Date.now() > ticket.expiresAt) {
    throw new Error("Terminal handoff expired; the original window remains available");
  }
  for (const key of HANDOFF_ENV) {
    if (typeof ticket.environment?.[key] === "string") process.env[key] = ticket.environment[key];
    else delete process.env[key];
  }
  return () => {
    if (Date.now() > ticket.expiresAt) throw new Error("Terminal handoff expired before Dashboard was ready");
    writeFileSync(path, JSON.stringify({...ticket, status: "ready"}), "utf8");
  };
}

export async function relaunchInConfiguredTerminal(
  config: TerminalLauncherConfig,
  executable = process.execPath,
  forwardedArgs = process.argv.slice(1),
  options: {timeoutMs?: number} = {},
): Promise<{launched: boolean; message: string}> {
  const child = terminalChildCommand(config, executable, forwardedArgs);
  if (!child) return {launched: false, message: "Continuing in the current terminal."};
  try {
    // The Host resolves candidates. Never search cwd/PATH again at launch.
    const command = isAbsolute(child.command) && existsSync(child.command) ? child.command : "";
    if (!command) return {launched: false, message: "Selected terminal is unavailable; continuing here. Choose another in /config → General."};
    if (config.identity) {
      if (!config.identity.verified || !config.identity.files.length ||
          !config.identity.files.some(file => resolve(file.path).toLowerCase() === resolve(command).toLowerCase()) ||
          config.identity.files.some(file => createHash("sha256").update(readFileSync(file.path)).digest("hex") !== file.sha256) ||
          config.identity.directories?.some(directory => JSON.stringify(readdirSync(directory.path)
            .filter(name => name.toLowerCase().endsWith(".dll")).sort()) !== JSON.stringify([...directory.files].sort()))) {
        return {launched: false, message: "TERMINAL_IDENTITY_CHANGED: terminal files changed after verification; continuing here. Rescan in /config → General."};
      }
    } else if (["git_bash", "git_bash_console"].includes(config.terminal)) {
      return {launched: false, message: "TERMINAL_IDENTITY_UNVERIFIED: Git Bash has no verified identity; continuing here. Rescan in /config → General."};
    }
    const directory = mkdtempSync(join(tmpdir(), "gitgo-terminal-"));
    const path = join(directory, "handoff.json");
    const token = randomUUID();
    const timeoutMs = Math.max(1, Math.min(options.timeoutMs ?? 20000, 20000));
    const environment = Object.fromEntries(HANDOFF_ENV.filter(k => process.env[k] !== undefined).map(k => [k, process.env[k]]));
    environment.GITGO_FRONTEND_ORIGIN = JSON.stringify({version: 1, terminal: config.terminal,
      platform: process.platform, source: "terminal_handoff"});
    try {
      writeFileSync(path, JSON.stringify({token, expiresAt: Date.now() + timeoutMs, status: "pending", environment}), {flag: "wx"});
      child.args.push("--launcher-handoff", path, "--launcher-handoff-token", token);
      return await new Promise((resolve) => {
      let settled = false;
      let timer: ReturnType<typeof setInterval> | undefined;
      const deadline = Date.now() + timeoutMs;
      const finish = (launched: boolean, message: string) => {
        if (settled) return;
        settled = true;
        if (timer) clearInterval(timer);
        resolve({launched, message});
      };
      const proc = spawn(command, child.args, {
        detached: true, stdio: "ignore", windowsHide: false,
        cwd: ["git_bash", "git_bash_console"].includes(config.terminal) ? dirname(command) : process.cwd(),
        env: terminalEnvironment(config),
      });
      proc.once("error", () => finish(false, "Terminal could not start; continuing here. Choose another in /config → General."));
      proc.once("exit", code => {
        if (code !== 0) finish(false, `Terminal exited (${code}); continuing here.`);
      });
      proc.once("spawn", () => {
        // Keep the original UI alive until the child Host and full renderer are
        // ready. A terminal process may exit normally after forwarding to its
        // existing server, so exit(0) alone is not readiness evidence.
        timer = setInterval(() => {
          try {
            const ticket = JSON.parse(readFileSync(path, "utf8"));
            if (ticket.token === token && ticket.status === "ready") {
              finish(true, "Selected terminal is ready."); return;
            }
          } catch { /* Retry only until the fixed launch deadline. */ }
          if (Date.now() >= deadline) finish(false, "Terminal did not become ready; continuing here. Change it in /config → General.");
        }, 100);
        proc.unref();
      });
    }); } finally {
      // Remove only the two exact files/directories created by this launch.
      try { unlinkSync(path); rmdirSync(directory); } catch { /* Expired tickets confer no launch authority. */ }
    }
  } catch {
    return {launched: false, message: "Terminal launch failed; continuing here. Choose another in /config → General."};
  }
}
