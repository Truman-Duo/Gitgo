import type { BackendClient } from "./client.js";

export type ProductVersion = { product_id: "gitgo-terminal"; version: string };
export type UpdateStatus = ProductVersion & {
  schema_version: 1; status: "disabled"; enabled: false; automatic_check: false;
  channel: "stable"; can_apply: false;
  blockers: string[];
};

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

export async function updateStatus(client: BackendClient): Promise<UpdateStatus> {
  const value = await client.callTool("update.status");
  const product = identity(value);
  // Fail closed if a newer Host changes the capability contract. This panel
  // must not accidentally present a successful update or expose an apply action.
  if (value.schema_version !== 1 || value.status !== "disabled" || value.enabled !== false
      || value.automatic_check !== false || value.can_apply !== false || value.channel !== "stable"
      || !Array.isArray(value.blockers) || value.blockers.length > 16
      || value.blockers.some((v: unknown) => typeof v !== "string" || v.length < 1 || v.length > 64 || /[^A-Z_]/.test(v))) {
    throw new Error("Update status unavailable");
  }
  return {...product, schema_version: 1, status: "disabled", enabled: false,
    automatic_check: false, channel: "stable", can_apply: false, blockers: [...value.blockers]};
}
