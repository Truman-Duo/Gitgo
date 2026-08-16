// src/theme/useSortAlpha.ts — Alphabetical sort for all page lists.
// Always-on: project list, command suggestions, hierarchical command children,
// LLM providers — any list that renders in display order calls sortByName().

export function sortByName<T extends { name?: string; label?: string; title?: string; slashName?: string }>(
  items: T[],
): T[] {
  return [...items].sort((a, b) => {
    const an = (a.name ?? a.label ?? a.title ?? a.slashName ?? "").toLowerCase();
    const bn = (b.name ?? b.label ?? b.title ?? b.slashName ?? "").toLowerCase();
    return an < bn ? -1 : an > bn ? 1 : 0;
  });
}
