// src/utils/clipboard.ts — 平台剪贴板读取（Ctrl+V 粘贴）。
import { execFile } from "node:child_process";
import { promisify } from "node:util";
const execute = promisify(execFile);
export const WINDOWS_CLIPBOARD_COMMAND = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; [Console]::Write([string](Get-Clipboard -Raw))";

export async function readClipboard(): Promise<string> {
  let text = "";
  try {
    const windows = process.platform === "win32";
    const result = await execute(windows ? "powershell" : process.platform === "darwin" ? "pbpaste" : "xclip",
      // PowerShell's output formatter appends a line terminator. Write the
      // clipboard value directly so that only actual clipboard newlines remain.
      windows ? ["-NoProfile", "-NonInteractive", "-Command", WINDOWS_CLIPBOARD_COMMAND]
        : process.platform === "darwin" ? [] : ["-o", "-selection", "clipboard"],
      { encoding: "utf-8", timeout: 3000, maxBuffer: 4 * 1024 * 1024, windowsHide: true });
    text = result.stdout
      .replace(/\r\n/g, "\n")
      .replace(/\r/g, "\n");
  } catch (_) {}
  return text;
}
