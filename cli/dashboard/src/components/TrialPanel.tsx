import {SelectionText} from "./Selection.js";
import {useCopy} from "../ui/PageFrame.js";
import {applicationBindings} from "../input/router.js";
// src/components/TrialPanel.tsx — /runtime trial: incoming external changes + triage
import React, { memo, useState, useEffect, useCallback } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { trialList, trialTriage } from "../backend/tools.js";
import { resolveTrialKey } from "../input/overlays/trial.js";
import { colors, usePanelSize } from "../theme/index.js";
import { ConfirmBox } from "./ConfirmBox.js";
import { chordLabel } from "../input/bindings.js";

type Incoming = { index: number; hash: string; message: string; author: string; date: string; triage: string };

type Props = {
  client: BackendClient;
  project: string;
  cols: number;
  onDismiss: () => void;
};

export const TrialPanel = memo(function TrialPanel({ client, project, onDismiss }: Props) {
  const t=useCopy();
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
  }, {bindingContext: confirm ? "" : "trial"});

  const { w } = usePanelSize({ minWidth: 40 });

  if (confirm) {
    return (
      <Box width={w}>
        <ConfirmBox title={`${confirm.action} incoming #${confirm.index}?`} confirmSel={confirmSel} />
      </Box>
    );
  }

  if (loading) return <Box padding={1}><Text dimColor>{t({en:"Loading incoming changes...",zh:"正在读取传入变更…"})}</Text></Box>;

  return (
    <Box flexDirection="column" padding={1} width={w}>
      <Box marginBottom={1}>
        <Text bold>{t({en:"Trial Incoming: ",zh:"试验传入变更： "})}{project}</Text>
        <Text dimColor>    {items.length} {t({en:" changes",zh:" 项变更"})}</Text>
      </Box>
      {status ? <Text dimColor>{status}</Text> : null}

      {items.length === 0 ? (
        <Text dimColor>{t({en:"No incoming changes.",zh:"尚无传入变更。"})}</Text>
      ) : (
        items.map((it, i) => {
          const active = i === sel;
          return (
            <Box key={it.index} flexDirection="row">
              <SelectionText selected={active}  >
                #{it.index}  {it.hash.slice(0, 10)}  [{it.triage}]
              </SelectionText>
              {active && (
                <Text dimColor>  {it.message.slice(0, Math.max(10, w - 48))}  {it.author || "?"}</Text>
              )}
            </Box>
          );
        })
      )}

      <Box marginTop={1}>
        <Text dimColor>{applicationBindings.label("trial.accept")} {t({en:" accept    ",zh:" 接受    "})}{applicationBindings.label("trial.promote")} {t({en:" promote    ",zh:" 提升    "})}{applicationBindings.label("trial.discard")} {t({en:" discard    ",zh:" 丢弃    "})}{chordLabel("upDown")} {t({en:" select    ",zh:" 选择    "})}{chordLabel("escape")} {t({en:" back",zh:" 返回"})}</Text>
      </Box>
    </Box>
  );
});
