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

/**
 * Parse a unified diff patch string into per-file diffs with hunks.
 * Handles `new file mode` / `deleted file mode`, `@@ -a,b +c,d @@` headers,
 * and `+`/`-`/` ` lines (skipping `\ No newline at end of file`).
 */
export function parseUnifiedDiff(patch: string): FileDiff[] {
  if (!patch) return [];
  const lines = patch.split("\n");
  const files: FileDiff[] = [];

  let i = 0;
  while (i < lines.length) {
    if (!lines[i].startsWith("diff --git ")) { i++; continue; }
    const file = extractPath(lines[i]);

    // Scan file header (index / mode / --- / +++) for status.
    let status: FileDiff["status"] = "modified";
    let j = i + 1;
    while (j < lines.length) {
      const l = lines[j];
      if (l.startsWith("new file mode")) status = "added";
      else if (l.startsWith("deleted file mode")) status = "deleted";
      else if (l.startsWith("--- ") || l.startsWith("+++ ") || l.startsWith("@@ ")) break;
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
          } else if (dl.startsWith(" ") || dl === "") {
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
