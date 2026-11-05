import {createRequire} from "node:module";
import {readFileSync} from "node:fs";

/** Build-time only: embed the pinned WASM bytes in Bun and Node bundles alike. */
export function svgWasmBase64() {
  return readFileSync(createRequire(import.meta.url).resolve("@resvg/resvg-wasm/index_bg.wasm")).toString("base64");
}
