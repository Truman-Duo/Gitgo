import type {Copy} from "../ui/pageContract.js";
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

export function governanceSections(tab: number, data: any, t:(value:Copy)=>string = value=>value.en): GovernanceSection[] {
  if (tab === 0) {
    const sections: GovernanceSection[] = [{title: t({en:"Suggestion quality",zh:"建议质量"}), lines: [
      t({en:count(data.suggestion_count)+" suggestions compared with user decisions.",zh:count(data.suggestion_count)+" 条建议与用户选择对照。"}),
      t({en:"Acceptance describes agreement with those decisions, not proven correctness.",zh:"采纳率表示与用户选择一致，不代表已证明正确。"}),
    ]}];
    for (const [key, title] of [["by_type", t({en:"By workflow",zh:"按工作流"})], ["by_commit_type", t({en:"By commit type",zh:"按提交类型"})], ["by_module", t({en:"By module",zh:"按模块"})]]) {
      sections.push({title: title!, lines: Object.entries(data[key!] || {}).map(([name, raw]) => {
        const value = raw as any;
        return words(name)+" · "+count(value.total)+t({en:" reviewed · ",zh:" 次审阅 · "})+pct(value.acceptance_rate)+t({en:" accepted",zh:" 采纳"})
          + (value.modified !== undefined ? " · "+count(value.modified)+t({en:" modified · ",zh:" 修改 · "})+count(value.rejected)+t({en:" rejected",zh:" 拒绝"}) : "");
      })});
    }
    return sections;
  }
  if (tab === 1) return [
    {title: t({en:"Modules changed together",zh:"共同变更的模块"}), lines: (data.co_changing_modules || []).map((v: any) =>
      (v.modules || []).join(" ↔ ")+" · "+count(v.co_occurrence)+t({en:" co-occurrences / ",zh:" 次共同变更 / "})+count(v.total_formal)+t({en:" formal commits",zh:" 次正式提交"}))},
    {title: t({en:"Commit patterns",zh:"提交模式"}), lines: (data.commit_type_clusters || []).map((v: any) =>
      words(v.type)+" · "+count(v.count)+t({en:" commits · ",zh:" 次提交 · "})+count(v.avg_sources)+t({en:" sources on average · ",zh:" 平均来源数 · "})+pct(v.multi_source_ratio)+t({en:" combined sources",zh:" 合并来源"}))},
    {title: t({en:"Trial follow-up",zh:"试验后续"}), lines: [
      count(data.trial_impact?.total_accepted)+t({en:" accepted trials · ",zh:" 次采纳试验 · "})+count(data.trial_impact?.triggered_workspace_change)+t({en:" followed by workspace changes",zh:" 次后续工作区变更"}),
      data.trial_impact?.total_accepted > 0 ? t({en:"Observed follow-up rate: ",zh:"观察到的后续变更比例："})+pct(data.trial_impact.avg_trigger_rate)+t({en:". Association does not establish causation.",zh:"。相关不证明因果。"}) : t({en:"No accepted trial samples yet.",zh:"尚无已采纳试验样本。"}),
    ]},
  ];
  if (tab === 2) return (data.items || []).map((item: any) => {
    const detail = item.detail || {};
    const summary = [detail.message, detail.reason, detail.summary].map(text).filter(Boolean);
    const process = text(detail.process_id);
    return {title: `${eventTime(item.timestamp)}  ·  ${words(item.operation)}  ·  ${words(item.status)}`,
      lines: [...summary, ...(process ? [t({en:"Process ",zh:"进程 "})+process.slice(0,8)+" · "+words(item.source)] : [])]};
  });
  return (data.releases || []).map((release: any) => ({
    title: eventTime(release.pushed_at)+" · "+(release.commits || []).length+t({en:" commits pushed",zh:" 次推送提交"}),
    lines: [text(release.reason) || t({en:"No release note recorded.",zh:"未记录发布说明。"}), ...(release.commits || []).map((commit: any) =>
      typeof commit === "string" ? commit : text(commit.subject || commit.message || commit.hash) || t({en:"Commit details unrecorded",zh:"未记录提交详情"}))],
  }));
}
