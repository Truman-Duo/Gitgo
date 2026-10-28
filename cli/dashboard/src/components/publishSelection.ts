export type Contribution = {source_role: string; source_ref: string; base_oid?: string};

export function parseContributions(text: string): Contribution[] {
  if (!text.trim()) return [];
  const rows = text.split(";").map(value => value.split("|").map(part => part.trim()));
  if (rows.length > 7 || rows.some(parts => parts.length < 2 || parts.length > 3
    || !["workspace", "trial"].includes(parts[0]) || !parts[1]
    || parts[2] && !/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(parts[2]))) throw new Error("PUBLISH_INVALID_CONTRIBUTIONS");
  return rows.map(([source_role, source_ref, base_oid]) => ({source_role, source_ref, ...(base_oid ? {base_oid} : {})}));
}

export function parseSelection(method: string, commits: string, paths: string) {
  if (method === "cherry_pick") {
    const selected_commits = commits.trim().split(/[;,\s]+/).filter(Boolean);
    if (!selected_commits.length || selected_commits.length > 32 || new Set(selected_commits).size !== selected_commits.length
      || selected_commits.some(oid => !/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(oid))) throw new Error("PUBLISH_INVALID_SELECTION");
    return {selected_commits};
  }
  if (method === "partial") {
    const selected = paths.split(";").map(value => value.trim()).filter(Boolean);
    if (!selected.length || selected.length > 512 || new Set(selected).size !== selected.length
      || selected.some(name => /[\\\x00-\x1f]/.test(name) || name.split("/").some(part => ["", ".", ".."].includes(part))))
      throw new Error("PUBLISH_INVALID_SELECTION");
    return {paths: selected};
  }
  return {};
}
