// src/components/DiffView.tsx — side-by-side (split) diff renderer.
// Falls back to unified view when the panel is too narrow.
// Column widths, ellipsis truncation, and line-number alignment live in
// theme/diffLayout.ts so they are shared by every diff renderer.
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import type { FileDiff, DiffHunk, DiffLine } from "../types.js";
import {
  colors,
  usePanelSize,
  diffAvail,
  diffLineNo,
  diffCell,
  unifiedColWidth,
  splitColWidth,
} from "../theme/index.js";

type Row = {
  oldLine: number | null;
  oldText: string | null;
  newLine: number | null;
  newText: string | null;
  type: "change" | "remove" | "add" | "context";
};

function alignHunk(hunk: DiffHunk): Row[] {
  const rows: Row[] = [];
  let oldNo = hunk.oldStart;
  let newNo = hunk.newStart;
  const ls = hunk.lines;
  let i = 0;
  while (i < ls.length) {
    const l = ls[i];
    if (l.type === "remove") {
      if (i + 1 < ls.length && ls[i + 1].type === "add") {
        rows.push({
          oldLine: oldNo++, oldText: l.text,
          newLine: newNo++, newText: ls[i + 1].text,
          type: "change",
        });
        i += 2;
      } else {
        rows.push({ oldLine: oldNo++, oldText: l.text, newLine: null, newText: null, type: "remove" });
        i++;
      }
    } else if (l.type === "add") {
      rows.push({ oldLine: null, oldText: null, newLine: newNo++, newText: l.text, type: "add" });
      i++;
    } else {
      rows.push({ oldLine: oldNo++, oldText: l.text, newLine: newNo++, newText: l.text, type: "context" });
      i++;
    }
  }
  return rows;
}

type CellStyle = { fg?: string; bg?: string };

function oldCellStyle(type: Row["type"]): CellStyle {
  if (type === "remove" || type === "change") return { fg: colors.diff.removed, bg: colors.diff.removedBg };
  return {};
}
function newCellStyle(type: Row["type"]): CellStyle {
  if (type === "add" || type === "change") return { fg: colors.diff.added, bg: colors.diff.addedBg };
  return {};
}

function statusColor(status: FileDiff["status"]): string {
  return status === "added" ? colors.diff.added : status === "deleted" ? colors.diff.removed : colors.named.gray;
}

function FileHeader({ file }: { file: FileDiff }) {
  return (
    <Box flexDirection="row" gap={1}>
      <Text bold color={statusColor(file.status)}>{file.file}</Text>
      <Text bold>{file.status}</Text>
      <Text bold color={colors.diff.added}>+{file.additions}</Text>
      <Text bold color={colors.diff.removed}>-{file.deletions}</Text>
    </Box>
  );
}

// One side of a split row: [num][gap][cell]. The cell Box owns the background and
// the fixed width, so sign + content render as ONE continuous block.
function SideLine({ lineNo, text, sign, style, digits, colW }: {
  lineNo: number | null;
  text: string | null;
  sign: string;
  style: CellStyle;
  digits: number;
  colW: number;
}) {
  const num = diffLineNo(lineNo, digits);
  const has = text != null;
  const content = diffCell(sign, text, colW);
  return (
    <>
      <Box width={digits}>
        <Text color={colors.diff.lineNumber}>{num}</Text>
      </Box>
      <Box width={colW} backgroundColor={has ? style.bg : undefined} flexShrink={0}>
        <Text color={has ? style.fg : undefined}>{content}</Text>
      </Box>
    </>
  );
}

function SplitFile({ file, avail }: { file: FileDiff; avail: number }) {
  const rows: Row[] = file.hunks.flatMap(alignHunk);
  const maxLine = rows.reduce((m, r) => Math.max(m, r.oldLine ?? 0, r.newLine ?? 0), 0);
  const digits = String(maxLine).length;
  const colW = splitColWidth(avail, digits);

  return (
    <Box flexDirection="column">
      <FileHeader file={file} />
      {rows.map((r, idx) => {
        const oldSign = r.type === "remove" || r.type === "change" ? "-" : " ";
        const newSign = r.type === "add" || r.type === "change" ? "+" : " ";
        return (
          <Box key={idx} flexDirection="row" gap={1}>
            <SideLine lineNo={r.oldLine} text={r.oldText} sign={oldSign} style={oldCellStyle(r.type)} digits={digits} colW={colW} />
            <Text dimColor>│</Text>
            <SideLine lineNo={r.newLine} text={r.newText} sign={newSign} style={newCellStyle(r.type)} digits={digits} colW={colW} />
          </Box>
        );
      })}
    </Box>
  );
}

function UnifiedFile({ file, avail }: { file: FileDiff; avail: number }) {
  let maxLine = 0;
  for (const h of file.hunks) {
    let o = h.oldStart, n = h.newStart;
    for (const l of h.lines) {
      if (l.type !== "add") { maxLine = Math.max(maxLine, o); o++; }
      if (l.type !== "remove") { maxLine = Math.max(maxLine, n); n++; }
    }
  }
  const digits = String(maxLine).length;
  const colW = unifiedColWidth(avail, digits);

  return (
    <Box flexDirection="column">
      <FileHeader file={file} />
      {file.hunks.map((h, hi) => {
        let o = h.oldStart, n = h.newStart;
        return h.lines.map((l: DiffLine, li) => {
          const fg = l.type === "add" ? colors.diff.added : l.type === "remove" ? colors.diff.removed : undefined;
          const bg = l.type === "add" ? colors.diff.addedBg : l.type === "remove" ? colors.diff.removedBg : undefined;
          let num: string, sign: string;
          if (l.type === "remove") { num = String(o).padStart(digits); sign = "-"; o++; }
          else if (l.type === "add") { num = String(n).padStart(digits); sign = "+"; n++; }
          else { num = String(o).padStart(digits); sign = " "; o++; n++; }
          const content = diffCell(sign, l.text, colW);
          return (
            <Box key={`${hi}-${li}`} flexDirection="row" gap={1}>
              <Text color={colors.diff.lineNumber}>{num}</Text>
              <Box width={colW} backgroundColor={bg} flexShrink={0}>
                <Text color={fg}>{content}</Text>
              </Box>
            </Box>
          );
        });
      })}
    </Box>
  );
}

export const DiffView = memo(function DiffView({ files }: { files: FileDiff[] }) {
  const { w } = usePanelSize({ minWidth: 30 });
  const avail = diffAvail(w);
  const split = w >= 60;
  return (
    <Box flexDirection="column" marginTop={1}
      borderStyle="single" borderColor={colors.diff.frame} paddingLeft={1}>
      {files.map((f, i) => (
        <Box key={i} flexDirection="column" marginTop={i === 0 ? 0 : 1}>
          {split && f.status === "modified" ? <SplitFile file={f} avail={avail} /> : <UnifiedFile file={f} avail={avail} />}
        </Box>
      ))}
    </Box>
  );
});
