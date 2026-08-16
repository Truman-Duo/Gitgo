// src/components/MemoryPanel.tsx — /runtime memory: list/snapshot/restore tool memories
import React, { memo, useState, useEffect, useCallback } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import { memoryList, memorySnapshot, memoryRestore } from "../mcp/tools.js";
import { resolveMemoryKey } from "../input/overlays/memory.js";
import { useSelectionStyle, usePanelSize } from "../theme/index.js";
import { ConfirmBox } from "./ConfirmBox.js";
import { chordLabel } from "../input/bindings.js";

type Snapshot = { source: string; timestamp: string; path: string; is_dir: boolean };

type Props = {
  client: McpClient;
  project: string;
  cols: number;
  onDismiss: () => void;
};

export const MemoryPanel = memo(function MemoryPanel({ client, project, onDismiss }: Props) {
  const [snaps, setSnaps] = useState<Snapshot[]>([]);
  const [sel, setSel] = useState(0);
  const [loading, setLoading] = useState(true);
  const [confirm, setConfirm] = useState<null | { ts: string }>(null);
  const [confirmSel, setConfirmSel] = useState(1);
  const [status, setStatus] = useState("");

  const refresh = useCallback(() => {
    memoryList(client, project)
      .then((r: any) => {
        setSnaps(Array.isArray(r) ? r : r?.snapshots || []);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [client, project]);

  useEffect(() => { refresh(); }, [refresh]);

  useInput((input: string, key: any) => {
    for (const a of resolveMemoryKey(!!confirm, input, key)) {
      switch (a.type) {
        case "dismiss": onDismiss(); break;
        case "move": setSel((s) => Math.max(0, Math.min(snaps.length - 1, s + a.delta))); break;
        case "snapshot":
          setStatus("Snapshotting...");
          memorySnapshot(client, project)
            .then(() => { refresh(); setStatus("Snapshot created"); })
            .catch((e: any) => setStatus(String(e.message || e)));
          break;
        case "restore": {
          const sn = snaps[sel];
          if (sn) { setConfirm({ ts: sn.timestamp }); setConfirmSel(0); }
          break;
        }
        case "confirmCancel": setConfirm(null); setConfirmSel(1); break;
        case "confirmMove": setConfirmSel(a.index); break;
        case "confirmYes":
          if (confirmSel === 0 && confirm) {
            memoryRestore(client, project, confirm.ts)
              .then(() => { refresh(); setStatus("Restored"); })
              .catch((e: any) => setStatus(String(e.message || e)));
          }
          setConfirm(null); setConfirmSel(1);
          break;
      }
    }
  });

  const { w } = usePanelSize({ minWidth: 40 });

  if (confirm) {
    return (
      <Box width={w}>
        <ConfirmBox title={`Restore snapshot ${confirm.ts.slice(0, 19)}?`} confirmSel={confirmSel} />
      </Box>
    );
  }

  if (loading) return <Box padding={1}><Text dimColor>Loading snapshots...</Text></Box>;

  return (
    <Box flexDirection="column" padding={1} width={w}>
      <Box marginBottom={1}>
        <Text bold>Memory Snapshots: {project}</Text>
        <Text dimColor>    {snaps.length} snapshots</Text>
      </Box>
      {status ? <Text dimColor>{status}</Text> : null}

      {snaps.length === 0 ? (
        <Text dimColor>No snapshots yet. Press S to create one.</Text>
      ) : (
        snaps.map((sn, i) => {
          const active = i === sel;
          const st = useSelectionStyle(active ? "focused" : "non-focused", "block", "accent");
          return (
            <Box key={sn.timestamp + sn.source} marginBottom={1}>
              <Text color={st.fg} backgroundColor={st.bg} bold={st.bold}>
                {sn.source}  {sn.timestamp.slice(0, 19)}
              </Text>
            </Box>
          );
        })
      )}

      <Box marginTop={1}>
        <Text dimColor>{chordLabel("letterS")}  snapshot    {chordLabel("letterR")}  restore selected    {chordLabel("upDown")} select    {chordLabel("escape")} back</Text>
      </Box>
    </Box>
  );
});
