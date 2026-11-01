import {SelectionText} from "./Selection.js";
import {useCopy} from "../ui/PageFrame.js";
import {applicationBindings} from "../input/router.js";
// src/components/FormalPanel.tsx — /runtime formal: formal commits + delete/dissolve
import React, { memo, useState, useEffect, useCallback } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { formalList, formalDelete, formalDissolve } from "../backend/tools.js";
import { resolveFormalKey } from "../input/overlays/formal.js";
import { colors, usePanelSize } from "../theme/index.js";
import { ConfirmBox } from "./ConfirmBox.js";
import { chordLabel } from "../input/bindings.js";

type Formal = {
  index: number; prefix: string; number: number; message: string;
  synced: boolean; pushed: boolean; is_incoming: boolean; created_at: string;
};

type Props = {
  client: BackendClient;
  project: string;
  cols: number;
  onDismiss: () => void;
};

export const FormalPanel = memo(function FormalPanel({ client, project, onDismiss }: Props) {
  const t=useCopy();
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
  }, {bindingContext: confirm ? "" : "formal"});

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

  if (loading) return <Box padding={1}><Text dimColor>{t({en:"Loading formal commits...",zh:"正在读取正式提交…"})}</Text></Box>;

  return (
    <Box flexDirection="column" padding={1} width={w}>
      <Box marginBottom={1}>
        <Text bold>{t({en:"Formal Commits: ",zh:"正式提交： "})}{project}</Text>
        <Text dimColor>    {items.length} {t({en:" commits",zh:" 项提交"})}</Text>
      </Box>
      {status ? <Text dimColor>{status}</Text> : null}

      {items.length === 0 ? (
        <Text dimColor>{t({en:"No formal commits.",zh:"尚无正式提交。"})}</Text>
      ) : (
        items.map((it, i) => {
          const active = i === sel;
          const syncLabel = it.pushed ? "pushed" : it.synced ? "synced" : "local";
          return (
            <Box key={it.index} flexDirection="row">
              <SelectionText selected={active}  >
                [{it.prefix}-{it.number}]  {syncLabel}
              </SelectionText>
              {active && (
                <Text dimColor>  {it.message.slice(0, Math.max(10, w - 32))}</Text>
              )}
            </Box>
          );
        })
      )}

      <Box marginTop={1}>
        <Text dimColor>{applicationBindings.label("formal.delete")} {t({en:" delete    ",zh:" 删除    "})}{applicationBindings.label("formal.dissolve")} {t({en:" dissolve    ",zh:" 解散    "})}{chordLabel("upDown")} {t({en:" select    ",zh:" 选择    "})}{chordLabel("escape")} {t({en:" back",zh:" 返回"})}</Text>
        <Text dimColor>{t({en:"edit message via /runtime formal edit &lt;index&gt; &lt;message&gt;",zh:"用 /runtime formal edit &lt;index&gt; &lt;message&gt; 修改提交信息"})}</Text>
      </Box>
    </Box>
  );
});
