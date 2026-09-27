// src/components/ConfigPanel.tsx — thin /config shell: tab bar + active tab module.
// Each tab (providers/bin/publish) is a self-contained module in ./config/ that
// owns its own state, key resolver, footer, and render. Adding a tab = one file
// + one entry in config/registry.ts; nothing else changes.

import React, { memo, useState, useCallback, useMemo } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import type { UseTextInputReturn } from "../hooks/useTextInput.js";
import type { FooterConfig } from "./CommandBar.js";
import { colors, usePanelSize, separator } from "../theme/index.js";
import { HorizontalHeaderStrip, clampHeaderIndex } from "./HorizontalHeaderStrip.js";
import { CONFIG_TABS } from "./config/registry.js";
import type { ConfigTabId, ShellControls, TabReport } from "./config/types.js";
import { matchChord } from "../input/bindings.js";

type Props = {
  client: BackendClient;
  project: string;
  initialTab?: string;
  cmdInput: UseTextInputReturn;
  onFooter: (cfg: FooterConfig | null) => void;
  onBack: () => void;
  onStatusUpdate?: (text: string) => void;
  onRefresh?: () => void;
};

export const ConfigPanel = memo(function ConfigPanel({
  client, project, initialTab, cmdInput, onFooter, onBack, onStatusUpdate, onRefresh,
}: Props) {
  const requestedTab = initialTab === "bin" ? "general" : initialTab;
  const [tab, setTab] = useState<ConfigTabId>((requestedTab as ConfigTabId) || "general");
  const [tabReport, setTabReport] = useState<TabReport>({ sub: false, fullscreen: false });
  const [contentFocused, setContentFocused] = useState(false);

  const goToTab = useCallback((id: ConfigTabId) => {
    setTabReport({ sub: false, fullscreen: false });
    setContentFocused(false);
    setTab(id);
  }, []);
  const tabPrev = useCallback(() => {
    setTabReport({ sub: false, fullscreen: false });
    setContentFocused(false);
    setTab((t) => {
      const i = CONFIG_TABS.findIndex((x) => x.id === t);
      return CONFIG_TABS[clampHeaderIndex(i, -1, CONFIG_TABS.length)].id;
    });
  }, []);
  const tabNext = useCallback(() => {
    setTabReport({ sub: false, fullscreen: false });
    setContentFocused(false);
    setTab((t) => {
      const i = CONFIG_TABS.findIndex((x) => x.id === t);
      return CONFIG_TABS[clampHeaderIndex(i, 1, CONFIG_TABS.length)].id;
    });
  }, []);
  const report = useCallback((r: TabReport) => setTabReport(r), []);
  const leaveContent = useCallback(() => setContentFocused(false), []);

  useInput((input, key) => {
    if (contentFocused || tabReport.fullscreen) return false;
    if (matchChord("left", input, key)) { tabPrev(); return; }
    if (matchChord("right", input, key)) { tabNext(); return; }
    if (matchChord("enter", input, key)) { setContentFocused(true); return; }
    if (matchChord("escape", input, key)) onBack();
  });

  const shell: ShellControls = useMemo(
    () => ({ back: onBack, goToTab, tabPrev, tabNext, leaveContent }),
    [onBack, goToTab, tabPrev, tabNext, leaveContent],
  );

  const active = CONFIG_TABS.find((t) => t.id === tab) ?? CONFIG_TABS[0];
  const Active = active.Component;

  const { w } = usePanelSize({ minWidth: 40, widthOffset: 4 });

  return (
    <Box flexDirection="column" paddingTop={1} paddingLeft={1} flexGrow={1}>
      {!tabReport.fullscreen && (
        <Box flexDirection="column">
          <HorizontalHeaderStrip
            items={CONFIG_TABS.map(item => ({id: item.id, label: item.label}))}
            selected={Math.max(0, CONFIG_TABS.findIndex(item => item.id === tab))}
            width={w}
            contentFocused={contentFocused}
          />
          <Text color={colors.divider.color}>{separator(w)}</Text>
        </Box>
      )}
      <Box flexDirection="column" flexGrow={1}>
        <Active
          client={client}
          project={project}
          cmdInput={cmdInput}
          onFooter={onFooter}
          onStatusUpdate={onStatusUpdate}
          onRefresh={onRefresh}
          report={report}
          shell={shell}
          contentFocused={contentFocused}
        />
      </Box>
    </Box>
  );
});
