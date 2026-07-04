// src/components/ContextPanel.tsx — Scene 2 right pane: 4 tabs
import React, { memo, useState, useCallback } from "react";
import { Box, Text } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import type { ProcessInfo, ToolEvent } from "../hooks/useLoopData.js";
import { ProcessTree } from "./ProcessTree.js";
import { ContractTab } from "./ContractTab.js";
import { LessonsTab } from "./LessonsTab.js";
import { EventsTab } from "./EventsTab.js";

type Props = {
  project: string;
  client: McpClient;
  cols: number;
  processes: Record<string, ProcessInfo>;
  toolEvents: ToolEvent[];
  processSel: number;
  onProcessSelect: (processId: string) => void;
  processFocus: boolean;
};

const TABS = ["Process", "Contract", "Lessons", "Events"] as const;

export const ContextPanel = memo(function ContextPanel({
  project, client, cols, processes, toolEvents,
  processSel, onProcessSelect, processFocus,
}: Props) {
  const [activeTab, setActiveTab] = useState<number>(0);
  const [contract, setContract] = useState<any>(null);
  const [lessons, setLessons] = useState<any>(null);
  const [events, setEvents] = useState<any[]>([]);

  const loadTab = useCallback(async (tab: number) => {
    if (tab === 1 && !contract) {
      try { const c = await client.callTool("gitgo_contract_show", { project }); setContract(c); } catch {}
    }
    if (tab === 2 && !lessons) {
      try { const l = await client.callTool("gitgo_lesson_list", { project }); setLessons(l); } catch {}
    }
    if (tab === 3 && events.length === 0) {
      try { const h: any = await client.callTool("gitgo_history", { project }); setEvents(h?.entries || []); } catch {}
    }
  }, [project, client, contract, lessons, events.length]);

  const switchTab = (tab: number) => { setActiveTab(tab); loadTab(tab); };

  const width = Math.max(24, cols);

  return (
    <Box flexDirection="column" width={width} paddingLeft={1}>
      <Box flexDirection="row" marginBottom={1}>
        {TABS.map((label, i) => (
          <Box key={label} marginRight={1}>
            <Text
              color={i === activeTab ? "cyan" : undefined}
              bold={i === activeTab}
              dimColor={i !== activeTab}
            >[{label}]</Text>
          </Box>
        ))}
      </Box>

      <Box flexDirection="column">
        {activeTab === 0 && (
          <ProcessTree processes={processes} sel={processSel} onSelect={onProcessSelect} focus={processFocus} />
        )}
        {activeTab === 1 && <ContractTab contract={contract} width={width} />}
        {activeTab === 2 && <LessonsTab lessons={lessons} width={width} />}
        {activeTab === 3 && <EventsTab events={events} toolEvents={toolEvents} width={width} />}
      </Box>
    </Box>
  );
});
