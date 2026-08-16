// src/components/TrialPanel.tsx — /runtime trial: incoming external changes + triage
import React, { memo, useState, useEffect, useCallback } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import { trialList, trialTriage } from "../mcp/tools.js";
import { resolveTrialKey } from "../input/overlayKeymaps.js";
import { useSelectionStyle, usePanelSize } from "../theme/index.js";
import { ConfirmBox } from "./ConfirmBox.js";
import { chordLabel } from "../input/bindings.js";

type Incoming = { index: number; hash: string; message: string; author: string; date: string; triage: string };

type Props = {
  client: McpClient;
  project: string;
  cols: number;
  onDismiss: () => void;
};

export const TrialPanel = memo(function TrialPanel({ client, project, onDismiss }: Props) {
  const [items, setItems] = useState<Incoming[]>([]);
  const [sel, setSel] = useState(0);
  const [loading, setLoading] = useState(true);
  const [confirm, setConfirm] = useState<null | { index: number; action: string }>(null);
  const [confirmSel, setConfirmSel] = useState(1);
  const [status, setStatus] = useState("");

  const refresh = useCallback(() => {
    trialList(client, project)
      .then((r: any) => {
        setItems(Array.isArray(r) ? r : r?.incoming || []);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [client, project]);

  useEffect(() => { refresh(); }, [refresh]);

  useInput((input: string, key: any) => {
    for (const a of resolveTrialKey(!!confirm, input, key)) {
      switch (a.type) {
        case "dismiss": onDismiss(); break;
        case "move": setSel((s) => Math.max(0, Math.min(items.length - 1, s + a.delta))); break;
        case "triage": {
          const it = items[sel];
          if (it) { setConfirm({ index: it.index, action: a.action }); setConfirmSel(0); }
          break;
        }
        case "confirmCancel": setConfirm(null); setConfirmSel(1); break;
        case "confirmMove": setConfirmSel(a.index); break;
        case "confirmYes":
          if (confirmSel === 0 && confirm) {
            trialTriage(client, project, confirm.index, confirm.action)
              .then(() => { refresh(); setStatus(`Triaged #${confirm.index} → ${confirm.action}`); })
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
        <ConfirmBox title={`${confirm.action} incoming #${confirm.index}?`} confirmSel={confirmSel} />
      </Box>
    );
  }

  if (loading) return <Box padding={1}><Text dimColor>Loading incoming changes...</Text></Box>;

  return (
    <Box flexDirection="column" padding={1} width={w}>
      <Box marginBottom={1}>
        <Text bold>Trial Incoming: {project}</Text>
        <Text dimColor>    {items.length} changes</Text>
      </Box>
      {status ? <Text dimColor>{status}</Text> : null}

      {items.length === 0 ? (
        <Text dimColor>No incoming changes.</Text>
      ) : (
        items.map((it, i) => {
          const active = i === sel;
          const st = useSelectionStyle(active ? "focused" : "non-focused", "block", "accent");
          return (
            <Box key={it.index} marginBottom={1} flexDirection="column">
              <Text color={st.fg} backgroundColor={st.bg} bold={st.bold}>
                #{it.index}  {it.hash.slice(0, 10)}  [{it.triage}]
              </Text>
              {active && (
                <Box paddingLeft={2}>
                  <Text dimColor>{it.message.slice(0, w - 12)}</Text>
                  <Text dimColor>{it.author || "?"}  {it.date?.slice(0, 16) || ""}</Text>
                </Box>
              )}
            </Box>
          );
        })
      )}

      <Box marginTop={1}>
        <Text dimColor>{chordLabel("letterA")} accept    {chordLabel("letterP")} promote    {chordLabel("letterD")} discard    {chordLabel("upDown")} select    {chordLabel("escape")} back</Text>
      </Box>
    </Box>
  );
});
