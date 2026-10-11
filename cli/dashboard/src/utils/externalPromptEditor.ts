import { existsSync } from "node:fs";
import { mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { spawn } from "node:child_process";
import { instances } from "@anthropic/ink";

function splitCommand(command: string): string[] {
  return Array.from(command.matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g), match => match[1] ?? match[2] ?? match[3] ?? "")
    .filter(Boolean);
}

function addWaitArgument(command: string[]): string[] {
  const name = basename(command[0] || "").toLowerCase();
  if (["code.exe", "cursor.exe", "codium.exe", "windsurf.exe"].includes(name)
      && !command.includes("--wait")) return [command[0]!, "--wait", ...command.slice(1)];
  return command;
}

function knownCodeExecutable(): string | null {
  const candidates = [
    process.env.LOCALAPPDATA && join(process.env.LOCALAPPDATA, "Programs", "Microsoft VS Code", "Code.exe"),
    process.env.ProgramFiles && join(process.env.ProgramFiles, "Microsoft VS Code", "Code.exe"),
  ].filter((value): value is string => Boolean(value));
  return candidates.find(existsSync) || null;
}

function editorCommand(configuredEditor = ""): string[] {
  if (configuredEditor.trim()) return addWaitArgument([configuredEditor.trim()]);
  const configured = process.env.VISUAL?.trim() || process.env.EDITOR?.trim();
  if (configured) {
    const parsed = splitCommand(configured);
    const program = basename(parsed[0] || "").toLowerCase();
    if (process.platform === "win32" && ["code", "code.cmd"].includes(program)) {
      const executable = knownCodeExecutable();
      if (executable) return addWaitArgument([executable, ...parsed.slice(1)]);
    }
    if (process.platform !== "win32" || (parsed[0]?.toLowerCase().endsWith(".exe") && existsSync(parsed[0]))) {
      return addWaitArgument(parsed);
    }
  }
  return process.platform === "win32" ? ["notepad.exe"] : ["vi"];
}

async function cleanupStaleDrafts(): Promise<void> {
  const root = tmpdir();
  const now = Date.now();
  for (const name of await readdir(root).catch(() => [] as string[])) {
    if (!name.startsWith("gitgo-prompt-")) continue;
    const path = join(root, name);
    const info = await stat(path).catch(() => null);
    if (info && now - info.mtimeMs > 24 * 60 * 60 * 1000) {
      await rm(path, {recursive: true, force: true}).catch(() => undefined);
    }
  }
}

export async function editPromptExternally(content: string, configuredEditor = ""): Promise<string> {
  await cleanupStaleDrafts();
  const directory = await mkdtemp(join(tmpdir(), "gitgo-prompt-"));
  const path = join(directory, "prompt.md");
  await writeFile(path, content, "utf8");
  const [program, ...args] = editorCommand(configuredEditor);
  if (!program) throw new Error("No external editor is configured");
  const ink = instances.get(process.stdout);
  let imported = false;
  let diagnostic = "";
  ink?.pause();
  ink?.suspendStdin();
  try {
    await new Promise<void>((resolve, reject) => {
      // Never inherit stdout/stderr: Electron/Code startup diagnostics would
      // paint directly over Ink's alternate screen and corrupt the dashboard.
      const child = spawn(program, [...args, path], {
        stdio: ["ignore", "pipe", "pipe"], windowsHide: false, shell: false,
      });
      const capture = (chunk: Buffer | string) => {
        diagnostic = (diagnostic + String(chunk)).slice(-4000);
      };
      child.stdout?.on("data", capture);
      child.stderr?.on("data", capture);
      child.once("error", reject);
      child.once("exit", code => code === 0
        ? resolve()
        : reject(new Error(`Editor exited with code ${code}`)));
    });
    // Windows editors commonly persist CRLF.  The terminal editor, cursor
    // mapper and Ink renderer use LF as their single logical line boundary;
    // normalize at the adapter boundary so imported drafts behave exactly
    // like text entered in the bar.
    const edited = (await readFile(path, "utf8")).replace(/\r\n?/g, "\n");
    imported = true;
    return edited;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const detail = diagnostic.trim() ? `; editor output: ${diagnostic.trim()}` : "";
    throw new Error(`${message}${detail}. Draft preserved at ${path}`);
  } finally {
    if (imported) {
      await rm(directory, {recursive: true, force: true}).catch(() => undefined);
    }
    // Reset and render as one resume operation. Rendering first and then
    // blanking the frame cache leaves the physical screen and cached nodes
    // disagreeing until resize; alternate-screen output can also scroll.
    try { ink?.resume({redraw: true}); }
    finally { ink?.resumeStdin(); }
  }
}
