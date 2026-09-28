import React, { memo, useCallback, useEffect, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { BackendClient } from "../backend/client.js";
import { customToolArchive, customToolList, customToolRestore } from "../backend/tools.js";
import { useManagedInput as useInput } from "../input/runtime.js";
import { resolveRuntimeToolsKey } from "../input/overlays/runtimeTools.js";
import { chordLabel } from "../input/bindings.js";
import { colors, truncate, usePanelSize, useSelectionStyle } from "../theme/index.js";

type Props = { client: BackendClient; project: string; cols: number; onDismiss: () => void; interactive?: boolean };

export const RuntimeToolsPanel = memo(function RuntimeToolsPanel({ client, project, onDismiss, interactive = true }: Props) {
  const [items, setItems] = useState<any[]>([]);
  const [selected, setSelected] = useState(0);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState("");
  const { w } = usePanelSize({ minWidth: 44 });

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

  const selection = useSelectionStyle("focused", "block");
  return (
    <Box flexDirection="column" padding={1} width={w}>
      <Box marginBottom={1}><Text bold>Saved tools — {project}</Text></Box>
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
      {status ? <Box marginTop={1}><Text color={colors.named.gray}>{status}</Text></Box> : null}
      <Box flexGrow={1} />
      <Text dimColor>{chordLabel("upDown")} select    {chordLabel("enter")} archive/restore    {chordLabel("escape")} back</Text>
    </Box>
  );
});
