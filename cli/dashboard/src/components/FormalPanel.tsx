// src/components/FormalPanel.tsx — /runtime formal: formal commits + delete/dissolve
import React, { memo, useState, useEffect, useCallback } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import { formalList, formalDelete, formalDissolve } from "../mcp/tools.js";
import { resolveFormalKey } from "../input/overlayKeymaps.js";
import { useSelectionStyle, usePanelSize } from "../theme/index.js";
import { ConfirmBox } from "./ConfirmBox.js";
import { chordLabel } from "../input/bindings.js";

type Formal = {
  index: number; prefix: string; number: number; message: string;
  synced: boolean; pushed: boolean; is_incoming: boolean; created_at: string;
};

type Props = {
  client: McpClient;
  project: string;
  cols: number;
  onDismiss: () => void;
};

export const FormalPanel = memo(function FormalPanel({ client, project, onDismiss }: Props) {
  const [items, setItems] = useState<Formal[]>([]);
  const [sel, setSel] = useState(0);
  const [loading, setLoading] = useState(true);
  const [confirm, setConfirm] = useState<null | { kind: "delete" | "dissolve"; index: number }>(null);
  const [confirmSel, setConfirmSel] = useState(1);
  const [status, setStatus] = useState("");

  const refresh = useCallback(() => {
    formalList(client, project)
      .then((r: any) => {
        setItems(Array.isArray(r) ? r : r?.formal_commits || []);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [client, project]);

  useEffect(() => { refresh(); }, [refresh]);

  useInput((input: string, key: any) => {
    for (const a of resolveFormalKey(!!confirm, input, key)) {
      switch (a.type) {
        case "dismiss": onDismiss(); break;
        case "move": setSel((s) => Math.max(0, Math.min(items.length - 1, s + a.delta))); break;
        case "delete": {
          const it = items[sel];
          if (it) { setConfirm({ kind: "delete", index: it.index }); setConfirmSel(0); }
          break;
        }
        case "dissolve": {
          const it = items[sel];
          if (it) { setConfirm({ kind: "dissolve", index: it.index }); setConfirmSel(0); }
          break;
        }
        case "confirmCancel": setConfirm(null); setConfirmSel(1); break;
        case "confirmMove": setConfirmSel(a.index); break;
        case "confirmYes":
          if (confirmSel === 0 && confirm) {
            const fn = confirm.kind === "delete" ? formalDelete : formalDissolve;
            fn(client, project, confirm.index)
              .then(() => { refresh(); setStatus(`${confirm.kind} #${confirm.index} done`); })
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
        <ConfirmBox
          title={`${confirm.kind === "delete" ? "Delete" : "Dissolve"} formal #${confirm.index}?`}
          danger={confirm.kind === "delete" ? "This removes the formal commit." : "Dissolve restores workspace commits."}
          confirmSel={confirmSel}
        />
      </Box>
    );
  }

  if (loading) return <Box padding={1}><Text dimColor>Loading formal commits...</Text></Box>;

  return (
    <Box flexDirection="column" padding={1} width={w}>
      <Box marginBottom={1}>
        <Text bold>Formal Commits: {project}</Text>
        <Text dimColor>    {items.length} commits</Text>
      </Box>
      {status ? <Text dimColor>{status}</Text> : null}

      {items.length === 0 ? (
        <Text dimColor>No formal commits.</Text>
      ) : (
        items.map((it, i) => {
          const active = i === sel;
          const st = useSelectionStyle(active ? "focused" : "non-focused", "block", "accent");
          const syncLabel = it.pushed ? "pushed" : it.synced ? "synced" : "local";
          return (
            <Box key={it.index} marginBottom={1} flexDirection="column">
              <Text color={st.fg} backgroundColor={st.bg} bold={st.bold}>
                [{it.prefix}-{it.number}]  {syncLabel}
              </Text>
              {active && (
                <Box paddingLeft={2}>
                  <Text dimColor>{it.message.slice(0, w - 12)}</Text>
                </Box>
              )}
            </Box>
          );
        })
      )}

      <Box marginTop={1}>
        <Text dimColor>{chordLabel("letterD")} delete    {chordLabel("letterX")} dissolve    {chordLabel("upDown")} select    {chordLabel("escape")} back</Text>
        <Text dimColor>edit message via /runtime formal edit &lt;index&gt; &lt;message&gt;</Text>
      </Box>
    </Box>
  );
});
