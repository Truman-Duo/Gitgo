import type {PublishCheck} from "../backend/publish.js";

// One compact format is shared by project settings and candidate drafts.
// Optional suffixes keep the usual single-target entry simple.
export function formatChecks(checks: PublishCheck[]): string {
  return checks.map(check => `${check.target} | ${check.seeds.join(",")} | ${check.timeout}`).join("; ");
}

export function parseChecks(raw: string, previous: PublishCheck[] = []): PublishCheck[] {
  if (!raw.trim()) return [];
  const rows = raw.split(";");
  if (rows.length > 8) throw Error("PUBLISH_INVALID_CHECK_POLICY");
  const checks = rows.map(row => {
    const parts = row.split("|").map(part => part.trim());
    const [target, seedText = "42", timeoutText = "30"] = parts;
    const seeds = seedText.split(",").map(value => value.trim());
    if (parts.length > 3 || !target || seeds.some(seed => !/^\d+$/.test(seed)) || !/^\d+$/.test(timeoutText))
      throw Error("PUBLISH_INVALID_CHECK_POLICY");
    const check: PublishCheck = {target, seeds: seeds.map(Number), timeout: Number(timeoutText)};
    if (check.seeds.length > 4 || new Set(check.seeds).size !== check.seeds.length
        || check.seeds.some(seed => seed > 4294967295) || check.timeout < 1 || check.timeout > 120)
      throw Error("PUBLISH_INVALID_CHECK_POLICY");
    const old = previous.find(old => old.target === target && old.timeout === check.timeout
      && JSON.stringify(old.seeds) === JSON.stringify(check.seeds));
    return old ? {...check, id: old.id} : check;
  });
  if (checks.reduce((total, check) => total + check.timeout * check.seeds.length, 0) > 180)
    throw Error("PUBLISH_CHECK_BUDGET_EXCEEDED");
  return checks;
}
