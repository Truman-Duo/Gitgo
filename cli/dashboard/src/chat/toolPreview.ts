import stripAnsi from "strip-ansi";
import type { ToolCallCard } from "../types.js";
import { truncate } from "../theme/index.js";

/** Bounded view of facts already supplied by the tool, never inferred execution output. */
export function toolPreview(tool: ToolCallCard, width: number, maxLines = 5) {
  const lines: Array<{ text: string; kind: "plain" }> = [];
  const plain = (value: string) =>
    stripAnsi(value)
      .replace(/\r\n/g, "\n")
      .replace(/[\x00-\x08\x0b-\x1f\x7f]/g, "");
  const targetLines = tool.target ? plain(tool.target).split("\n") : [];
  for (const line of targetLines.slice(0, 2))
    lines.push({ text: line, kind: "plain" });
  if (tool.result_text)
    for (const line of plain(tool.result_text).split("\n"))
      lines.push({ text: line, kind: "plain" });
  return {
    lines: lines.slice(0, maxLines).map((line) => ({
      ...line,
      text: truncate(line.text, Math.max(8, width)),
    })),
    more: targetLines.length > 2 || lines.length > maxLines,
  };
}
