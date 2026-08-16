// src/components/ConfigPanel.tsx — thin /config shell: tab bar + active tab module.
// Each tab (providers/bin/publish) is a self-contained module in ./config/ that
// owns its own state, key resolver, footer, and render. Adding a tab = one file
// + one entry in config/registry.ts; nothing else changes.

import React, { memo, useState, useCallback, useMemo } from "react";
import { Box, Text } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import type { UseTextInputReturn } from "../hooks/useTextInput.js";
import type { FooterConfig } from "./CommandBar.js";
import { colors, usePanelSize, separator } from "../theme/index.js";
import { CONFIG_TABS } from "./config/registry.js";
import type { ConfigTabId, ShellControls, TabReport } from "./config/types.js";

type Props = {
  client: McpClient;
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
  const [tab, setTab] = useState<ConfigTabId>((initialTab as ConfigTabId) || "providers");
  const [tabReport, setTabReport] = useState<TabReport>({ sub: false, fullscreen: false });

  const goToTab = useCallback((id: ConfigTabId) => {
    setTabReport({ sub: false, fullscreen: false });
    setTab(id);
  }, []);
  const tabPrev = useCallback(() => {
    setTabReport({ sub: false, fullscreen: false });
    setTab((t) => {
      const i = CONFIG_TABS.findIndex((x) => x.id === t);
      return CONFIG_TABS[(i + CONFIG_TABS.length - 1) % CONFIG_TABS.length].id;
    });
  }, []);
  const tabNext = useCallback(() => {
    setTabReport({ sub: false, fullscreen: false });
    setTab((t) => {
      const i = CONFIG_TABS.findIndex((x) => x.id === t);
      return CONFIG_TABS[(i + 1) % CONFIG_TABS.length].id;
    });
  }, []);
  const report = useCallback((r: TabReport) => setTabReport(r), []);

  const shell: ShellControls = useMemo(
    () => ({ back: onBack, goToTab, tabPrev, tabNext }),
    [onBack, goToTab, tabPrev, tabNext],
  );

  const active = CONFIG_TABS.find((t) => t.id === tab) ?? CONFIG_TABS[0];
  const Active = active.Component;

  const { w } = usePanelSize({ minWidth: 40, widthOffset: 4 });

  return (
    <Box flexDirection="column" paddingTop={1} paddingLeft={1} flexGrow={1}>
      {!tabReport.fullscreen && (
        <Box flexDirection="column">
          <Box flexDirection="row" justifyContent="space-evenly">
            {CONFIG_TABS.map((t) => {
              const isActive = t.id === tab;
              const sub = isActive && tabReport.sub;
              const bg = isActive ? (sub ? colors.tab.detail.bg : colors.tab.active.bg) : undefined;
              const fg = isActive ? (sub ? colors.tab.detail.fg : colors.tab.active.fg) : colors.tab.detail.fg;
              return (
                <Box key={t.id} backgroundColor={bg} paddingLeft={1} paddingRight={1}>
                  <Text color={fg} backgroundColor={bg} bold={isActive && !sub}>
                    {t.label}
                  </Text>
                </Box>
              );
            })}
          </Box>
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
        />
      </Box>
    </Box>
  );
});
