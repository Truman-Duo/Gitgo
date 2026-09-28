// src/components/config/registry.ts — single registration point for /config tabs.
// Add/remove/reorder a tab here (plus its one module file) — nothing else changes.

import type { ConfigTabModule } from "./types.js";
import { GeneralTab } from "./GeneralTab.js";
import { ProvidersTab } from "./ProvidersTab.js";

export const CONFIG_TABS: ConfigTabModule[] = [
  { id: "general", label: "General", Component: GeneralTab },
  { id: "providers", label: "Providers", Component: ProvidersTab },
];
