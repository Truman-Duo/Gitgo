// src/components/config/registry.ts — single registration point for /config tabs.
// Add/remove/reorder a tab here (plus its one module file) — nothing else changes.

import type { ConfigTabModule } from "./types.js";
import { GeneralTab } from "./GeneralTab.js";
import { ProvidersTab } from "./ProvidersTab.js";
import {KeybindingsTab} from "./KeybindingsTab.js";

export const CONFIG_TABS: ConfigTabModule[] = [
  { id: "general", label: {en: "General", zh: "通用设置"}, Component: GeneralTab },
  { id: "providers", label: {en: "Providers", zh: "模型服务商"}, Component: ProvidersTab, ownsViewport: true },
  { id: "keybindings", label: {en: "Keybindings", zh: "绑键"}, Component: KeybindingsTab, ownsViewport: true },
];
