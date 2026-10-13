import type { BackendClient } from "./client.js";

export type ProductVersion = { product_id: "gitgo-terminal"; version: string };
type LegacyUpdateStatus = ProductVersion & {
  schema_version: 1; status: "disabled"; enabled: false; automatic_check: false;
  channel: "stable"; can_apply: false;
  blockers: string[];
};
export type PreparationStatus = ProductVersion & {
  schema_version: 2 | 3; status: "unconfigured" | "idle" | "available" | "up_to_date" | "downloaded" | "stale";
  enabled: false; automatic_check: false; channel: "stable" | "preview"; can_apply: false;
  can_check: boolean; can_download: boolean; blockers: string[]; latest_version: string | null;
  can_request_apply: boolean;
  candidate: null | {candidate_id: string; version: string; release_id: string; bytes: number; channel: "stable" | "preview"};
};
export type UpdateStatus = LegacyUpdateStatus | PreparationStatus;

function identity(value: any): ProductVersion {
  // Bound terminal text and reject control sequences instead of displaying
  // arbitrary protocol content or guessing a fallback frontend version.
  if (value?.product_id !== "gitgo-terminal" || typeof value?.version !== "string"
      || value.version.length > 128
      || /[^\x20-\x7e]/.test(value.version)) {
    throw new Error("Product version unavailable");
  }
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/.exec(value.version);
  if (!match || match[4]?.split(".").some(part => /^0\d+$/.test(part))) {
    throw new Error("Product version unavailable");
  }
  return {product_id: value.product_id, version: value.version};
}

export async function productVersion(client: BackendClient): Promise<ProductVersion> {
  return identity(await client.callTool("product.version"));
}

export function parseUpdateStatus(value: any): UpdateStatus {
  const product = identity(value);
  // Fail closed if a newer Host changes the capability contract. This panel
  // must not accidentally present a successful update or expose an apply action.
  if (value.enabled !== false || value.automatic_check !== false || value.can_apply !== false
      || !Array.isArray(value.blockers) || value.blockers.length > 16
      || value.blockers.some((v: unknown) => typeof v !== "string" || v.length < 1 || v.length > 64 || /[^A-Z_]/.test(v))) {
    throw new Error("Update status unavailable");
  }
  if (value.schema_version === 1 && value.status === "disabled" && value.channel === "stable") {
    return {...product, schema_version: 1, status: "disabled", enabled: false,
      automatic_check: false, channel: "stable", can_apply: false, blockers: [...value.blockers]};
  }
  if (![2, 3].includes(value.schema_version) || !["stable", "preview"].includes(value.channel)
      || !["unconfigured", "idle", "available", "up_to_date", "downloaded", "stale"].includes(value.status)
      || typeof value.can_check !== "boolean" || typeof value.can_download !== "boolean") throw new Error("Update status unavailable");
  if (value.schema_version === 3 && (typeof value.can_request_apply !== "boolean"
      || (value.can_request_apply && value.status !== "downloaded"))) throw new Error("Update handoff unavailable");
  let candidate: PreparationStatus["candidate"] = null;
  if (value.candidate !== null) {
    const item = value.candidate;
    identity({product_id: product.product_id, version: item?.version});
    if (typeof item.candidate_id !== "string" || item.candidate_id.length !== 64 || !/^[0-9a-f]{64}$/.test(item.candidate_id)
        || typeof item.release_id !== "string" || item.release_id.length > 128
        || /[^\x20-\x7e]/.test(item.release_id) || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(item.release_id)
        || !Number.isSafeInteger(item.bytes) || item.bytes < 1 || item.bytes > 2 * 1024 ** 3
        || item.channel !== value.channel) throw new Error("Update candidate unavailable");
    candidate = {candidate_id: item.candidate_id, version: item.version,
      release_id: item.release_id, bytes: item.bytes, channel: item.channel};
  }
  if (value.latest_version !== null) identity({product_id: product.product_id, version: value.latest_version});
  const selected = ["available", "downloaded"].includes(value.status);
  if (selected !== Boolean(candidate) || (candidate && candidate.version !== value.latest_version)
      || value.can_download !== (value.status === "available")
      || value.can_check !== (value.status !== "unconfigured")
      || (!selected && value.status !== "up_to_date" && value.latest_version !== null)
      || (value.status === "up_to_date" && value.latest_version === null)) throw new Error("Update capability mismatch");
  return {...product, schema_version: value.schema_version, status: value.status, enabled: false, automatic_check: false,
    channel: value.channel, can_apply: false, can_check: value.can_check, can_download: value.can_download,
    can_request_apply: value.schema_version === 3 && value.can_request_apply,
    blockers: [...value.blockers], latest_version: value.latest_version, candidate};
}

export async function updateStatus(client: BackendClient, channel: "stable" | "preview" = "stable"): Promise<UpdateStatus> {
  const result = parseUpdateStatus(await (channel === "stable" ? client.callTool("update.status") : client.callTool("update.status", {channel})));
  if (result.schema_version !== 1 && result.channel !== channel) throw new Error("Update channel mismatch");
  return result;
}

export async function checkUpdate(client: BackendClient, channel: "stable" | "preview", operationId: string): Promise<UpdateStatus> {
  const result = parseUpdateStatus(await client.callTool("update.check", {channel, operation_id: operationId}, 180));
  if (result.schema_version === 1 || result.channel !== channel) throw new Error("Update channel mismatch");
  return result;
}

export async function downloadUpdate(client: BackendClient, channel: "stable" | "preview", candidateId: string, operationId: string): Promise<UpdateStatus> {
  const result = parseUpdateStatus(await client.callTool("update.download", {channel, candidate_id: candidateId, operation_id: operationId}, 1200));
  if (result.schema_version === 1 || result.channel !== channel || result.status !== "downloaded"
      || result.candidate?.candidate_id !== candidateId) throw new Error("Prepared release mismatch");
  return result;
}

export async function cancelUpdate(client: BackendClient, operationId: string): Promise<void> {
  const result = await client.callTool("update.cancel", {operation_id: operationId}, 10);
  if (result?.cancelled !== true) throw new Error("Update cancellation unavailable");
}

export async function requestApplyUpdate(client: BackendClient, channel: "stable" | "preview", candidateId: string, operationId: string): Promise<void> {
  const result = await client.callTool("update.request_apply", {channel, candidate_id: candidateId, operation_id: operationId}, 180);
  if (result?.schema_version !== 1 || result.status !== "exit_required" || result.exit_code !== 75
      || result.channel !== channel || result.candidate_id !== candidateId) throw new Error("Update handoff mismatch");
}
