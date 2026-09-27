// src/utils/diff.ts — parse `git diff --unified` output into FileDiff[].
import type { FileDiff, DiffHunk } from "../types.js";

/** Extract the target path from a `diff --git a/X b/Y` header. */
function extractPath(header: string): string {
  const b = header.match(/ b\/(.+)$/);
  if (b) return b[1];
  const a = header.match(/ a\/(.+?) b\//);
  if (a) return a[1];
  return header.slice("diff --git ".length);
}

function normalizePatchPath(raw: string): string {
  let value = raw.split("\t", 1)[0]!.trim();
  if (value.startsWith('"') && value.endsWith('"')) {
    try { value = JSON.parse(value); } catch { value = value.slice(1, -1); }
  }
  return value.replace(/^[ab]\//, "");
}

/**
 * Parse a unified diff patch string into per-file diffs with hunks.
 * Handles `new file mode` / `deleted file mode`, `@@ -a,b +c,d @@` headers,
 * and `+`/`-`/` ` lines (skipping `\ No newline at end of file`).
 */
export function parseUnifiedDiff(patch: string): FileDiff[] {
  if (!patch) return [];
  let lines = patch.split("\n");
  // Gitgo's write/edit tools deliberately return a compact unified patch
  // (`---`, `+++`, hunks) rather than shelling out to `git diff`.  That is a
  // valid unified diff, but it has no optional `diff --git` envelope.  The UI
  // used to reject that production format even though tests only covered the
  // git-enveloped variant.  Normalize the compact single-file form at this
  // boundary so every timeline renderer consumes the same FileDiff model.
  if (!lines.some((line) => line.startsWith("diff --git "))) {
    const oldHeader = lines.findIndex((line, index) =>
      line.startsWith("--- ") && lines[index + 1]?.startsWith("+++ "),
    );
    if (oldHeader >= 0) {
      const oldPath = normalizePatchPath(lines[oldHeader]!.slice(4));
      const newPath = normalizePatchPath(lines[oldHeader + 1]!.slice(4));
      const path = newPath === "/dev/null" ? oldPath : newPath;
      lines = [`diff --git a/${path} b/${path}`, ...lines];
    }
  }
  const files: FileDiff[] = [];

  let i = 0;
  while (i < lines.length) {
    if (!lines[i].startsWith("diff --git ")) { i++; continue; }
    let file = extractPath(lines[i]);

    // Scan file header (index / mode / --- / +++) for status.
    let status: FileDiff["status"] = "modified";
    let j = i + 1;
    while (j < lines.length) {
      const l = lines[j];
      if (l.startsWith("new file mode")) status = "added";
      else if (l.startsWith("deleted file mode")) status = "deleted";
      else if (l.startsWith("--- ")) {
        const oldPath = normalizePatchPath(l.slice(4));
        const next = lines[j + 1] || "";
        if (next.startsWith("+++ ")) {
          const newPath = normalizePatchPath(next.slice(4));
          if (oldPath === "/dev/null") status = "added";
          if (newPath === "/dev/null") status = "deleted";
          file = newPath === "/dev/null" ? oldPath : newPath;
        }
        break;
      }
      else if (l.startsWith("+++ ") || l.startsWith("@@ ")) break;
      else if (l.startsWith("diff --git ")) break;
      j++;
    }

    // Parse hunks until the next file header.
    const hunks: DiffHunk[] = [];
    let additions = 0;
    let deletions = 0;
    while (j < lines.length && !lines[j].startsWith("diff --git ")) {
      const m = lines[j].match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
      if (m) {
        const hunk: DiffHunk = {
          oldStart: parseInt(m[1], 10),
          oldLines: m[2] !== undefined ? parseInt(m[2], 10) : 1,
          newStart: parseInt(m[3], 10),
          newLines: m[4] !== undefined ? parseInt(m[4], 10) : 1,
          lines: [],
        };
        j++;
        while (j < lines.length && !lines[j].startsWith("@@ ") && !lines[j].startsWith("diff --git ")) {
          const dl = lines[j];
          if (dl.startsWith("+") && !dl.startsWith("+++")) {
            hunk.lines.push({ type: "add", text: dl.slice(1) });
            additions++;
          } else if (dl.startsWith("-") && !dl.startsWith("---")) {
            hunk.lines.push({ type: "remove", text: dl.slice(1) });
            deletions++;
          } else if (dl.startsWith(" ")) {
            hunk.lines.push({ type: "context", text: dl.slice(1) });
          } else if (dl.startsWith("\\")) {
            // "\ No newline at end of file" — skip
          }
          j++;
        }
        hunks.push(hunk);
      } else {
        j++;
      }
    }

    files.push({ file, additions, deletions, status, hunks });
    i = j;
  }

  return files;
}
