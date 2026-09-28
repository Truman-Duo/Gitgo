// src/components/RuntimeMenu.tsx — /runtime secondary menu (LLMConfig-style tab header)
// Tab header: only the active tab gets a background; inactive tabs are transparent.
// (The dim "detail" background appears only when a tab is expanded internally,
//  which this menu does not yet have.)
import React, { memo, useEffect, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import { resolveRuntimeMenuKey } from "../input/overlays/runtimeMenu.js";
import { colors, usePanelSize, separator } from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";
import { HorizontalHeaderStrip, clampHeaderIndex } from "./HorizontalHeaderStrip.js";
import type { BackendClient } from "../backend/client.js";
import type { ProcessInfo } from "../hooks/useLoopData.js";
import { contractShow } from "../backend/tools.js";
import { RUNTIME_TABS } from "./RuntimeShell.js";
import { ContractTab } from "./ContractTab.js";
import { LessonsPanel } from "./LessonsPanel.js";
import { GovernancePanel } from "./GovernancePanel.js";
import { MemoryPanel } from "./MemoryPanel.js";
import { RuntimeContextPanel } from "./RuntimeContextPanel.js";
import { RuntimeToolsPanel } from "./RuntimeToolsPanel.js";
import type { UseTextInputReturn } from "../hooks/useTextInput.js";
import type { FooterConfig } from "./CommandBar.js";

type RuntimeItem = { id: string; label: string };

const ITEMS: RuntimeItem[] = [...RUNTIME_TABS];

type Props = {
  cols: number;
  rows: number;
  client: BackendClient;
  project: string;
  processes: Record<string, ProcessInfo>;
  onSelect: (subCmd: string) => void;
  onDismiss: () => void;
  cmdInput: UseTextInputReturn;
  onFooter: (config: FooterConfig | null) => void;
};

export const RuntimeMenu = memo(function RuntimeMenu({
  cols: _cols, rows: _rows, client, project, processes, onSelect, onDismiss,
  cmdInput, onFooter,
}: Props) {
  const { w } = usePanelSize({ minWidth: 40, widthOffset: 4 });
  const [selIdx, setSelIdx] = useState(0);
  const [contentFocused, setContentFocused] = useState(false);
  const [contract, setContract] = useState<any>(null);
  const [contractError, setContractError] = useState("");

  useEffect(() => {
    const id = ITEMS[selIdx]?.id;
    if (id !== "contract") return;
    let alive = true;
    setContract(null); setContractError("");
    contractShow(client, project).then(value => { if (alive) setContract(value); })
      .catch(reason => { if (alive) setContractError(String(reason?.message || reason)); });
    return () => { alive = false; };
  }, [client, project, selIdx]);
  useInput((input: string, key: any) => {
    if (contentFocused) return false;
    for (const a of resolveRuntimeMenuKey(input, key)) {
      if (a.type === "dismiss") {
        onDismiss();
      } else if (a.type === "move") {
        setSelIdx((s) => clampHeaderIndex(s, a.delta, ITEMS.length));
      } else if (a.type === "confirm") {
        setContentFocused(true);
      }
    }
  }, {isActive: !contentFocused});

  const leaveContent = () => setContentFocused(false);
  const activeId = ITEMS[selIdx]?.id || "lesson";
  const content = activeId === "lesson"
    ? <LessonsPanel client={client} project={project} cols={_cols} onDismiss={leaveContent}
        interactive={contentFocused} cmdInput={cmdInput} onFooter={onFooter}/>
    : activeId === "contract"
      ? <Box flexDirection="column" padding={1}>
          {contractError ? <Text color={colors.danger}>Error: {contractError}</Text>
            : contract === null ? <Text dimColor>Loading…</Text>
            : <ContractTab contract={contract} width={w}/>}
          <Text dimColor>{contentFocused ? `${chordLabel("escape")} tabs` : `${chordLabel("enter")} open`}</Text>
        </Box>
      : activeId === "governance"
        ? <GovernancePanel client={client} project={project} cols={_cols} onDismiss={leaveContent} interactive={contentFocused}/>
        : activeId === "memory"
          ? <MemoryPanel client={client} project={project} cols={_cols} onDismiss={leaveContent} interactive={contentFocused}/>
          : activeId === "context"
            ? <RuntimeContextPanel project={project} processes={processes} onDismiss={leaveContent} interactive={contentFocused}/>
            : <RuntimeToolsPanel client={client} project={project} cols={_cols} onDismiss={leaveContent} interactive={contentFocused}/>;

  return (
    <Box flexDirection="column" paddingTop={1} paddingLeft={1} flexGrow={1}>
      {/* Tab header */}
      <Box flexDirection="column">
        <HorizontalHeaderStrip items={ITEMS} selected={selIdx} width={w} contentFocused={contentFocused} />
        <Text color={colors.divider.color}>{separator(w)}</Text>
      </Box>

      <Box flexDirection="column" flexGrow={1} paddingTop={1}>{content}</Box>
    </Box>
  );
});
