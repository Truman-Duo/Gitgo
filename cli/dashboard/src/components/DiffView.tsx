// src/components/DiffView.tsx — side-by-side (split) diff renderer.
// Falls back to unified view when the panel is too narrow.
// Column widths, ellipsis truncation, and line-number alignment live in
// theme/diffLayout.ts so they are shared by every diff renderer.
import React, { memo } from "react";
import { Ansi, Box, Text } from "@anthropic/ink";
import type { Color } from "@anthropic/ink";
import { highlight, supportsLanguage } from "cli-highlight";
import type { FileDiff, DiffHunk, DiffLine } from "../types.js";
import {
  colors,
  usePanelSize,
  diffAvail,
  diffLineNo,
  unifiedColWidth,
  splitColWidth,
  canUseSplitDiff,
  truncate,
} from "../theme/index.js";

type Row = {
  oldLine: number | null;
  oldText: string | null;
  newLine: number | null;
  newText: string | null;
  type: "change" | "remove" | "add" | "context";
};

export function alignHunk(hunk: DiffHunk): Row[] {
  const rows: Row[] = [];
  let oldNo = hunk.oldStart;
  let newNo = hunk.newStart;
  const ls = hunk.lines;
  let i = 0;
  while (i < ls.length) {
    const l = ls[i];
    if (l.type === "remove") {
      const removed: DiffLine[] = [];
      while (i < ls.length && ls[i]!.type === "remove") removed.push(ls[i++]!);
      const added: DiffLine[] = [];
      while (i < ls.length && ls[i]!.type === "add") added.push(ls[i++]!);
      const count = Math.max(removed.length, added.length);
      for (let offset = 0; offset < count; offset++) {
        const old = removed[offset];
        const next = added[offset];
        rows.push({
          oldLine: old ? oldNo++ : null,
          oldText: old?.text ?? null,
          newLine: next ? newNo++ : null,
          newText: next?.text ?? null,
          type: old && next ? "change" : old ? "remove" : "add",
        });
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

type CellStyle = { fg?: Color; bg?: Color };

const LANGUAGE_BY_EXTENSION: Record<string, string> = {
  c: "c", cc: "cpp", cpp: "cpp", cxx: "cpp", h: "c", hpp: "cpp",
  cs: "csharp", css: "css", go: "go", html: "html", htm: "html",
  java: "java", js: "javascript", jsx: "javascript", json: "json",
  md: "markdown", py: "python", rs: "rust", sh: "bash", sql: "sql",
  ts: "typescript", tsx: "typescript", xml: "xml", yaml: "yaml", yml: "yaml",
};

function languageForPath(path: string): string | undefined {
  const extension = path.split(/[\\/]/).pop()?.split(".").pop()?.toLowerCase() || "";
  const language = LANGUAGE_BY_EXTENSION[extension];
  return language && supportsLanguage(language) ? language : undefined;
}

function CodeCell({sign, text, width, style, language}: {
  sign: string; text: string | null; width: number; style: CellStyle;
  language?: string;
}) {
  if (text == null) return <Text color={style.fg}>{sign === " " ? "" : sign}</Text>;
  const source = truncate(text, Math.max(1, width - 2));
  let rendered = source;
  if (language) {
    try { rendered = highlight(source, {language, ignoreIllegals: true}); } catch { /* plain fallback */ }
  }
  return <><Text color={style.fg}>{sign} </Text>{language ? <Ansi>{rendered}</Ansi> : <Text color={style.fg}>{source}</Text>}</>;
}

function oldCellStyle(type: Row["type"]): CellStyle {
  if (type === "remove" || type === "change") return { fg: colors.diff.removed, bg: colors.diff.removedBg };
  return {};
}
function newCellStyle(type: Row["type"]): CellStyle {
  if (type === "add" || type === "change") return { fg: colors.diff.added, bg: colors.diff.addedBg };
  return {};
}

function statusColor(status: FileDiff["status"]): Color {
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
function SideLine({ lineNo, text, sign, style, digits, colW, language }: {
  lineNo: number | null;
  text: string | null;
  sign: string;
  style: CellStyle;
  digits: number;
  colW: number;
  language?: string;
}) {
  const num = diffLineNo(lineNo, digits);
  const has = text != null;
  return (
    <>
      <Box width={digits} flexShrink={0}>
        <Text color={colors.diff.lineNumber}>{num}</Text>
      </Box>
      <Box width={colW} backgroundColor={has ? style.bg : undefined} flexShrink={0}>
        <CodeCell sign={sign} text={text} width={colW} style={has ? style : {}} language={language}/>
      </Box>
    </>
  );
}

function SplitFile({ file, avail }: { file: FileDiff; avail: number }) {
  const rows: Row[] = file.hunks.flatMap(alignHunk);
  const maxLine = rows.reduce((m, r) => Math.max(m, r.oldLine ?? 0, r.newLine ?? 0), 0);
  const digits = String(maxLine).length;
  const colW = splitColWidth(avail, digits);
  const language = languageForPath(file.file);

  return (
    <Box flexDirection="column">
      <FileHeader file={file} />
      {rows.map((r, idx) => {
        const oldSign = r.type === "remove" || r.type === "change" ? "-" : " ";
        const newSign = r.type === "add" || r.type === "change" ? "+" : " ";
        return (
          <Box key={idx} flexDirection="row" gap={1}>
            <SideLine lineNo={r.oldLine} text={r.oldText} sign={oldSign} style={oldCellStyle(r.type)} digits={digits} colW={colW} language={language} />
            <Text dimColor>│</Text>
            <SideLine lineNo={r.newLine} text={r.newText} sign={newSign} style={newCellStyle(r.type)} digits={digits} colW={colW} language={language} />
          </Box>
        );
      })}
    </Box>
  );
}

function maxLineDigits(file: FileDiff): number {
  let maxLine = 0;
  for (const h of file.hunks) {
    let o = h.oldStart, n = h.newStart;
    for (const l of h.lines) {
      if (l.type !== "add") { maxLine = Math.max(maxLine, o); o++; }
      if (l.type !== "remove") { maxLine = Math.max(maxLine, n); n++; }
    }
  }
  return Math.max(1, String(maxLine).length);
}

function UnifiedFile({ file, avail }: { file: FileDiff; avail: number }) {
  const digits = maxLineDigits(file);
  const colW = unifiedColWidth(avail, digits);
  const language = languageForPath(file.file);

  return (
    <Box flexDirection="column">
      <FileHeader file={file} />
      {file.hunks.map((h, hi) => {
        let o = h.oldStart, n = h.newStart;
        return h.lines.map((l: DiffLine, li) => {
          const fg = l.type === "add" ? colors.diff.added : l.type === "remove" ? colors.diff.removed : undefined;
          const bg = l.type === "add" ? colors.diff.addedBg : l.type === "remove" ? colors.diff.removedBg : undefined;
          let oldLine: number | null = null, newLine: number | null = null, sign: string;
          if (l.type === "remove") { oldLine = o++; sign = "-"; }
          else if (l.type === "add") { newLine = n++; sign = "+"; }
          else { oldLine = o++; newLine = n++; sign = " "; }
          return (
            <Box key={`${hi}-${li}`} flexDirection="row" gap={1}>
              <Box width={digits} flexShrink={0}><Text color={colors.diff.lineNumber}>{diffLineNo(oldLine, digits)}</Text></Box>
              <Box width={digits} flexShrink={0}><Text color={colors.diff.lineNumber}>{diffLineNo(newLine, digits)}</Text></Box>
              <Box width={colW} backgroundColor={bg} flexShrink={0}>
                <CodeCell sign={sign} text={l.text} width={colW} style={{fg, bg}} language={language}/>
              </Box>
            </Box>
          );
        });
      })}
    </Box>
  );
}

export const DiffView = memo(function DiffView({ files, width }: { files: FileDiff[]; width?: number }) {
  const { w: terminalWidth } = usePanelSize({ minWidth: 30 });
  const panelWidth = Math.max(30, Math.min(terminalWidth, width ?? terminalWidth));
  const avail = diffAvail(panelWidth);
  return (
    <Box flexDirection="column" marginTop={1}
      borderStyle="single" borderColor={colors.diff.frame} paddingLeft={1}>
      {files.map((f, i) => (
        <Box key={i} flexDirection="column" marginTop={i === 0 ? 0 : 1}>
          {f.status === "modified" && canUseSplitDiff(avail, maxLineDigits(f))
            ? <SplitFile file={f} avail={avail} />
            : <UnifiedFile file={f} avail={avail} />}
        </Box>
      ))}
    </Box>
  );
});
