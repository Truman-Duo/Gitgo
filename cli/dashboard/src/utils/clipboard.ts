// src/utils/clipboard.ts — 平台剪贴板读取（Ctrl+V 粘贴）。
import { execSync } from "node:child_process";

export function readClipboard(): string {
  let text = "";
  try {
    text = execSync(
      process.platform === "win32"
        ? "powershell -NoProfile -Command Get-Clipboard"
        : process.platform === "darwin"
        ? "pbpaste"
        : "xclip -o -selection clipboard",
      { encoding: "utf-8" }
    )
      .replace(/\r\n/g, "\n")
      .replace(/\r/g, "\n");
  } catch (_) {}
  return text;
}
