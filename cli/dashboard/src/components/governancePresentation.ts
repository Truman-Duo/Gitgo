/** Readable projections of canonical reports, without manufacturing missing metrics. */
export type GovernanceSection = {title: string; lines: string[]};
const count = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "—";
const pct = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(0)}%` : "—";
const words = (value: unknown) => String(value || "Unknown event").replace(/_/g, " ");
const text = (value: unknown) => typeof value === "string" ? value.replace(/[\r\n\t]+/g, " ") : "";
export function eventTime(value: unknown): string {
  const date = new Date(String(value || ""));
  return Number.isNaN(date.getTime()) ? "Time unrecorded" : date.toLocaleString();
}

export function governanceSections(tab: number, data: any): GovernanceSection[] {
  if (tab === 0) {
    const sections: GovernanceSection[] = [{title: "Suggestion quality", lines: [
      `${count(data.suggestion_count)} suggestions compared with user decisions.`,
      "Acceptance describes agreement with those decisions, not proven correctness.",
    ]}];
    for (const [key, title] of [["by_type", "By workflow"], ["by_commit_type", "By commit type"], ["by_module", "By module"]]) {
      sections.push({title: title!, lines: Object.entries(data[key!] || {}).map(([name, raw]) => {
        const value = raw as any;
        return `${words(name)}  ·  ${count(value.total)} reviewed  ·  ${pct(value.acceptance_rate)} accepted`
          + (value.modified !== undefined ? `  ·  ${count(value.modified)} modified  ·  ${count(value.rejected)} rejected` : "");
      })});
    }
    return sections;
  }
  if (tab === 1) return [
    {title: "Modules changed together", lines: (data.co_changing_modules || []).map((v: any) =>
      `${(v.modules || []).join(" ↔ ")}  ·  ${count(v.co_occurrence)} co-occurrences / ${count(v.total_formal)} formal commits`)},
    {title: "Commit patterns", lines: (data.commit_type_clusters || []).map((v: any) =>
      `${words(v.type)}  ·  ${count(v.count)} commits  ·  ${count(v.avg_sources)} sources on average  ·  ${pct(v.multi_source_ratio)} combined sources`)},
    {title: "Trial follow-up", lines: [
      `${count(data.trial_impact?.total_accepted)} accepted trials  ·  ${count(data.trial_impact?.triggered_workspace_change)} followed by workspace changes`,
      data.trial_impact?.total_accepted > 0 ? `Observed follow-up rate: ${pct(data.trial_impact.avg_trigger_rate)}. Association does not establish causation.` : "No accepted trial samples yet.",
    ]},
  ];
  if (tab === 2) return (data.items || []).map((item: any) => {
    const detail = item.detail || {};
    const summary = [detail.message, detail.reason, detail.summary].map(text).filter(Boolean);
    const process = text(detail.process_id);
    return {title: `${eventTime(item.timestamp)}  ·  ${words(item.operation)}  ·  ${words(item.status)}`,
      lines: [...summary, ...(process ? [`Process ${process.slice(0, 8)}  ·  ${words(item.source)}`] : [])]};
  });
  return (data.releases || []).map((release: any) => ({
    title: `${eventTime(release.pushed_at)}  ·  ${(release.commits || []).length} commits pushed`,
    lines: [text(release.reason) || "No release note recorded.", ...(release.commits || []).map((commit: any) =>
      typeof commit === "string" ? commit : text(commit.subject || commit.message || commit.hash) || "Commit details unrecorded")],
  }));
}
