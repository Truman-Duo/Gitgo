// src/components/config/registry.ts — single registration point for /config tabs.
// Add/remove/reorder a tab here (plus its one module file) — nothing else changes.

import type { ConfigTabModule } from "./types.js";
import { ProvidersTab } from "./ProvidersTab.js";
import { BinTab } from "./BinTab.js";
import { PublishTab } from "./PublishTab.js";

export const CONFIG_TABS: ConfigTabModule[] = [
  { id: "providers", label: "Providers", Component: ProvidersTab },
  { id: "bin", label: "Bin", Component: BinTab },
  { id: "publish", label: "Publish", Component: PublishTab },
];
