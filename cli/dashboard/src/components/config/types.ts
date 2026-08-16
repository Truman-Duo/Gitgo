// src/components/config/types.ts — shared contracts for ConfigPanel tab modules.
// A tab is a self-contained module: own state + own key resolver + own footer +
// own render. The shell (ConfigPanel.tsx) only owns tab selection + the tab bar.

import type { ComponentType } from "react";
import type { McpClient } from "../../mcp/client.js";
import type { UseTextInputReturn } from "../../hooks/useTextInput.js";
import type { FooterConfig } from "../CommandBar.js";

export type ConfigTabId = "providers" | "bin" | "publish";

/** Navigation + coordination callbacks the shell hands to each tab. */
export type ShellControls = {
  back: () => void;
  goToTab: (id: ConfigTabId) => void;
  tabPrev: () => void;
  tabNext: () => void;
};

/** Sub-tab / fullscreen state a tab reports up to the shell (for the tab bar). */
export type TabReport = {
  sub: boolean;
  fullscreen: boolean;
};

export type ConfigTabProps = {
  client: McpClient;
  project: string;
  cmdInput: UseTextInputReturn;
  onFooter: (cfg: FooterConfig | null) => void;
  onStatusUpdate?: (text: string) => void;
  onRefresh?: () => void;
  report: (r: TabReport) => void;
  shell: ShellControls;
};

export type ConfigTabModule = {
  id: ConfigTabId;
  label: string;
  Component: ComponentType<ConfigTabProps>;
};
