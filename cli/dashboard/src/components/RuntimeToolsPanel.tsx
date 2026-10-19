import React, { memo, useCallback, useEffect, useRef, useState } from "react";
import { Box, ScrollBox, Text, type ScrollBoxHandle } from "@anthropic/ink";
import type { BackendClient } from "../backend/client.js";
import { customToolArchive, customToolList, customToolRestore } from "../backend/tools.js";
import { useManagedInput as useInput, useScrollInput } from "../input/runtime.js";
import { resolveRuntimeToolsKey } from "../input/overlays/runtimeTools.js";
import { chordLabel } from "../input/bindings.js";
import { colors, truncate, usePanelSize, useSelectionStyle } from "../theme/index.js";

type Props = { client: BackendClient; project: string; cols: number; onDismiss: () => void; interactive?: boolean };

export const RuntimeToolsPanel = memo(function RuntimeToolsPanel({ client, project, onDismiss, interactive = true }: Props) {
  const scrollRef = useRef<ScrollBoxHandle>(null);
  useScrollInput(scrollRef, interactive);
  const [items, setItems] = useState<any[]>([]);
  const [selected, setSelected] = useState(0);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState("");
  const { w } = usePanelSize({ minWidth: 20, widthOffset: 4 });

  const refresh = useCallback(() => {
    setLoading(true);
    customToolList(client, project, true)
      .then((result: any) => {
        setItems(Array.isArray(result?.tools) ? result.tools : []);
        setSelected((value) => Math.max(0, Math.min(value, (result?.tools?.length || 1) - 1)));
      })
      .catch((error: any) => setStatus(String(error?.message || error)))
      .finally(() => setLoading(false));
  }, [client, project]);

  useEffect(() => refresh(), [refresh]);

  useInput((input: string, key: any) => {
    for (const action of resolveRuntimeToolsKey(input, key)) {
      if (action.type === "dismiss") onDismiss();
      if (action.type === "move") {
        setSelected((value) => Math.max(0, Math.min(items.length - 1, value + action.delta)));
      }
      if (action.type === "confirm") {
        const item = items[selected];
        if (!item) continue;
        const change = item.state === "archived" ? customToolRestore : customToolArchive;
        change(client, project, item.name)
          .then(() => { setStatus(item.state === "archived" ? "Tool restored" : "Tool archived"); refresh(); })
          .catch((error: any) => setStatus(String(error?.message || error)));
      }
    }
  }, {isActive: interactive});

  const selection = useSelectionStyle(interactive ? "focused" : "non-focused", "block");
  return (
    <Box flexDirection="column" paddingX={1} paddingTop={1} width={w} flexGrow={1} minHeight={0}>
      <Box marginBottom={1}><Text bold>Saved tools — {project}</Text></Box>
      <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1} minHeight={0}>
      {loading ? <Text dimColor>Loading tools...</Text> : null}
      {!loading && items.length === 0 ? <Text dimColor>No saved custom tools.</Text> : null}
      {items.map((item, index) => {
        const active = index === selected;
        const label = `${item.name}  v${item.version}  ${item.state}`;
        return (
          <Box key={item.tool_id || item.name} flexDirection="column">
            <Text color={active ? selection.fg : undefined}
              backgroundColor={active ? selection.bg : undefined}>
              {active ? "› " : "  "}{truncate(label, Math.max(10, w - 4))}
            </Text>
            {active ? <>
              <Text dimColor>  {truncate(item.description || "No description", Math.max(10, w - 4))}</Text>
              <Text dimColor>  {item.authority === "user_approved_privileged" ? "Privileged · exact approval per invocation" : "Pure transform"}{item.effect ? ` · ${item.effect}` : ""}</Text>
            </> : null}
          </Box>
        );
      })}
      </ScrollBox>
      {status ? <Box marginTop={1}><Text color={colors.named.gray}>{status}</Text></Box> : null}
      <Text dimColor>{chordLabel("upDown")} select    {chordLabel("enter")} archive/restore    {chordLabel("escape")} back</Text>
    </Box>
  );
});
