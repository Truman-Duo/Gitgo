// src/components/valueTree.ts — render arbitrary backend JSON as indented text lines.
// Shared by governance / memory / trial / formal panels where nested dict shapes vary.

export function valueLines(value: any, indent = 0): string[] {
  const pad = "  ".repeat(indent);
  if (value === null || value === undefined) return [`${pad}—`];
  if (Array.isArray(value)) {
    if (value.length === 0) return [`${pad}[]`];
    const out: string[] = [];
    for (const item of value) out.push(...valueLines(item, indent + 1));
    return out;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length === 0) return [`${pad}{}`];
    const out: string[] = [];
    for (const [k, v] of entries) {
      if (v !== null && typeof v === "object") {
        out.push(`${pad}${k}:`);
        out.push(...valueLines(v, indent + 1));
      } else {
        out.push(`${pad}${k}: ${v ?? "—"}`);
      }
    }
    return out;
  }
  return [`${pad}${String(value)}`];
}
